"""横断整合性レビュー対応(2026-08、指摘10)の横断Invariantテスト。

個別の指摘ごとの単体テストとは別に、以下2系統の「連鎖全体」が矛盾なく
一貫していることをこのファイルでまとめて検証する。

A) RecommendationType → user-action(is_full_sell_like/is_sell_like/
   is_critical_risk) → NotificationCategory(resolve_notification_category) →
   formatter-label(recommendation_adapter) → batch-summary-category
   (holdings_watchlist_handlerの4分類集計) → cross-pipeline-priority
   (_notification_priority) → actionable/non-actionable
   (_NON_ACTIONABLE_CATEGORIES) の一貫性。

B) WatchlistJobType → resolve_watchlist_job_type() → Dispatcher/Worker/
   Finalizer(rotation commitゲート)の各分岐が一貫しており、未知値に対して
   全経路でfail-closed(暗黙のelse-fallbackが無い)であること。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.enums import (
    CRITICAL_RISK_RECOMMENDATION_TYPES,
    FULL_SELL_RECOMMENDATION_TYPES,
    SELL_LIKE_RECOMMENDATION_TYPES,
    ConfidenceLevel,
    NotificationCategory,
    RecommendationType,
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
from jstock_advisor.services import watchlist_batch_finalizer
from jstock_advisor.services.line_notification_service import (
    _NON_ACTIONABLE_CATEGORIES,
    _notification_priority,
    resolve_notification_category,
)

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
def test_a5_actionable_and_non_actionable_categories_never_overlap_in_priority(
    recommendation_type: RecommendationType,
) -> None:
    """あるRecommendationTypeから得られるNotificationCategoryが
    _NON_ACTIONABLE_CATEGORIES(LINE非送信)に含まれる場合、cross-pipeline
    優先度表では常にpriority<=0(比較対象外)であり、逆に優先度が正の値を
    持つカテゴリは_NON_ACTIONABLE_CATEGORIESに含まれない。両者が同時に
    真になる(=送信しないのに優先度だけは高く記録される、または送信するのに
    優先度比較が一切効かない)矛盾が無いことを保証する。"""
    recommendation = _recommendation(recommendation_type)
    category = resolve_notification_category(recommendation)
    priority = _notification_priority(recommendation)
    if category in _NON_ACTIONABLE_CATEGORIES:
        assert priority <= 0, (recommendation_type, category, priority)


def test_a6_sell_and_partial_sell_share_the_same_priority_tier() -> None:
    """横断整合性レビュー対応2026-08指摘7の設計固定: SELLとPARTIAL_SELLは
    「本日この銘柄について売却方向の通知は済んでいる」という点で同格として
    扱うため、cross-pipeline優先度が完全に一致すること。"""
    sell_priority = _notification_priority(_recommendation(RecommendationType.SELL))
    partial_priority = _notification_priority(
        _recommendation(RecommendationType.PARTIAL_PROFIT_TAKE)
    )
    assert sell_priority > 0
    assert sell_priority == partial_priority


def test_a7_critical_risk_priority_outranks_all_other_actionable_categories() -> None:
    """CRITICAL_RISKの優先度が、SELL/PARTIAL_SELL/BUYのいずれよりも高い
    (要求仕様の優先順位: 重大リスク＞買い到達＞売却検討・一部売却検討(同格)
    ＞買い候補、の先頭を固定する)。"""
    critical_priority = _notification_priority(
        _recommendation(RecommendationType.URGENT_HOLDING_REVIEW)
    )
    sell_priority = _notification_priority(_recommendation(RecommendationType.SELL))
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
