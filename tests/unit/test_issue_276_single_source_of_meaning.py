"""Issue #276(N-11): 同じ意味の重複定義 4 群を、単一の定義へ集約したことの固定。

設計: #276 issuecomment-5956301317(G1〜G4・T1〜T7)。USER 決定(G3 = Option A): …-5962328625。
**挙動不変**(USER_VISIBLE_CHANGE = NO)。通知・判定・保存データの値は変えない。

  G1  sell-like 集合: 公開定数 LEGACY / HOLDING_DECISION から SELL_LIKE を導出。私的コピーの削除
  G2  WATCH_FAMILY_ACTIONS / BUY_FAMILY_ACTIONS を、インラインの同内容の集合の代わりに使う
  G3  「要確認」を表す 4 つの別 layer の enum: コードは統合せず、対応表 1 つ + 一致 / drift テスト
  G4  ProfitProtectionSignal(StrEnum): 生成側と判定側が同じ enum を参照する

本テストは実データを使わない(enum の値のみ)。
"""

from __future__ import annotations

import ast
import itertools
import json
from enum import Enum
from pathlib import Path

import pytest

from jstock_advisor.domain.entities import enums
from jstock_advisor.domain.entities.enums import (
    BUY_FAMILY_ACTIONS,
    FULL_SELL_RECOMMENDATION_TYPES,
    HOLDING_DECISION_RECOMMENDATION_TYPES,
    LEGACY_SELL_RECOMMENDATION_TYPES,
    SELL_LIKE_RECOMMENDATION_TYPES,
    WATCH_FAMILY_ACTIONS,
    BuyAction,
    JudgmentStrength,
    NotificationCategory,
    NotificationIntent,
    NotificationType,
    ProfitProtectionSignal,
    PurchaseCategory,
    RecommendationType,
)
from jstock_advisor.domain.notification.notification_intent import (
    resolve_attention_origin,
    resolve_notification_intent,
)
from jstock_advisor.domain.signals.profit_protection import ProfitProtectionMetrics

_SRC = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"
_RT = RecommendationType
_BA = BuyAction


# --- G1: sell-like 集合 --------------------------------------------------------------


def test_t1_sell_like_is_derived_from_the_legacy_and_holding_decision_sets() -> None:
    """[AC1・AC3] SELL_LIKE は LEGACY | HOLDING_DECISION の導出で、値は従来と同一。"""
    assert {_RT.SELL, _RT.URGENT_REVIEW, _RT.REVIEW} == LEGACY_SELL_RECOMMENDATION_TYPES
    assert {
        _RT.SELL_CONSIDERATION,
        _RT.STRONG_SELL_CONSIDERATION,
        _RT.URGENT_HOLDING_REVIEW,
    } == HOLDING_DECISION_RECOMMENDATION_TYPES
    assert SELL_LIKE_RECOMMENDATION_TYPES == (
        LEGACY_SELL_RECOMMENDATION_TYPES | HOLDING_DECISION_RECOMMENDATION_TYPES
    )
    # 導出前の値(6 件)そのもの
    assert {
        _RT.SELL,
        _RT.URGENT_REVIEW,
        _RT.REVIEW,
        _RT.SELL_CONSIDERATION,
        _RT.STRONG_SELL_CONSIDERATION,
        _RT.URGENT_HOLDING_REVIEW,
    } == SELL_LIKE_RECOMMENDATION_TYPES
    assert isinstance(SELL_LIKE_RECOMMENDATION_TYPES, frozenset)
    assert not (LEGACY_SELL_RECOMMENDATION_TYPES & HOLDING_DECISION_RECOMMENDATION_TYPES)
    # FULL_PROFIT_TAKE は SELL_LIKE に含まれない(既存の関係。FULL_SELL は部分集合ではない)
    assert _RT.FULL_PROFIT_TAKE not in SELL_LIKE_RECOMMENDATION_TYPES
    assert _RT.FULL_PROFIT_TAKE in FULL_SELL_RECOMMENDATION_TYPES


def test_private_names_remain_as_aliases_of_the_public_sets() -> None:
    """既存の参照(tests/unit/test_enums.py 等)のため、private 名は同じ値を指す alias として残る。"""
    assert enums._LEGACY_SELL_RECOMMENDATION_TYPES is LEGACY_SELL_RECOMMENDATION_TYPES
    assert enums._HOLDING_DECISION_RECOMMENDATION_TYPES is HOLDING_DECISION_RECOMMENDATION_TYPES


def test_t7_strong_types_of_the_legacy_sell_are_derived() -> None:
    """[AC4] _STRONG_TYPES は LEGACY − {REVIEW}(従来の (SELL, URGENT_REVIEW) と同じ)。"""
    from jstock_advisor.services import sell_signal_service

    assert set(sell_signal_service._STRONG_TYPES) == {_RT.SELL, _RT.URGENT_REVIEW}
    assert (
        LEGACY_SELL_RECOMMENDATION_TYPES - {_RT.REVIEW}
    ) == sell_signal_service._STRONG_TYPES


def test_t3_evaluation_exit_types_are_intentionally_not_sell_like() -> None:
    """★ 意図した差の固定(#270 の設計): `_EXIT_TYPES`(株価の下落が成功を意味する型)は
    SELL_LIKE(通知側の概念)と別集合で、統合してはならない。その差の内訳を固定する。"""
    from jstock_advisor.domain.evaluation_rules import _EXIT_TYPES

    exit_types = set(_EXIT_TYPES)
    assert exit_types != set(SELL_LIKE_RECOMMENDATION_TYPES)
    assert exit_types - SELL_LIKE_RECOMMENDATION_TYPES == {
        _RT.PARTIAL_PROFIT_TAKE,
        _RT.FULL_PROFIT_TAKE,
        _RT.WATCH,
        _RT.PARTIAL_RISK_REDUCTION,
    }
    assert SELL_LIKE_RECOMMENDATION_TYPES - exit_types == {_RT.URGENT_HOLDING_REVIEW}


# --- G2: family 集合 -------------------------------------------------------------------


def test_family_action_sets_have_the_expected_members() -> None:
    assert {_BA.WATCH_FOR_PRICE, _BA.WATCH_BEFORE_EARNINGS} == WATCH_FAMILY_ACTIONS
    assert {_BA.STRONG_BUY, _BA.BUY, _BA.SMALL_ENTRY} == BUY_FAMILY_ACTIONS


def _enum_member_set(node: ast.AST) -> frozenset[tuple[str, str]] | None:
    """set / tuple / list リテラル(または frozenset({...}) の引数)の要素が全て
    `Enum.MEMBER` なら、その集合を返す。"""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
        "frozenset",
        "set",
        "tuple",
        "list",
    }:
        if len(node.args) != 1:
            return None
        node = node.args[0]
    if not isinstance(node, ast.Set | ast.Tuple | ast.List):
        return None
    members: set[tuple[str, str]] = set()
    for element in node.elts:
        if not (
            isinstance(element, ast.Attribute) and isinstance(element.value, ast.Name)
        ):
            return None
        members.add((element.value.id, element.attr))
    return frozenset(members) if members else None


_FAMILIES = {
    "WATCH_FAMILY_ACTIONS": frozenset(
        {("BuyAction", "WATCH_FOR_PRICE"), ("BuyAction", "WATCH_BEFORE_EARNINGS")}
    ),
    "BUY_FAMILY_ACTIONS": frozenset(
        {("BuyAction", "STRONG_BUY"), ("BuyAction", "BUY"), ("BuyAction", "SMALL_ENTRY")}
    ),
    "LEGACY_SELL_RECOMMENDATION_TYPES": frozenset(
        {
            ("RecommendationType", "SELL"),
            ("RecommendationType", "URGENT_REVIEW"),
            ("RecommendationType", "REVIEW"),
        }
    ),
    "HOLDING_DECISION_RECOMMENDATION_TYPES": frozenset(
        {
            ("RecommendationType", "SELL_CONSIDERATION"),
            ("RecommendationType", "STRONG_SELL_CONSIDERATION"),
            ("RecommendationType", "URGENT_HOLDING_REVIEW"),
        }
    ),
}


def test_t2_no_private_copies_of_the_family_sets_outside_enums() -> None:
    """[AC2] 4 つの集合と同じ内容の set / tuple リテラルが、定義元(enums.py)以外の src に無い。"""
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel == "domain/entities/enums.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            members = _enum_member_set(node)
            if members is None:
                continue
            for name, family in _FAMILIES.items():
                if members == family:
                    offenders.append(f"{rel}:{node.lineno} は {name} と同じ内容のリテラル")
    assert offenders == []


def test_t2_the_old_private_holding_decision_copy_is_gone() -> None:
    from jstock_advisor.services import line_notification_service

    assert not hasattr(line_notification_service, "_HOLDING_DECISION_RECOMMENDATION_TYPES")
    assert line_notification_service.HOLDING_DECISION_RECOMMENDATION_TYPES is (
        HOLDING_DECISION_RECOMMENDATION_TYPES
    )


# --- G3: 「要確認」を表す 4 つの enum(Option A: 対応表 1 つ + 一致 / drift テスト) -------------

#: 『要確認』に当たる member の対応表(★ 単一の定義)。4 つは別 layer の別 enum で、段階ごとに
#: 「要確認」へ写像される(統合しない。USER 決定 G3 = Option A)。新しい MANUAL_REVIEW* /
#: REVIEW の member を足したときに、この表へ載せ忘れると下の drift テストが赤くなる。
MANUAL_REVIEW_MEMBERS: dict[type, frozenset[str]] = {
    # 判定結果。REVIEW はユーザー向けには「要確認」として見せる(通知カテゴリ MANUAL_REVIEW)。
    # MANUAL_REVIEW_REQUIRED は現状、生成元が無い
    RecommendationType: frozenset({"REVIEW", "MANUAL_REVIEW_REQUIRED"}),
    # 買い判定の行動
    BuyAction: frozenset({"MANUAL_REVIEW"}),
    # 購入候補のカテゴリ
    PurchaseCategory: frozenset({"MANUAL_REVIEW"}),
    # 通知カテゴリ
    NotificationCategory: frozenset({"MANUAL_REVIEW"}),
    # ★ 実装時の実測で見つけた、設計時の 4 つ以外の 2 つ(設計は『4 つ』と記録していた)
    # 通知の種別。RecommendationType.MANUAL_REVIEW_REQUIRED から 1 対 1 に写像される
    # (services/line_notification_service.py の対応表)
    NotificationType: frozenset({"MANUAL_REVIEW_REQUIRED"}),
    # 推奨判定の強度の段(INFO < WATCH < REVIEW < …)。『要確認』と同義の別 layer の enum ではなく
    # 強度の順序の 1 段だが、同じ命名(REVIEW)のため、取り違えを防ぐ目的で表に載せる
    JudgmentStrength: frozenset({"REVIEW"}),
}


def _review_like_member_names(enum_cls: type) -> frozenset[str]:
    """『要確認』を表す命名の member: 名前が MANUAL_REVIEW で始まるもの、または REVIEW そのもの。"""
    return frozenset(
        member.name
        for member in enum_cls  # type: ignore[attr-defined]
        if member.name.startswith("MANUAL_REVIEW") or member.name == "REVIEW"
    )


@pytest.mark.parametrize("enum_cls", list(MANUAL_REVIEW_MEMBERS))
def test_t6_manual_review_members_match_the_mapping_table(enum_cls: type) -> None:
    """[AC4] 4 つの enum の『要確認』の member が対応表と一致する(新 member の追加で赤になる)。"""
    assert _review_like_member_names(enum_cls) == MANUAL_REVIEW_MEMBERS[enum_cls]
    for name in MANUAL_REVIEW_MEMBERS[enum_cls]:
        assert hasattr(enum_cls, name)


def test_t6_the_mapping_table_covers_all_enums_that_have_review_like_members() -> None:
    """表に載っていない enum に『要確認』の member が生えたら赤にする(全 enum を走査)。"""
    unlisted: list[str] = []
    for obj in vars(enums).values():
        if (
            isinstance(obj, type)
            and issubclass(obj, Enum)
            and obj not in MANUAL_REVIEW_MEMBERS
            and _review_like_member_names(obj)
        ):
            unlisted.append(obj.__name__)
    assert unlisted == []


def test_buy_action_manual_review_is_labelled_as_such() -> None:
    assert enums.buy_action_label(BuyAction.MANUAL_REVIEW) == "要確認"


# --- G4: ProfitProtectionSignal ----------------------------------------------------------

_SIGNAL_STRINGS = ("DATA_INSUFFICIENT", "STRONG", "CANDIDATE", "NONE")


def test_profit_protection_signal_members_equal_the_historical_strings() -> None:
    assert [m.value for m in ProfitProtectionSignal] == list(_SIGNAL_STRINGS)
    for text in _SIGNAL_STRINGS:
        assert ProfitProtectionSignal(text) == text  # StrEnum は str と等しい
    # 集合に入れた member が、同じ文字列で引ける(判定側の frozenset 比較が文字列でも成立する)
    assert "STRONG" in frozenset({ProfitProtectionSignal.STRONG})


def _metrics(*, insufficient: str | None, strong: bool, candidate: bool) -> ProfitProtectionMetrics:
    return ProfitProtectionMetrics(
        insufficient_data_reason=insufficient,
        peak_price_since_entry=None,
        peak_date=None,
        peak_gain_pct=None,
        current_gain_pct=None,
        drawdown_from_peak_pct=None,
        gain_giveback_ratio_pct=None,
        candidate_signal=candidate,
        strong_signal=strong,
    )


def test_t4_producer_labels_are_exactly_the_enum_members() -> None:
    """[AC1] producer(signal_label)が返す値の集合 = enum の全 member。"""
    produced = {
        _metrics(insufficient=ins, strong=s, candidate=c).signal_label
        for ins, s, c in itertools.product([None, "理由"], [False, True], [False, True])
    }
    assert produced == set(ProfitProtectionSignal)
    # 優先順位は従来のまま: 判定不能 > STRONG > CANDIDATE > NONE
    insufficient = _metrics(insufficient="x", strong=True, candidate=True)
    assert insufficient.signal_label == "DATA_INSUFFICIENT"
    assert _metrics(insufficient=None, strong=True, candidate=True).signal_label == "STRONG"
    assert _metrics(insufficient=None, strong=False, candidate=True).signal_label == "CANDIDATE"
    assert _metrics(insufficient=None, strong=False, candidate=False).signal_label == "NONE"
    # producer が返す値は enum の member(名前による生成)
    assert all(isinstance(v, ProfitProtectionSignal) for v in produced)


_ACTIONABLE = {
    NotificationCategory.CRITICAL_RISK,
    NotificationCategory.BUY,
    NotificationCategory.SELL,
    NotificationCategory.PARTIAL_SELL,
}


def _reference_intent(category: NotificationCategory, signal: str | None) -> NotificationIntent:
    """修正前の実装(文字列比較)を写した参照実装。"""
    if category is NotificationCategory.WATCH and signal in {"CANDIDATE", "STRONG"}:
        return NotificationIntent.ATTENTION
    if category in _ACTIONABLE:
        return NotificationIntent.ACTIONABLE
    return NotificationIntent.INTERNAL_ONLY


def _reference_origin(category: NotificationCategory, signal: str | None) -> str | None:
    if _reference_intent(category, signal) is not NotificationIntent.ATTENTION:
        return None
    if signal == "STRONG":
        return "PROFIT_PROTECTION_STRONG_NOT_EXECUTABLE"
    return "PROFIT_PROTECTION_CANDIDATE"


_SIGNAL_INPUTS: list[str | None] = [
    None,
    "",
    "unknown",
    "strong",  # 大文字小文字は区別される(従来どおり STRONG と等しくない)
    *_SIGNAL_STRINGS,
    *list(ProfitProtectionSignal),
]


@pytest.mark.parametrize(
    ("category", "signal"),
    list(itertools.product(list(NotificationCategory), _SIGNAL_INPUTS)),
)
def test_t5_notification_intent_is_unchanged_for_every_input(
    category: NotificationCategory, signal: str | None
) -> None:
    """[AC4] ★ 挙動不変: 全ての (カテゴリ × signal) の組で、文字列比較の参照実装と出力が一致する。
    enum の値や比較先を変えた実装では落ちる。"""
    assert resolve_notification_intent(category, signal) == _reference_intent(category, signal)
    assert resolve_attention_origin(category, signal) == _reference_origin(category, signal)


def test_t5_enum_member_and_plain_string_give_identical_results() -> None:
    for category in NotificationCategory:
        for member in ProfitProtectionSignal:
            assert resolve_notification_intent(category, member) == resolve_notification_intent(
                category, member.value
            )
            assert resolve_attention_origin(category, member) == resolve_attention_origin(
                category, member.value
            )


def test_serialization_of_the_signal_is_unchanged() -> None:
    """保存される値は従来の文字列のまま(Recommendation の型は str | None のまま)。"""
    from tests.factories import build_recommendation

    rec = build_recommendation(profit_protection_signal=ProfitProtectionSignal.STRONG)
    assert rec.profit_protection_signal == "STRONG"
    assert json.loads(rec.model_dump_json())["profit_protection_signal"] == "STRONG"
    assert json.dumps({"s": ProfitProtectionSignal.CANDIDATE}) == '{"s": "CANDIDATE"}'
    assert str(ProfitProtectionSignal.NONE) == "NONE"
    # 旧版の文字列で作った record と、保持される値が同じ
    legacy = build_recommendation(profit_protection_signal="STRONG")
    assert legacy.profit_protection_signal == rec.profit_protection_signal
    assert type(rec.profit_protection_signal) is str
