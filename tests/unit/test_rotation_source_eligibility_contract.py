"""入替元の適格性の導出 `derive_source_eligibility` の契約テスト(Issue #604 P-a。dormant)。

設計: #604 rev2(…6137374981)4 節 + rev3(…6137850710)R2・R3・R4(設計レビュー PASS …6137908828)。

  1  RecommendationType の全メンバーを明示的に分類する(新メンバーで落ちる)。出口は正本を呼ぶ
  2  導出の順序と理由コード(UNKNOWN 9 種・BLOCKED 4 種・ELIGIBLE 1 種)
  3  ★ fail-closed ★: ELIGIBLE は『評価完了・出口の証拠なし』のときだけ(全組合せを oracle で検証)
  4  時間窓(両端を含む)・型の検証・同じ推奨を複数の欄が指すとき 1 件として扱う
  5  純粋性: 時計・I/O・AWS・LINE・services に触れない・どこからも import されない(dormant)
"""

from __future__ import annotations

import ast
import datetime as dt
import itertools
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import (
    BuyAction,
    ConfidenceLevel,
    RecommendationType,
    resolve_holding_summary_action,
)
from jstock_advisor.domain.entities.evaluation_audit import SUMMARY_CATEGORIES
from jstock_advisor.domain.entities.holding_evaluation_record import HoldingEvaluationRecord
from jstock_advisor.domain.signals import rotation_source_eligibility as module
from jstock_advisor.domain.signals.capital_rotation import SourceEligibility
from jstock_advisor.domain.signals.rotation_source_eligibility import (
    HOLDING_CATEGORIES,
    NON_EXIT_CLASSIFICATION,
    EligibilityContractError,
    EligibilityReason,
    EligibilityResult,
    EligibilityWindow,
    HoldingEvaluationFacts,
    RecommendationClass,
    RecommendationFacts,
    classify_recommendation_type,
    derive_source_eligibility,
    facts_from_record,
    recommendation_facts,
)
from jstock_advisor.services.line_notification_service import (
    resolve_attention_origin_for_recommendation,
)
from tests.unit import test_allocation_shadow_compute_contract as compute_tests

_REPO = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO / "src" / "jstock_advisor"
_MODULE_PATH = _PACKAGE / "domain" / "signals" / "rotation_source_eligibility.py"

_START = dt.datetime(2026, 10, 14, 8, 0, tzinfo=dt.UTC)
_END = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.UTC)
_INSIDE = dt.datetime(2026, 10, 14, 8, 5, tzinfo=dt.UTC)
_WINDOW = EligibilityWindow(_START, _END)

R = EligibilityReason
E = SourceEligibility


def facts(**overrides: Any) -> HoldingEvaluationFacts:
    """既定 = ELIGIBLE になる記録(利確が担当・純粋 HOLD・両方の判定が走った・推奨なし)。"""
    values: dict[str, Any] = {
        "evaluated_at": _INSIDE,
        "authoritative_engine": "PROFIT_TAKING",
        "authoritative_outcome_category": "hold",
        "authoritative_recommendation_id": None,
        "legacy_sell_ran": True,
        "legacy_sell_recommendation_id": None,
        "profit_taking_ran": True,
        "profit_taking_recommendation_id": None,
    }
    values.update(overrides)
    return HoldingEvaluationFacts(**values)


def derive(
    record: HoldingEvaluationFacts | None = None,
    recommendations: dict[str, RecommendationFacts] | None = None,
    window: EligibilityWindow = _WINDOW,
) -> EligibilityResult:
    return derive_source_eligibility(
        facts() if record is None else record, recommendations or {}, window
    )


def of_type(recommendation_type: RecommendationType, origin: str | None = None) -> Any:
    return {"r1": RecommendationFacts(recommendation_type.value, origin)}


# =================================================================================================
# 1 分類
# =================================================================================================

_EXPECTED_CLASS = {
    RecommendationType.PARTIAL_PROFIT_TAKE: RecommendationClass.EXIT,
    RecommendationType.FULL_PROFIT_TAKE: RecommendationClass.EXIT,
    RecommendationType.SELL: RecommendationClass.EXIT,
    RecommendationType.SELL_CONSIDERATION: RecommendationClass.EXIT,
    RecommendationType.STRONG_SELL_CONSIDERATION: RecommendationClass.EXIT,
    RecommendationType.PARTIAL_RISK_REDUCTION: RecommendationClass.EXIT,
    RecommendationType.URGENT_REVIEW: RecommendationClass.EXIT,  # 緊急確認 = 正本は CRITICAL
    RecommendationType.URGENT_HOLDING_REVIEW: RecommendationClass.EXIT,
    RecommendationType.REVIEW: RecommendationClass.REVIEW,
    RecommendationType.MANUAL_REVIEW_REQUIRED: RecommendationClass.REVIEW,
    RecommendationType.REVIEW_AFTER_EARNINGS: RecommendationClass.REVIEW,
    RecommendationType.REVIEW_BEFORE_EARNINGS: RecommendationClass.REVIEW,
    RecommendationType.WATCH: RecommendationClass.ATTENTION,
    RecommendationType.WATCH_BEFORE_EARNINGS: RecommendationClass.ATTENTION,
    RecommendationType.HOLD: RecommendationClass.HOLD,
    RecommendationType.BUY: RecommendationClass.NOT_HOLDING_SIDE,
    RecommendationType.WATCH_BUY: RecommendationClass.NOT_HOLDING_SIDE,
    RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW: RecommendationClass.NOT_PER_HOLDING,
}


def test_every_recommendation_type_is_explicitly_classified_exactly_once() -> None:
    """★ 新しいメンバーが増えたら、ここで落ちて分類を決めさせる ★(adapter の作法と同じ)。"""
    assert set(_EXPECTED_CLASS) == set(RecommendationType)
    exits = {t for t in RecommendationType if resolve_holding_summary_action(t) is not None}
    assert exits | set(NON_EXIT_CLASSIFICATION) == set(RecommendationType)
    assert not exits & set(NON_EXIT_CLASSIFICATION)  # 出口は正本が決める。表に重複して持たない
    for member, expected in _EXPECTED_CLASS.items():
        assert classify_recommendation_type(member) is expected, member


def test_the_exit_set_is_decided_by_the_canonical_resolver_and_not_re_implemented() -> None:
    source = _MODULE_PATH.read_text(encoding="utf-8")

    assert "resolve_holding_summary_action(" in source
    # 出口の型を、この module の表に列挙していない(正本だけが出口を決める)
    for member in (
        "PARTIAL_PROFIT_TAKE",
        "FULL_PROFIT_TAKE",
        "SELL_CONSIDERATION",
        "URGENT_REVIEW",
    ):
        assert f"RecommendationType.{member}" not in source, member


def test_a_member_missing_from_the_table_is_unknown_not_eligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分類漏れ(表から HOLD が消えた状況)は、入替元にしない側へ倒れる(例外・ELIGIBLE にしない)。"""
    reduced = {k: v for k, v in NON_EXIT_CLASSIFICATION.items() if k is not RecommendationType.HOLD}
    monkeypatch.setattr(module, "NON_EXIT_CLASSIFICATION", reduced)

    result = derive(facts(authoritative_recommendation_id="r1"), of_type(RecommendationType.HOLD))

    assert result == EligibilityResult(E.UNKNOWN, R.TYPE_UNCLASSIFIED)


# =================================================================================================
# 2 導出の順序と理由コード
# =================================================================================================


def test_the_default_record_is_eligible_with_no_exit_evidence() -> None:
    assert derive() == EligibilityResult(E.ELIGIBLE, R.NO_EXIT_EVIDENCE)


def test_a_missing_record_is_unknown() -> None:
    assert derive_source_eligibility(None, {}, _WINDOW) == EligibilityResult(
        E.UNKNOWN, R.RECORD_MISSING
    )


@pytest.mark.parametrize(
    "moment", [_START - dt.timedelta(seconds=1), _END + dt.timedelta(seconds=1)]
)
def test_a_record_outside_the_window_is_unknown(moment: dt.datetime) -> None:
    """別の日・同日の手動実行の記録を拾わない。"""
    assert derive(facts(evaluated_at=moment)) == EligibilityResult(E.UNKNOWN, R.OUTSIDE_WINDOW)


@pytest.mark.parametrize("moment", [_START, _END])
def test_the_window_includes_both_ends(moment: dt.datetime) -> None:
    assert derive(facts(evaluated_at=moment)).eligibility is E.ELIGIBLE


@pytest.mark.parametrize(
    "category",
    ["", "HOLD", "Hold", "held", "candidate_not_ranked", "near_buy", "passed", "sent ", None],
)
def test_a_category_outside_the_vocabulary_is_unknown_even_when_everything_else_is_clean(
    category: Any,
) -> None:
    value = category if isinstance(category, str) else "x"
    if category is None:
        with pytest.raises(EligibilityContractError):
            facts(authoritative_outcome_category=None)
        return

    assert derive(facts(authoritative_outcome_category=value)) == EligibilityResult(
        E.UNKNOWN, R.CATEGORY_UNKNOWN
    )


def test_the_vocabulary_is_the_six_holding_categories_of_the_summary() -> None:
    assert (
        frozenset({"sent", "hold", "review", "data_insufficient", "suppressed", "failed"})
        == HOLDING_CATEGORIES
    )
    assert set(SUMMARY_CATEGORIES) >= HOLDING_CATEGORIES  # summary_category の語彙の部分集合


@pytest.mark.parametrize("category", ["review", "data_insufficient", "failed"])
def test_an_evaluation_that_did_not_complete_is_unknown(category: str) -> None:
    assert derive(facts(authoritative_outcome_category=category)) == EligibilityResult(
        E.UNKNOWN, R.EVALUATION_NOT_COMPLETED
    )


@pytest.mark.parametrize("engine", [None, "", "legacy_sell", "SOMETHING_NEW"])
def test_a_missing_or_unknown_engine_is_unknown(engine: str | None) -> None:
    assert derive(facts(authoritative_engine=engine)) == EligibilityResult(
        E.UNKNOWN, R.ENGINE_UNKNOWN
    )


def test_the_shadow_engine_is_never_used_as_the_reason_to_be_eligible() -> None:
    result = derive(facts(authoritative_engine="HOLDING_DECISION_SCORE"))

    assert result == EligibilityResult(E.UNKNOWN, R.ENGINE_NOT_AUTHORITATIVE)


@pytest.mark.parametrize("engine", ["LEGACY_SELL", "PROFIT_TAKING"])
def test_either_authoritative_engine_proceeds_to_the_exit_check(engine: str) -> None:
    assert derive(facts(authoritative_engine=engine)).eligibility is E.ELIGIBLE


@pytest.mark.parametrize(
    ("legacy_ran", "profit_ran"), [(False, True), (True, False), (False, False)]
)
def test_a_judgement_that_did_not_run_means_not_evaluated_so_unknown(
    legacy_ran: bool, profit_ran: bool
) -> None:
    """ran = False の判定は『出口なし』と言えない(id が None でも)。両方が走ったときだけ進む。"""
    result = derive(facts(legacy_sell_ran=legacy_ran, profit_taking_ran=profit_ran))

    assert result == EligibilityResult(E.UNKNOWN, R.EVALUATION_NOT_COMPLETED)


def test_ran_with_no_recommendation_id_means_no_exit() -> None:
    """純粋 HOLD の利確は Recommendation を作らない。ran = True かつ id なし = 出口なし。"""
    assert (
        derive(facts(profit_taking_ran=True, profit_taking_recommendation_id=None)).eligibility
        is E.ELIGIBLE
    )


def test_a_pointed_recommendation_that_cannot_be_found_is_unknown() -> None:
    result = derive(facts(profit_taking_recommendation_id="missing"), {})

    assert result == EligibilityResult(E.UNKNOWN, R.RECOMMENDATION_MISSING)


@pytest.mark.parametrize("value", ["", "buy", "SOMETHING_NEW", "hold"])
def test_an_unknown_recommendation_type_value_is_unknown(value: str) -> None:
    result = derive(facts(authoritative_recommendation_id="r1"), {"r1": RecommendationFacts(value)})

    assert result == EligibilityResult(E.UNKNOWN, R.TYPE_UNCLASSIFIED)


@pytest.mark.parametrize(
    "recommendation_type",
    [
        RecommendationType.BUY,
        RecommendationType.WATCH_BUY,
        RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW,
    ],
)
def test_a_type_that_does_not_belong_to_a_holding_is_unknown(
    recommendation_type: RecommendationType,
) -> None:
    result = derive(facts(authoritative_recommendation_id="r1"), of_type(recommendation_type))

    assert result == EligibilityResult(E.UNKNOWN, R.TYPE_UNCLASSIFIED)


_BLOCKED_BY = {
    RecommendationClass.EXIT: R.EXIT_RECOMMENDATION,
    RecommendationClass.REVIEW: R.REVIEW_RECOMMENDATION,
    RecommendationClass.ATTENTION: R.ATTENTION,
}


@pytest.mark.parametrize(
    "recommendation_type",
    [t for t, c in _EXPECTED_CLASS.items() if c in _BLOCKED_BY],
)
@pytest.mark.parametrize("field", ["authoritative", "legacy_sell", "profit_taking"])
def test_every_exit_review_or_attention_type_blocks_whichever_field_points_to_it(
    recommendation_type: RecommendationType, field: str
) -> None:
    record = facts(**{f"{field}_recommendation_id": "r1"})

    result = derive(record, of_type(recommendation_type))

    expected = _BLOCKED_BY[_EXPECTED_CLASS[recommendation_type]]
    assert result == EligibilityResult(E.BLOCKED_BY_EXIT, expected)


def test_a_hold_recommendation_alone_stays_eligible() -> None:
    result = derive(facts(authoritative_recommendation_id="r1"), of_type(RecommendationType.HOLD))

    assert result == EligibilityResult(E.ELIGIBLE, R.NO_EXIT_EVIDENCE)


@pytest.mark.parametrize(
    "origin", ["PROFIT_PROTECTION_CANDIDATE", "PROFIT_PROTECTION_STRONG_NOT_EXECUTABLE"]
)
def test_an_attention_origin_blocks_even_a_hold_type(origin: str) -> None:
    result = derive(
        facts(profit_taking_recommendation_id="r1"), of_type(RecommendationType.HOLD, origin)
    )

    assert result == EligibilityResult(E.BLOCKED_BY_EXIT, R.ATTENTION)


@pytest.mark.parametrize("category", ["sent", "suppressed"])
def test_a_notified_or_suppressed_holding_is_blocked_even_with_no_recommendation(
    category: str,
) -> None:
    """抑止は通知の都合で、判定は出口。『抑止された = HOLD』とみなさない。"""
    assert derive(facts(authoritative_outcome_category=category)) == EligibilityResult(
        E.BLOCKED_BY_EXIT, R.NOTIFIED_OR_SUPPRESSED
    )


def test_unknown_takes_precedence_over_blocked_when_one_evidence_is_missing() -> None:
    record = facts(authoritative_recommendation_id="r1", profit_taking_recommendation_id="gone")

    result = derive(record, of_type(RecommendationType.SELL))

    assert result == EligibilityResult(E.UNKNOWN, R.RECOMMENDATION_MISSING)


def test_the_reason_of_a_block_is_chosen_by_a_fixed_priority() -> None:
    """出口 > 要確認 > 注意 > 通知済み・抑止。ids の並びに依らず決定的。"""
    record = facts(
        authoritative_outcome_category="sent",
        authoritative_recommendation_id="a",
        legacy_sell_recommendation_id="b",
        profit_taking_recommendation_id="c",
    )
    recommendations = {
        "a": RecommendationFacts("WATCH"),
        "b": RecommendationFacts("REVIEW"),
        "c": RecommendationFacts("SELL"),
    }

    assert derive(record, recommendations).reason is R.EXIT_RECOMMENDATION
    del recommendations["c"]
    record = facts(
        authoritative_outcome_category="sent",
        authoritative_recommendation_id="a",
        legacy_sell_recommendation_id="b",
    )
    assert derive(record, recommendations).reason is R.REVIEW_RECOMMENDATION
    del recommendations["b"]
    record = facts(authoritative_outcome_category="sent", authoritative_recommendation_id="a")
    assert derive(record, recommendations).reason is R.ATTENTION
    assert derive(facts(authoritative_outcome_category="sent")).reason is R.NOTIFIED_OR_SUPPRESSED


def test_the_same_recommendation_pointed_by_several_fields_counts_once() -> None:
    record = facts(
        authoritative_recommendation_id="r1",
        legacy_sell_recommendation_id="r1",
        profit_taking_recommendation_id="r1",
    )

    assert record.recommendation_ids() == ("r1",)
    assert derive(record, of_type(RecommendationType.HOLD)).eligibility is E.ELIGIBLE


# =================================================================================================
# 3 ★ fail-closed ★ 全組合せを、独立の oracle と突き合わせる
# =================================================================================================

_CATEGORIES = [*sorted(HOLDING_CATEGORIES), "bogus"]
_ENGINES = ["LEGACY_SELL", "PROFIT_TAKING", "HOLDING_DECISION_SCORE", None, "bogus"]
_RAN = [(True, True), (True, False), (False, True), (False, False)]
_TYPES = [*[t.value for t in RecommendationType], "bogus", None]  # None = 推奨なし
_ORIGINS = [None, "PROFIT_PROTECTION_CANDIDATE"]


def _oracle(
    category: str,
    engine: str | None,
    ran: tuple[bool, bool],
    recommendation_type: str | None,
    origin: str | None,
) -> bool:
    """『ELIGIBLE になってよい』の条件を、設計の文章から直接書いた(module の実装を呼ばない)。"""
    if category != "hold":
        return False
    if engine not in ("LEGACY_SELL", "PROFIT_TAKING"):
        return False
    if not (ran[0] and ran[1]):
        return False
    if origin is not None:
        return False
    return recommendation_type in (None, "HOLD")


def test_eligible_exactly_when_the_independent_oracle_says_so_over_every_combination() -> None:
    combos = list(itertools.product(_CATEGORIES, _ENGINES, _RAN, _TYPES, _ORIGINS))
    eligible = 0
    for category, engine, ran, recommendation_type, origin in combos:
        if recommendation_type is None and origin is not None:
            continue  # 推奨が無いのに origin だけがある組は作れない(origin は推奨の事実)
        record = facts(
            authoritative_outcome_category=category,
            authoritative_engine=engine,
            legacy_sell_ran=ran[0],
            profit_taking_ran=ran[1],
            profit_taking_recommendation_id=None if recommendation_type is None else "r1",
        )
        recommendations = (
            {}
            if recommendation_type is None
            else {"r1": RecommendationFacts(recommendation_type, origin)}
        )
        result = derive(record, recommendations)
        expected = _oracle(category, engine, ran, recommendation_type, origin)
        assert (result.eligibility is E.ELIGIBLE) is expected, (
            category,
            engine,
            ran,
            recommendation_type,
            origin,
            result,
        )
        eligible += expected
    assert len(combos) > 1500
    assert 0 < eligible < len(combos) // 20  # ELIGIBLE は狭い(空振りでも・広すぎでもない)


def test_no_exit_or_review_or_attention_type_is_ever_eligible_in_any_combination() -> None:
    not_eligible_types = [
        t.value for t, c in _EXPECTED_CLASS.items() if c is not RecommendationClass.HOLD
    ]
    for category, engine, ran, recommendation_type in itertools.product(
        _CATEGORIES, _ENGINES, _RAN, not_eligible_types
    ):
        record = facts(
            authoritative_outcome_category=category,
            authoritative_engine=engine,
            legacy_sell_ran=ran[0],
            profit_taking_ran=ran[1],
            authoritative_recommendation_id="r1",
        )
        result = derive(record, {"r1": RecommendationFacts(recommendation_type)})
        assert result.eligibility is not E.ELIGIBLE, (category, engine, ran, recommendation_type)


def test_a_sent_or_suppressed_holding_is_never_eligible_in_any_combination() -> None:
    for category, engine, ran, recommendation_type in itertools.product(
        ["sent", "suppressed"], _ENGINES, _RAN, [*_TYPES]
    ):
        record = facts(
            authoritative_outcome_category=category,
            authoritative_engine=engine,
            legacy_sell_ran=ran[0],
            profit_taking_ran=ran[1],
            profit_taking_recommendation_id=None if recommendation_type is None else "r1",
        )
        recommendations = (
            {} if recommendation_type is None else {"r1": RecommendationFacts(recommendation_type)}
        )
        assert derive(record, recommendations).eligibility is not E.ELIGIBLE


def test_every_result_carries_a_reason_that_matches_its_eligibility() -> None:
    unknown = {
        R.RECORD_MISSING, R.OUTSIDE_WINDOW, R.CATEGORY_UNKNOWN, R.EVALUATION_NOT_COMPLETED,
        R.ENGINE_UNKNOWN, R.ENGINE_NOT_AUTHORITATIVE, R.RECOMMENDATION_MISSING, R.TYPE_UNCLASSIFIED,
    }  # fmt: skip
    blocked = {
        R.EXIT_RECOMMENDATION,
        R.REVIEW_RECOMMENDATION,
        R.ATTENTION,
        R.NOTIFIED_OR_SUPPRESSED,
    }
    assert unknown | blocked | {R.NO_EXIT_EVIDENCE} == set(EligibilityReason)
    for category, engine, ran, recommendation_type in itertools.product(
        _CATEGORIES, _ENGINES, _RAN, _TYPES
    ):
        record = facts(
            authoritative_outcome_category=category,
            authoritative_engine=engine,
            legacy_sell_ran=ran[0],
            profit_taking_ran=ran[1],
            profit_taking_recommendation_id=None if recommendation_type is None else "r1",
        )
        recommendations = (
            {} if recommendation_type is None else {"r1": RecommendationFacts(recommendation_type)}
        )
        result = derive(record, recommendations)
        allowed = {E.UNKNOWN: unknown, E.BLOCKED_BY_EXIT: blocked, E.ELIGIBLE: {R.NO_EXIT_EVIDENCE}}
        assert result.reason in allowed[result.eligibility], result


def test_the_derivation_is_deterministic_and_does_not_mutate_its_inputs() -> None:
    record = facts(profit_taking_recommendation_id="r1")
    recommendations = of_type(RecommendationType.HOLD)
    before = (record, dict(recommendations))

    first = derive(record, recommendations)
    second = derive(record, recommendations)

    assert first == second
    assert (record, recommendations) == before


# =================================================================================================
# 4 入力の型・時間窓
# =================================================================================================


def test_the_window_must_be_aware_and_ordered() -> None:
    with pytest.raises(EligibilityContractError):
        EligibilityWindow(dt.datetime(2026, 10, 14, 8, 0), _END)
    with pytest.raises(EligibilityContractError):
        EligibilityWindow(_START, dt.datetime(2026, 10, 14, 9, 0))
    with pytest.raises(EligibilityContractError):
        EligibilityWindow(_END, _START)
    EligibilityWindow(_START, _START)  # 幅 0 は可


@pytest.mark.parametrize(
    "overrides",
    [
        {"evaluated_at": dt.datetime(2026, 10, 14, 8, 5)},  # naive
        {"evaluated_at": "2026-10-14T08:05:00+00:00"},
        {"legacy_sell_ran": 1},
        {"profit_taking_ran": "yes"},
        {"authoritative_engine": 7},
        {"authoritative_outcome_category": 7},
        {"profit_taking_recommendation_id": ""},
        {"authoritative_recommendation_id": 5},
    ],
)
def test_malformed_facts_are_a_contract_error_not_a_silent_default(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(EligibilityContractError) as caught:
        facts(**overrides)
    assert "2026" not in str(caught.value)  # 値を含めない


@pytest.mark.parametrize(
    "arguments",
    [{"recommendation_type": 5}, {"recommendation_type": "HOLD", "attention_origin": 3}],
)
def test_malformed_recommendation_facts_are_a_contract_error(arguments: dict[str, Any]) -> None:
    with pytest.raises(EligibilityContractError):
        RecommendationFacts(**arguments)


# =================================================================================================
# 5 記録・Recommendation からの写し(正本と同値)
# =================================================================================================


def test_facts_from_a_persisted_record_copy_the_fields_without_changing_them() -> None:
    record = HoldingEvaluationRecord(
        holding_evaluation_id="h:1",
        holding_id="owner-a#0001",
        owner="owner-a",
        stock_code="0001",
        evaluated_at=_INSIDE,
        rule_version="v1",
        authoritative_outcome_category="hold",
        authoritative_engine="LEGACY_SELL",
        authoritative_recommendation_id="ra",
        legacy_sell_ran=True,
        legacy_sell_recommendation_id="rb",
        profit_taking_ran=False,  # 2 つの ran を取り違えない(違う値で写す)
        profit_taking_recommendation_id="rc",
    )

    copied = facts_from_record(record)

    assert copied == HoldingEvaluationFacts(
        evaluated_at=_INSIDE,
        authoritative_engine="LEGACY_SELL",
        authoritative_outcome_category="hold",
        authoritative_recommendation_id="ra",
        legacy_sell_ran=True,
        legacy_sell_recommendation_id="rb",
        profit_taking_ran=False,  # 2 つの ran を取り違えない(違う値で写す)
        profit_taking_recommendation_id="rc",
    )
    assert "owner-a" not in repr(copied) and "0001" not in repr(copied)  # 識別情報を持たない


def test_the_recomputed_attention_origin_equals_the_canonical_resolver() -> None:
    """★ 正本との同値 ★ 注入した正本の resolver の結果をそのまま写す(別の再実装ではない)。"""
    for signal in (None, "CANDIDATE", "STRONG", "DATA_INSUFFICIENT"):
        for recommendation_type in (RecommendationType.WATCH, RecommendationType.HOLD):
            recommendation = compute_tests.rec(
                "0001",
                recommendation_type=recommendation_type,
                buy_action=None,
                profit_protection_signal=signal,
            )

            written = recommendation_facts(
                recommendation, resolve_attention_origin_for_recommendation
            )

            assert written.attention_origin == resolve_attention_origin_for_recommendation(
                recommendation
            )
            assert written.recommendation_type == recommendation_type.value


def test_a_profit_protection_watch_is_blocked_through_the_recomputed_origin() -> None:
    recommendation = compute_tests.rec(
        "0001",
        recommendation_type=RecommendationType.WATCH,
        buy_action=None,
        profit_protection_signal="STRONG",
    )
    written = recommendation_facts(recommendation, resolve_attention_origin_for_recommendation)

    result = derive(facts(profit_taking_recommendation_id="r1"), {"r1": written})

    assert result.eligibility is E.BLOCKED_BY_EXIT
    assert written.attention_origin == "PROFIT_PROTECTION_STRONG_NOT_EXECUTABLE"


def test_a_recommendation_without_an_attention_origin_gets_none() -> None:
    recommendation = compute_tests.rec(
        "0001", recommendation_type=RecommendationType.HOLD, buy_action=None
    )

    written = recommendation_facts(recommendation, resolve_attention_origin_for_recommendation)

    assert written == RecommendationFacts("HOLD", None)
    assert BuyAction.BUY and ConfidenceLevel.HIGH  # 型の import を保つ(上の fixture が使う)


# =================================================================================================
# 6 純粋性(AST)
# =================================================================================================


def _tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def test_the_module_imports_only_the_standard_library_and_the_domain() -> None:
    imported = []
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]

    allowed_prefixes = ("jstock_advisor.domain.",)
    for name in imported:
        assert name.split(".")[0] in {
            "datetime",
            "collections",
            "dataclasses",
            "enum",
            "typing",
            "__future__",
            "jstock_advisor",
        }, name
        if name.startswith("jstock_advisor"):
            assert name.startswith(allowed_prefixes), name
    # LINE の送信 module・services・infrastructure・config へ到達しない
    for forbidden in ("services", "infrastructure", "line_notification", "boto", "config"):
        assert not [m for m in imported if forbidden in m.lower()], forbidden


def test_the_module_reads_no_clock_and_does_no_io() -> None:
    called = {
        node.func.attr
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    names = {node.id for node in ast.walk(_tree()) if isinstance(node, ast.Name)}

    assert not called & {"now", "utcnow", "today", "time", "sleep", "open", "read_text", "getenv"}
    assert not names & {"print", "open", "input", "logger", "logging"}


def test_nothing_in_src_imports_the_module_yet() -> None:
    """dormant: P の入力の組み立て(P-b)が入るまで、どこからも import されない。"""
    importers = [
        path.relative_to(_PACKAGE).as_posix()
        for path in sorted(_PACKAGE.rglob("*.py"))
        if path != _MODULE_PATH
        and "rotation_source_eligibility" in path.read_text(encoding="utf-8")
    ]

    assert importers == []
