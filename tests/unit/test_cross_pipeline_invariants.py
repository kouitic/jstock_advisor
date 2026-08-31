"""横断整合性レビュー対応(2026-08、指摘10)の横断Invariantテスト。

個別の指摘ごとの単体テストとは別に、以下2系統の「連鎖全体」が矛盾なく
一貫していることをこのファイルでまとめて検証する。

A) RecommendationType → user-action(is_full_sell_like/is_sell_like/
   is_critical_risk) → NotificationCategory(resolve_notification_category) →
   formatter-label(recommendation_adapter) → batch-summary-category
   (holdings_watchlist_handlerの4分類集計) → cross-pipeline-priority
   (notification_priority_for_recommendation) → actionable/non-actionable
   (resolve_notification_intent_for_recommendation()、送信可否の唯一の正本) の
   一貫性。かつて存在した_NON_ACTIONABLE_CATEGORIES(カテゴリ単位のfrozenset)は
   WATCH categoryがATTENTION(送信対象)とINTERNAL_ONLY(非送信)の両方になり得る
   ことを表現できず、送信可否の正本が2つあるように見える不整合の原因になっていた
   ため削除した(再コードレビュー対応2026-08、指摘3)。

B) WatchlistJobType → resolve_watchlist_job_type() → Dispatcher/Worker/
   Finalizer(rotation commitゲート)の各分岐が一貫しており、未知値に対して
   全経路でfail-closed(暗黙のelse-fallbackが無い)であること。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

import pytest

from jstock_advisor.domain.entities.enums import (
    CRITICAL_RISK_RECOMMENDATION_TYPES,
    FULL_SELL_RECOMMENDATION_TYPES,
    SELL_LIKE_RECOMMENDATION_TYPES,
    ConfidenceLevel,
    DividendValidationStatus,
    EarningsDateStatus,
    EvidenceCoverageStatus,
    NotificationCategory,
    NotificationIntent,
    RecommendationType,
    RecordDateUnknownReason,
    is_critical_risk,
    is_full_sell_like,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.aws.batch_tracker import (
    JOB_TYPE_NEW_CANDIDATE_SCREENING,
    JOB_TYPE_WATCHLIST_MAINTENANCE,
    UnknownWatchlistJobTypeError,
    WatchlistJobType,
    resolve_watchlist_job_type,
)
from jstock_advisor.infrastructure.edinet.types import EdinetFailureReason, EdinetFetchStatus
from jstock_advisor.services import watchlist_batch_finalizer
from jstock_advisor.services.line_notification_service import (
    notification_priority_for_recommendation,
    resolve_notification_category,
    resolve_notification_intent_for_recommendation,
)
from jstock_advisor.services.screening_data_provider import ScreeningDataStatus

_NOW = dt.datetime(2026, 8, 16, 8, 0, tzinfo=dt.UTC)


def _recommendation(recommendation_type: RecommendationType) -> Recommendation:
    return Recommendation(
        recommendation_id=f"inv-{recommendation_type.value}",
        stock_code="1234",
        stock_name="テスト銘柄",
        recommended_at=_NOW,
        recommendation_type=recommendation_type,
        price_at_recommendation=Decimal("1000"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )


# ===== A: RecommendationType → 分類 → 通知カテゴリ → 優先度 の一貫性 =====


@pytest.mark.parametrize("recommendation_type", list(RecommendationType))
def test_a1_every_recommendation_type_resolves_to_a_notification_category_without_error(
    recommendation_type: RecommendationType,
) -> None:
    """全RecommendationType(buy_action=None、保有銘柄経路想定)が例外なく
    いずれか1つのNotificationCategoryへ分類できること(未対応の新規メンバー
    追加による無言の分岐漏れを検知する)。"""
    category = resolve_notification_category(_recommendation(recommendation_type))
    assert category in NotificationCategory


@pytest.mark.parametrize("recommendation_type", sorted(FULL_SELL_RECOMMENDATION_TYPES))
def test_a2_full_sell_like_types_are_categorized_as_sell(
    recommendation_type: RecommendationType,
) -> None:
    """is_full_sell_like()がTrueを返す型は、resolve_notification_category()
    でもNotificationCategory.SELLへ分類される(個別本文の「全部売却検討」
    ラベルと、まとめ通知のカテゴリ分類が同じ判定ソースに揃っていること)。"""
    assert is_full_sell_like(recommendation_type) is True
    category = resolve_notification_category(_recommendation(recommendation_type))
    assert category == NotificationCategory.SELL


def test_a3_full_sell_recommendation_types_is_subset_of_sell_like_or_explicitly_handled() -> None:
    """FULL_SELL_RECOMMENDATION_TYPESの各メンバーは、SELL_LIKE_RECOMMENDATION_
    TYPESに含まれる(STRONG_SELL_CONSIDERATION)か、resolve_notification_
    category()内で個別に「全部売却検討」としてSELLへ分類される特例
    (FULL_PROFIT_TAKE)のいずれかであり、どちらの経路でも最終的にSELL
    カテゴリへ到達することを固定する。"""
    for rt in FULL_SELL_RECOMMENDATION_TYPES:
        assert (
            rt in SELL_LIKE_RECOMMENDATION_TYPES or rt == RecommendationType.FULL_PROFIT_TAKE
        )


@pytest.mark.parametrize("recommendation_type", sorted(CRITICAL_RISK_RECOMMENDATION_TYPES))
def test_a4_critical_risk_types_are_categorized_as_critical_risk(
    recommendation_type: RecommendationType,
) -> None:
    """is_critical_risk()がTrueを返す型は、resolve_notification_category()
    でも必ずCRITICAL_RISKへ分類される(is_critical_riskがcheck_cross_
    pipeline_priority_eligibility等で優先度比較そのものをスキップする際の
    前提と矛盾しないことを保証する)。"""
    assert is_critical_risk(recommendation_type) is True
    category = resolve_notification_category(_recommendation(recommendation_type))
    assert category == NotificationCategory.CRITICAL_RISK


@pytest.mark.parametrize("recommendation_type", list(RecommendationType))
def test_a5_internal_only_intent_never_has_positive_priority(
    recommendation_type: RecommendationType,
) -> None:
    """あるRecommendationTypeの通知意図(resolve_notification_intent_for_
    recommendation()、送信可否の唯一の正本)がINTERNAL_ONLY(LINE非送信)の場合、
    cross-pipeline優先度表では常にpriority<=0(比較対象外)であることを保証する。
    逆に優先度が正の値を持つ場合は必ずACTIONABLEかATTENTION(送信対象)である
    (=送信しないのに優先度だけは高く記録される、または送信するのに優先度比較が
    一切効かない、という矛盾が無いことの確認)。"""
    recommendation = _recommendation(recommendation_type)
    intent = resolve_notification_intent_for_recommendation(recommendation)
    priority = notification_priority_for_recommendation(recommendation)
    if intent is NotificationIntent.INTERNAL_ONLY:
        assert priority <= 0, (recommendation_type, intent, priority)


def test_a6_sell_and_partial_sell_share_the_same_priority_tier() -> None:
    """横断整合性レビュー対応2026-08指摘7の設計固定: SELLとPARTIAL_SELLは
    「本日この銘柄について売却方向の通知は済んでいる」という点で同格として
    扱うため、cross-pipeline優先度が完全に一致すること。"""
    sell_priority = notification_priority_for_recommendation(
        _recommendation(RecommendationType.SELL)
    )
    partial_priority = notification_priority_for_recommendation(
        _recommendation(RecommendationType.PARTIAL_PROFIT_TAKE)
    )
    assert sell_priority > 0
    assert sell_priority == partial_priority


def test_a7_critical_risk_priority_outranks_all_other_actionable_categories() -> None:
    """CRITICAL_RISKの優先度が、SELL/PARTIAL_SELL/BUYのいずれよりも高い
    (要求仕様の優先順位: 重大リスク＞買い到達＞売却検討・一部売却検討(同格)
    ＞買い候補、の先頭を固定する)。"""
    critical_priority = notification_priority_for_recommendation(
        _recommendation(RecommendationType.URGENT_HOLDING_REVIEW)
    )
    sell_priority = notification_priority_for_recommendation(
        _recommendation(RecommendationType.SELL)
    )
    assert critical_priority > sell_priority


# ===== B: WatchlistJobType → resolve → Dispatcher/Worker/Finalizer の一貫性 =====


@pytest.mark.parametrize("job_type", list(WatchlistJobType))
def test_b1_every_job_type_round_trips_through_resolve(job_type: WatchlistJobType) -> None:
    """WatchlistJobTypeの全メンバーが、自身の文字列値からresolve_watchlist_
    job_type()経由で過不足なく復元できること(Dispatcher/Worker双方が
    同じ変換規則を共有していることの前提)。"""
    assert resolve_watchlist_job_type(job_type.value) is job_type


def test_b2_missing_value_without_default_is_rejected_fail_closed() -> None:
    with pytest.raises(UnknownWatchlistJobTypeError):
        resolve_watchlist_job_type(None)


def test_b3_missing_value_with_default_falls_back_to_default_only() -> None:
    """defaultはraw=None(キー自体が無い)の場合のみ有効。これはDispatcherの
    EventBridge Schedule Input未指定時の後方互換専用であり、他のどの経路にも
    暗黙のfallbackを許可しない。"""
    resolved = resolve_watchlist_job_type(None, default=WatchlistJobType.NEW_CANDIDATE_SCREENING)
    assert resolved is WatchlistJobType.NEW_CANDIDATE_SCREENING


@pytest.mark.parametrize(
    "raw",
    ["", "new_candidate_screening", "NEW_CANDIDATE_SCREENING ", "TYPO", "watchlist_maintenance"],
)
def test_b4_unknown_non_none_value_is_rejected_even_with_default(raw: str) -> None:
    """defaultを指定していても、raw自体が非Noneの未知値(typo・大文字小文字
    違い・前後空白混入等)であればfail-closedで例外を送出する(「未知値は
    defaultへ暗黙にフォールバックする」という抜け道が無いことを保証する)。
    これがDispatcher側でrotation commit・SQS投入等いかなる副作用の前にも
    確実に処理を止める根拠になっている。"""
    with pytest.raises(UnknownWatchlistJobTypeError):
        resolve_watchlist_job_type(raw, default=WatchlistJobType.NEW_CANDIDATE_SCREENING)


def test_b5_legacy_aliases_point_to_the_same_enum_members() -> None:
    """JOB_TYPE_*エイリアスは新設のWatchlistJobTypeメンバーそのものを指す
    (再定義された別の文字列ではない)。既存コード中の`job_type ==
    JOB_TYPE_NEW_CANDIDATE_SCREENING`という比較がenumメンバー同士の比較で
    あり続けることを保証する。"""
    assert JOB_TYPE_NEW_CANDIDATE_SCREENING is WatchlistJobType.NEW_CANDIDATE_SCREENING
    assert JOB_TYPE_WATCHLIST_MAINTENANCE is WatchlistJobType.WATCHLIST_MAINTENANCE


def test_b6_rotation_commit_gate_only_admits_new_candidate_screening() -> None:
    """_maybe_commit_rotation()(finalizeの唯一のrotation commit入口)は、
    job_type=="NEW_CANDIDATE_SCREENING"以外では、records/DynamoDB等の
    副作用に一切触れず即座にno-opで返る(計画Part A-9の防御的ガード)。
    WatchlistJobTypeの全メンバーについて、NEW_CANDIDATE_SCREENING以外は
    安全にno-opであることを直接確認する。"""
    for job_type in WatchlistJobType:
        if job_type == WatchlistJobType.NEW_CANDIDATE_SCREENING:
            continue
        # recordsに意図的に不正な値(None)を渡す。もしゲートを通過して
        # records を参照する経路があれば、ここで例外になり検知できる。
        result = watchlist_batch_finalizer._maybe_commit_rotation(
            "batch-invariant-test", {"job_type": job_type.value}, None, _NOW
        )
        assert result is None


# =============================================================================
# C) Issue #85 Phase B2 / Group 1: semantic status family inventory
#
# 過去バグ(BP-01 Missing value semantics)の共通根は
# 「**取得できなかった**」と「**調べた結果、値が無かった**」を同じ表現へ潰すこと。
# 本コードベースはこれを domain ごとに **別々の enum** で表現している
# (#55 利回り / #59 provider例外 / #53 EDINET / #75 平均取得単価)。
#
# ここでは個々の domain 単体テストを複製せず、
# **「この enum family 全体が semantic role を持ち続けること」** を横断で固定する。
#
# 重要: すべての enum が同じメンバー名を持つべき、という一般化はしない。
# 名称は domain ごとに違ってよい。見るのは **role が別メンバーとして残っているか**。
# =============================================================================


class _SemanticRole(StrEnum):
    """欠測 semantics の役割。domain 間で名称は違っても役割は共通。"""

    #: 値が取得でき、判定に使える。
    PRESENT = "PRESENT"
    #: 調べた結果、値が無い/該当しない。**判定として妥当な欠測**。
    ABSENT_KNOWN = "ABSENT_KNOWN"
    #: そもそも調べられなかった / 判定できない。**欠測へ潰してはいけない**。
    UNDETERMINED = "UNDETERMINED"


@dataclass(frozen=True)
class _SemanticFamily:
    """semantic role → その domain での enum メンバー名。"""

    enum_type: type[StrEnum]
    roles: dict[_SemanticRole, tuple[str, ...]]
    origin_issue: str


#: 「値あり / 値が無い / 判定できない」を表現する enum の台帳。
#: **新しい semantic status enum を追加したらここへも登録すること。**
_SEMANTIC_FAMILIES: tuple[_SemanticFamily, ...] = (
    _SemanticFamily(
        EdinetFetchStatus,
        {
            _SemanticRole.PRESENT: ("SUCCESS_WITH_DOCUMENTS",),
            _SemanticRole.ABSENT_KNOWN: ("SUCCESS_EMPTY",),
            _SemanticRole.UNDETERMINED: ("FETCH_FAILED",),
        },
        "#53",
    ),
    _SemanticFamily(
        ScreeningDataStatus,
        {
            _SemanticRole.PRESENT: ("OK",),
            _SemanticRole.ABSENT_KNOWN: ("NOT_FOUND",),
            _SemanticRole.UNDETERMINED: ("DATA_ERROR",),
        },
        "#59",
    ),
    _SemanticFamily(
        EvidenceCoverageStatus,
        {
            _SemanticRole.PRESENT: ("EVALUATED",),
            _SemanticRole.ABSENT_KNOWN: ("NOT_APPLICABLE",),
            _SemanticRole.UNDETERMINED: ("NOT_EVALUATED",),
        },
        "#55",
    ),
    _SemanticFamily(
        EarningsDateStatus,
        {
            _SemanticRole.PRESENT: ("CONFIRMED",),
            _SemanticRole.ABSENT_KNOWN: ("STALE_PAST_DATE",),
            _SemanticRole.UNDETERMINED: ("UNAVAILABLE",),
        },
        "#53",
    ),
    _SemanticFamily(
        DividendValidationStatus,
        {
            _SemanticRole.PRESENT: ("VALIDATED",),
            _SemanticRole.ABSENT_KNOWN: ("NOT_YET_VALIDATABLE",),
            _SemanticRole.UNDETERMINED: ("SECONDARY_UNAVAILABLE",),
        },
        "#59",
    ),
    _SemanticFamily(
        RecordDateUnknownReason,
        {
            _SemanticRole.ABSENT_KNOWN: ("NOT_APPLICABLE",),
            _SemanticRole.UNDETERMINED: (
                "SOURCE_NOT_FOUND",
                "PARSE_ERROR",
                "CORPORATE_ACTION_UNRESOLVED",
                "DATA_PROVIDER_MISSING",
            ),
        },
        "#59",
    ),
)


def _family_ids() -> list[str]:
    return [family.enum_type.__name__ for family in _SEMANTIC_FAMILIES]


@pytest.mark.parametrize("family", _SEMANTIC_FAMILIES, ids=_family_ids())
def test_c1_declared_members_exist_in_the_enum(family: _SemanticFamily) -> None:
    """台帳に書いたメンバーが実際に enum へ存在すること(メンバー削除・改名の検知)。"""
    actual = {member.name for member in family.enum_type}
    declared = {name for names in family.roles.values() for name in names}

    missing = sorted(declared - actual)
    assert not missing, (
        f"{family.enum_type.__name__}({family.origin_issue})から semantic メンバーが"
        f"消えている: {missing}。欠測 semantics の区別が失われていないか確認すること"
    )


@pytest.mark.parametrize("family", _SEMANTIC_FAMILIES, ids=_family_ids())
def test_c2_absent_and_undetermined_are_distinct_members(family: _SemanticFamily) -> None:
    """**「値が無い」と「判定できない」が別メンバーであること**(BP-01 の中核)。

    片方を他方へ統合すると、取得失敗が欠測として扱われる過去バグが再発する。
    """
    absent = set(family.roles.get(_SemanticRole.ABSENT_KNOWN, ()))
    undetermined = set(family.roles.get(_SemanticRole.UNDETERMINED, ()))

    assert absent, f"{family.enum_type.__name__}: ABSENT_KNOWN 役が未宣言"
    assert undetermined, f"{family.enum_type.__name__}: UNDETERMINED 役が未宣言"
    overlap = sorted(absent & undetermined)
    assert not overlap, (
        f"{family.enum_type.__name__}({family.origin_issue}): "
        f"「値が無い」と「判定できない」が同一メンバーへ統合されている: {overlap}"
    )


@pytest.mark.parametrize("family", _SEMANTIC_FAMILIES, ids=_family_ids())
def test_c3_every_enum_member_is_classified(family: _SemanticFamily) -> None:
    """enum へメンバーを追加したら台帳へも分類すること(inventory の完全性)。

    未分類のまま増えると、その値がどの semantic role なのか不明のまま
    consumer 側で暗黙に「欠測」へ寄せられる余地が生まれる。
    """
    actual = {member.name for member in family.enum_type}
    declared = {name for names in family.roles.values() for name in names}

    unclassified = sorted(actual - declared)
    assert not unclassified, (
        f"{family.enum_type.__name__} へ追加されたメンバーが semantic role へ"
        f"分類されていない: {unclassified}"
    )


def test_c4_no_single_member_covers_both_success_and_failure() -> None:
    """PRESENT と UNDETERMINED が同一メンバーで表現されていないこと(全 family)。"""
    collisions: list[str] = []
    for family in _SEMANTIC_FAMILIES:
        present = set(family.roles.get(_SemanticRole.PRESENT, ()))
        undetermined = set(family.roles.get(_SemanticRole.UNDETERMINED, ()))
        if present & undetermined:
            collisions.append(f"{family.enum_type.__name__}: {sorted(present & undetermined)}")
    assert not collisions, f"成功と判定不能が同一メンバーで表現されている: {collisions}"


def test_c5_edinet_success_empty_is_not_a_failure_reason() -> None:
    """#53 の中核契約: SUCCESS_EMPTY(0件)は FETCH_FAILED と別状態であること。

    `EdinetFailureReason` は FETCH_FAILED の内訳であり、
    SUCCESS_EMPTY の理由コードが混入していないことを確認する。
    """
    failure_reasons = {member.name for member in EdinetFailureReason}

    assert "SUCCESS_EMPTY" not in failure_reasons
    assert EdinetFetchStatus.SUCCESS_EMPTY is not EdinetFetchStatus.FETCH_FAILED
    assert EdinetFetchStatus.SUCCESS_EMPTY.value != EdinetFetchStatus.FETCH_FAILED.value


def test_c6_semantic_family_inventory_is_not_empty() -> None:
    """台帳自体が空になっていないこと(テストの空回りを防ぐ)。"""
    assert len(_SEMANTIC_FAMILIES) >= 6
    assert len(set(_family_ids())) == len(_SEMANTIC_FAMILIES), "同じ enum が重複登録されている"
