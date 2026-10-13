"""Issue #602 PR-G1: RAER 入力の組み立て(assembler)の契約テスト。

固定するもの(設計 #602 issuecomment-6138720448)
  (1) 土台: benefit_state の逆算の全分岐・fair_value_usable = None・利回りの矛盾
  (2) 軸ごとの導出と『導出不能 → 代替値なし』(企業品質・リスク・営業トレンド)
  (3) 軸ごとの失敗は他の軸を巻き込まない・例外を投げない(壊れた入力で fuzz)
  (4) comparison_key(全 4 軸 = 定数)・end-to-end
  (5) 生産側との契約(同型 sweep): フィールド名・辞書のキー・状態値が生産側と一致する
  (6) 構造: float 演算なし・時計なし・どこからも import されない(dormant)
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import random
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from jstock_advisor.config.models import EarningsTrendRulesConfig
from jstock_advisor.domain.entities.common import MarginAdjustment
from jstock_advisor.domain.entities.earnings_trend import EarningsTrendResult
from jstock_advisor.domain.entities.enums import (
    BuyIndustrySector,
    ConfidenceLevel,
    EarningsTrendEvaluationState,
    MarginRiskCategory,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.price_freshness import PriceFreshnessVerdict
from jstock_advisor.domain.signals.earnings_trend import earnings_trend_result_to_metrics
from jstock_advisor.domain.valuation import raer_inputs as ri
from jstock_advisor.domain.valuation.expected_return import (
    IncomeInput,
    Note,
    ReasonCode,
    compute_income,
)
from jstock_advisor.domain.valuation.raer import (
    COMBINED_MODEL_VERSION,
    AxisCode,
    AxisReasonCode,
    AxisStatus,
    RaerState,
    RiskFlag,
    make_comparison_key,
)
from jstock_advisor.domain.valuation.raer_composition import (
    OperatingTrendScore,
    RaerCoefficients,
    TrendComponent,
    TrendUnavailable,
    TrendWeights,
)
from jstock_advisor.domain.valuation.raer_inputs import (
    RAER_FULL_COMPARISON_KEY,
    RISK_DETECTION_FACTS_KEY,
    AssemblerPolicy,
    CandidateRaerFacts,
    HoldingMarketView,
    ProvenanceItem,
    ProvenanceNote,
    ProvenanceReason,
    RiskFlagSource,
    RiskFlagView,
    assemble_raer_inputs,
    facts_from_recommendation,
    holding_market_view,
    holding_market_view_from_recommendation,
    raer_result_for_candidate,
    risk_flag_view,
    risk_flag_view_from_recommendation,
)
from jstock_advisor.domain.valuation.yield_calc import BenefitProgramState
from jstock_advisor.services.buy_signal_service import SHADOW_STATE_COMPUTED
from tests.factories import build_recommendation

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "jstock_advisor"
_MODULE_PATH = _SRC / "domain" / "valuation" / "raer_inputs.py"
_DATE = date(2026, 10, 13)

D = Decimal
CL = ConfidenceLevel
ES = EarningsTrendEvaluationState
A = AxisCode
PI = ProvenanceItem
PR = ProvenanceReason
V = PriceFreshnessVerdict


def _coefficients() -> RaerCoefficients:
    return RaerCoefficients(
        fv_confidence_factor={CL.HIGH: D("1.00"), CL.MEDIUM: D("0.85"), CL.LOW: D("0.60")},
        quality_deduction_max=D(4),
        quality_max_score=D(50),
        risk_deductions={
            RiskFlag.EARNINGS_WITHIN_3_BUSINESS_DAYS: D("3.0"),
            RiskFlag.EARNINGS_WITHIN_7_BUSINESS_DAYS: D("1.5"),
            RiskFlag.CYCLICAL_INDUSTRY: D("1.5"),
            RiskFlag.MAJOR_CUSTOMER_DEPENDENCY: D("1.5"),
            RiskFlag.TEMPORARY_EARNINGS_BOOST_RISK: D("2.0"),
            RiskFlag.SMALL_CAP_OR_LOW_LIQUIDITY: D("2.0"),
        },
        risk_deduction_cap=D(6),
        trend_deduction_scale=D(3),
        min_applied_axes=3,
        required_axes=frozenset({A.FAIR_VALUE_CONFIDENCE}),
    )


_POLICY = AssemblerPolicy(
    company_quality_minimum=D("0.6"),
    trend_weights=TrendWeights(D("1.0"), D("0.75"), D("0.5"), D("0.25")),
    trend_min_coverage_required=D("0.3"),
    trend_model_version="earnings_trend_v3",
)


def _common_quality(**overrides: Any) -> dict[str, object]:
    values: dict[str, object] = {
        "shadow_state": SHADOW_STATE_COMPUTED,
        "score": 30.0,
        "coverage_ratio": 0.8,
    }
    values.update(overrides)
    return values


def _trend_metrics(**overrides: Any) -> dict[str, object]:
    values: dict[str, object] = {
        "state": "EVALUATED",
        "operating_income_trend_component": 37.5,
        "operating_cashflow_trend_component": -20.0,
    }
    values.update(overrides)
    return values


def _facts(**overrides: Any) -> CandidateRaerFacts:
    values: dict[str, Any] = {
        "price": D("1000"),
        "price_as_of_date": date(2026, 10, 13),
        "buy_industry_sector": BuyIndustrySector.GENERAL,
        "fair_value_neutral": D("1059"),
        "fair_value_confidence": CL.MEDIUM,
        "fair_value_usable": True,
        "fair_value_unusable_reason": None,
        "dividend_yield_pct": 3.0,
        "benefit_yield_pct": None,
        "total_yield_pct": 3.0,
        "margin_computed": True,
        "margin_adjustment_codes": (),
        "margin_superseded_codes": (),
        "risk_detection": None,
        "common_quality": _common_quality(),
        "earnings_trend_metrics": _trend_metrics(),
    }
    values.update(overrides)
    return CandidateRaerFacts(**values)


def _assemble(facts: CandidateRaerFacts | None = None, **kwargs: Any) -> ri.AssembledRaerInputs:
    return assemble_raer_inputs(
        facts or _facts(),
        evaluation_date=kwargs.pop("evaluation_date", _DATE),
        price_freshness=kwargs.pop("price_freshness", V.NORMAL),
        policy=kwargs.pop("policy", _POLICY),
    )


# =================================================================================================
# (4) 比較の鍵・end-to-end
# =================================================================================================


def test_the_full_comparison_key_is_the_four_implemented_axes_and_the_combined_model_version() -> (
    None
):
    expected = (
        "raer-combined-v1-provisional-axes-y|"
        "BUSINESS_RISK,COMPANY_QUALITY,EARNINGS_TREND,FAIR_VALUE_CONFIDENCE"
    )
    assert expected == RAER_FULL_COMPARISON_KEY
    assert COMBINED_MODEL_VERSION in RAER_FULL_COMPARISON_KEY
    assert "DATA_QUALITY" not in RAER_FULL_COMPARISON_KEY  # 軸 4 は未実装


def test_end_to_end_the_four_axes_give_the_hand_calculated_value_and_the_full_key() -> None:
    result, evaluation, provenance = raer_result_for_candidate(
        _facts(),
        evaluation_date=_DATE,
        price_freshness=V.NORMAL,
        policy=_POLICY,
        coefficients=_coefficients(),
    )
    # U = (1059/1000 - 1) x 100 = 5.9 / I = 3.0 / U' = 5.9 x 0.85 = 5.015
    # d2 = 4 x (1 - 30/50) = 1.6 / d3 = 0(リスクなしを確認済み)
    # s = (37.5 x 1.0 + (-20) x 0.75) / 1.75 = 12.857143 → 悪化なし d5 = 0
    assert result.state is RaerState.PROVISIONAL
    assert result.value == D("6.415")
    assert result.comparison_key == RAER_FULL_COMPARISON_KEY
    assert evaluation.composition is not None
    assert all(entry.derived for entry in provenance.entries)
    assert {entry.item for entry in provenance.entries} == set(ProvenanceItem)


def test_three_applied_axes_give_a_different_key_so_they_are_not_compared_with_four() -> None:
    result, _, provenance = raer_result_for_candidate(
        _facts(earnings_trend_metrics=_trend_metrics(state="NOT_EVALUATED")),
        evaluation_date=_DATE,
        price_freshness=V.NORMAL,
        policy=_POLICY,
        coefficients=_coefficients(),
    )
    assert result.state is RaerState.PROVISIONAL
    assert result.comparison_key is not None
    assert result.comparison_key != RAER_FULL_COMPARISON_KEY
    assert result.comparison_key == make_comparison_key(
        COMBINED_MODEL_VERSION,
        (A.FAIR_VALUE_CONFIDENCE, A.COMPANY_QUALITY, A.BUSINESS_RISK),
    )
    entry = provenance.entry(PI.EARNINGS_TREND)
    assert not entry.derived
    assert entry.reason is PR.SOURCE_NOT_EVALUATED


# =================================================================================================
# (1) 土台: U / I / 軸 1
# =================================================================================================


def test_the_upside_uses_the_existing_computation_and_the_fair_value_confidence_is_carried() -> (
    None
):
    assembled = _assemble()
    er = assembled.inputs.expected_return
    assert er.upside.value == pytest.approx(5.9)
    assert er.income.value == 3.0
    assert er.fair_value_confidence is CL.MEDIUM
    assert er.revision.reasons == (ReasonCode.REVISION_NOT_IMPLEMENTED,)


def test_an_unknown_fair_value_usability_is_never_assumed_usable() -> None:
    assembled = _assemble(_facts(fair_value_usable=None))
    er = assembled.inputs.expected_return
    assert er.upside.value is None
    assert ReasonCode.FAIR_VALUE_UNAVAILABLE in er.upside.reasons
    entry = assembled.provenance.entry(PI.UPSIDE)
    assert not entry.derived
    assert entry.reason is PR.FV_USABILITY_UNKNOWN


def test_an_unusable_fair_value_keeps_the_existing_reason_and_makes_no_upside() -> None:
    assembled = _assemble(
        _facts(fair_value_usable=False, fair_value_unusable_reason="TOO_FEW_METHODS")
    )
    er = assembled.inputs.expected_return
    assert er.upside.value is None
    assert ReasonCode.FAIR_VALUE_NOT_USABLE in er.upside.reasons
    assert "TOO_FEW_METHODS" in er.upside.detail


def test_an_unknown_unusable_reason_word_is_not_invented_into_the_vocabulary() -> None:
    assembled = _assemble(
        _facts(fair_value_usable=False, fair_value_unusable_reason="SOMETHING_NEW")
    )
    er = assembled.inputs.expected_return
    assert ReasonCode.FAIR_VALUE_NOT_USABLE in er.upside.reasons
    assert "SOMETHING_NEW" not in er.upside.detail


@pytest.mark.parametrize("verdict", [V.HARD_STOP, V.DATA_INSUFFICIENT])
def test_a_stale_price_makes_no_upside(verdict: V) -> None:
    er = _assemble(price_freshness=verdict).inputs.expected_return
    assert er.upside.value is None
    assert ReasonCode.CURRENT_PRICE_STALE in er.upside.reasons


def test_a_price_warning_still_computes_and_is_noted() -> None:
    er = _assemble(price_freshness=V.WARNING).inputs.expected_return
    assert er.upside.value is not None
    assert Note.PRICE_FRESHNESS_WARNING in er.notes


def test_a_missing_price_makes_no_upside() -> None:
    er = _assemble(_facts(price=None)).inputs.expected_return
    assert ReasonCode.CURRENT_PRICE_UNAVAILABLE in er.upside.reasons


def test_a_missing_fair_value_confidence_leaves_axis_one_unavailable_and_the_raer_unavailable() -> (
    None
):
    result, _, provenance = raer_result_for_candidate(
        _facts(fair_value_confidence=None),
        evaluation_date=_DATE,
        price_freshness=V.NORMAL,
        policy=_POLICY,
        coefficients=_coefficients(),
    )
    assert result.state is RaerState.UNAVAILABLE
    assert result.value is None
    assert not provenance.entry(PI.FAIR_VALUE_CONFIDENCE).derived


# ---- 優待の状態の逆算(#55 の真理値表 A〜H)-----------------------------------------------------


@pytest.mark.parametrize(
    ("dividend", "benefit", "total", "income", "note", "reason"),
    [
        # A: 配当あり・制度なし → 配当のみ
        (3.0, None, 3.0, 3.0, ProvenanceNote.BENEFIT_STATE_DERIVED_FROM_YIELDS, None),
        # G: 配当 0 を明示・制度なし → 0(信頼できる 0)
        (0.0, None, 0.0, 0.0, ProvenanceNote.BENEFIT_STATE_DERIVED_FROM_YIELDS, None),
        # B / H: 配当あり・評価可能 → 配当 + 優待
        (3.0, 0.5, 3.5, 3.5, ProvenanceNote.BENEFIT_STATE_DERIVED_FROM_YIELDS, None),
        (0.0, 0.5, 0.5, 0.5, ProvenanceNote.BENEFIT_STATE_DERIVED_FROM_YIELDS, None),
        # C: 配当あり・評価不能 → 値なし
        (
            3.0,
            None,
            None,
            None,
            ProvenanceNote.BENEFIT_STATE_DERIVED_FROM_YIELDS,
            PR.SOURCE_MISSING,
        ),
        # D / E / F: 配当が不明 → 値なし(配当の None から 0 を推測しない)
        (None, None, None, None, ProvenanceNote.BENEFIT_STATE_NOT_DERIVABLE, PR.SOURCE_MISSING),
        (
            None,
            0.5,
            None,
            None,
            ProvenanceNote.BENEFIT_STATE_DERIVED_FROM_YIELDS,
            PR.SOURCE_MISSING,
        ),
        # 突き合わせの不一致(再計算した総合利回りが永続値と違う)→ 値を作らない
        # ・総合があるのに配当が無い
        (
            None,
            None,
            3.0,
            None,
            ProvenanceNote.BENEFIT_STATE_DERIVED_FROM_YIELDS,
            PR.TOTAL_YIELD_MISMATCH,
        ),
        # ・★ 優待が総合に入っていたのに、優待利回りが永続されていない(黙って過小にしない)
        (
            3.0,
            None,
            3.5,
            None,
            ProvenanceNote.BENEFIT_STATE_DERIVED_FROM_YIELDS,
            PR.TOTAL_YIELD_MISMATCH,
        ),
        # ・配当と優待が揃っているのに総合が欠測
        (
            3.0,
            0.5,
            None,
            None,
            ProvenanceNote.BENEFIT_STATE_DERIVED_FROM_YIELDS,
            PR.TOTAL_YIELD_MISMATCH,
        ),
        # ・総合が配当 + 優待と違う
        (
            3.0,
            0.5,
            3.4,
            None,
            ProvenanceNote.BENEFIT_STATE_DERIVED_FROM_YIELDS,
            PR.TOTAL_YIELD_MISMATCH,
        ),
        # ・優待があるのに配当が不明で、総合だけある
        (
            None,
            0.5,
            3.5,
            None,
            ProvenanceNote.BENEFIT_STATE_DERIVED_FROM_YIELDS,
            PR.TOTAL_YIELD_MISMATCH,
        ),
    ],
)
def test_the_benefit_state_is_read_back_from_the_persisted_yields(
    dividend: float | None,
    benefit: float | None,
    total: float | None,
    income: float | None,
    note: ProvenanceNote,
    reason: ProvenanceReason | None,
) -> None:
    assembled = _assemble(
        _facts(dividend_yield_pct=dividend, benefit_yield_pct=benefit, total_yield_pct=total)
    )
    result = assembled.inputs.expected_return.income
    entry = assembled.provenance.entry(PI.INCOME)
    assert result.value == income
    assert entry.derived is (income is not None)
    assert entry.reason is reason
    assert note in entry.notes


def test_a_total_yield_mismatch_gives_no_income_and_a_generic_reason_with_a_marker() -> None:
    result = _assemble(
        _facts(dividend_yield_pct=3.0, benefit_yield_pct=None, total_yield_pct=3.5)
    ).inputs.expected_return.income
    assert result.value is None
    assert result.reasons == (ReasonCode.DIVIDEND_UNKNOWN,)
    assert result.detail == "yield_record_mismatch"


def test_the_total_yield_comparison_tolerance_is_one_fixed_constant_and_inclusive() -> None:
    tolerance = ri.YIELD_RECORD_TOLERANCE
    assert tolerance == 1e-9
    within = _assemble(
        _facts(dividend_yield_pct=3.0, benefit_yield_pct=0.5, total_yield_pct=3.5 + 5e-10)
    )
    assert within.inputs.expected_return.income.value is not None
    outside = _assemble(
        _facts(dividend_yield_pct=3.0, benefit_yield_pct=0.5, total_yield_pct=3.5 + 1e-8)
    )
    assert outside.inputs.expected_return.income.value is None
    assert outside.provenance.entry(PI.INCOME).reason is PR.TOTAL_YIELD_MISMATCH


def test_the_comparison_runs_for_every_derived_state_not_only_for_the_valued_branch() -> None:
    # 優待あり / 制度なし / 評価不能 / 不明のどの導出でも、永続値とずれれば値を作らない
    for dividend, benefit, total in [
        (3.0, 0.5, 3.6),  # VALUED
        (3.0, None, 3.1),  # NO_PROGRAM
        (3.0, None, 0.0),  # NO_PROGRAM(0 は欠測ではない)
        (None, None, 0.0),  # 配当が不明
    ]:
        income = _assemble(
            _facts(dividend_yield_pct=dividend, benefit_yield_pct=benefit, total_yield_pct=total)
        ).inputs.expected_return.income
        assert income.value is None, (dividend, benefit, total)


def test_an_unvaluable_and_a_valued_without_yield_give_the_same_income_result() -> None:
    # 逆算は『出力同値』であり、状態の復元ではない。2 つの状態が同じ結果を返す前提を固定する
    # (将来この 2 つの扱いが分かれたら、このテストが落ちて逆算の見直しに気づける)
    unvaluable = compute_income(IncomeInput(3.0, None, BenefitProgramState.UNVALUABLE))
    valued_without_yield = compute_income(IncomeInput(3.0, None, BenefitProgramState.VALUED))
    assert unvaluable == valued_without_yield
    assert unvaluable.value is None
    assert unvaluable.reasons == (ReasonCode.BENEFIT_UNVALUABLE,)


def test_an_unvaluable_benefit_is_reported_and_the_income_is_not_summed_without_it() -> None:
    result = _assemble(
        _facts(dividend_yield_pct=3.0, benefit_yield_pct=None, total_yield_pct=None)
    ).inputs.expected_return.income
    assert result.value is None
    assert ReasonCode.BENEFIT_UNVALUABLE in result.reasons


def test_a_missing_dividend_is_never_read_as_zero() -> None:
    result = _assemble(
        _facts(dividend_yield_pct=None, benefit_yield_pct=None, total_yield_pct=None)
    ).inputs.expected_return.income
    assert result.value is None
    assert ReasonCode.DIVIDEND_UNKNOWN in result.reasons


# =================================================================================================
# (2) 軸 2 企業品質
# =================================================================================================


def test_the_quality_is_taken_as_is_from_the_shared_zero_to_fifty_score_without_halving() -> None:
    quality = _assemble(_facts(common_quality=_common_quality(score=42.0))).inputs.quality
    assert quality is not None
    assert quality.holding_quality_score_0_50 == D("42.0")
    assert quality.coverage_ratio == D("0.8")
    assert quality.min_coverage == D("0.6")


@pytest.mark.parametrize("score", [0, 0.0, 50, 50.0])
def test_the_score_boundaries_zero_and_fifty_are_accepted(score: float) -> None:
    quality = _assemble(_facts(common_quality=_common_quality(score=score))).inputs.quality
    assert quality is not None


@pytest.mark.parametrize("score", [-0.01, 50.01, 100.0, 85.0])
def test_a_score_outside_zero_to_fifty_is_never_rescaled_or_clamped(score: float) -> None:
    # 100 点満点の v1 の値が紛れ込んでも 2 で割らず、軸を使わない
    assembled = _assemble(_facts(common_quality=_common_quality(score=score)))
    assert assembled.inputs.quality is None
    entry = assembled.provenance.entry(PI.COMPANY_QUALITY)
    assert not entry.derived
    assert entry.reason is PR.INVALID_VALUE


@pytest.mark.parametrize("coverage", [-0.1, 1.01, 5])
def test_a_coverage_outside_zero_to_one_is_invalid(coverage: float) -> None:
    assembled = _assemble(_facts(common_quality=_common_quality(coverage_ratio=coverage)))
    assert assembled.inputs.quality is None
    assert assembled.provenance.entry(PI.COMPANY_QUALITY).reason is PR.INVALID_VALUE


@pytest.mark.parametrize("bad", [True, "30", None, float("nan"), float("inf")], ids=repr)
def test_a_non_numeric_score_is_invalid_and_never_becomes_zero(bad: object) -> None:
    assembled = _assemble(_facts(common_quality=_common_quality(score=bad)))
    assert assembled.inputs.quality is None
    assert assembled.provenance.entry(PI.COMPANY_QUALITY).reason is PR.INVALID_VALUE


def test_a_missing_coverage_is_never_assumed_to_be_one() -> None:
    values = _common_quality()
    del values["coverage_ratio"]
    assembled = _assemble(_facts(common_quality=values))
    assert assembled.inputs.quality is None
    assert assembled.provenance.entry(PI.COMPANY_QUALITY).reason is PR.INVALID_VALUE


def test_a_missing_observation_is_missing_and_a_failed_one_is_reported_as_failed() -> None:
    missing = _assemble(_facts(common_quality=None))
    assert missing.inputs.quality is None
    assert missing.provenance.entry(PI.COMPANY_QUALITY).reason is PR.SOURCE_MISSING
    failed = _assemble(
        _facts(common_quality={"shadow_state": "COMPUTATION_FAILED", "error_type": "KeyError"})
    )
    assert failed.inputs.quality is None
    assert failed.provenance.entry(PI.COMPANY_QUALITY).reason is PR.SOURCE_COMPUTATION_FAILED
    no_state = _assemble(_facts(common_quality={"score": 30.0, "coverage_ratio": 0.8}))
    assert no_state.inputs.quality is None
    assert no_state.provenance.entry(PI.COMPANY_QUALITY).reason is PR.SOURCE_MISSING


def test_a_computed_state_is_required_even_when_the_numbers_are_present() -> None:
    assembled = _assemble(_facts(common_quality=_common_quality(shadow_state="COMPUTATION_FAILED")))
    assert assembled.inputs.quality is None


def test_the_coverage_below_the_existing_minimum_is_kept_and_the_axis_is_unavailable() -> None:
    low = _assemble(_facts(common_quality=_common_quality(coverage_ratio=0.59)))
    assert low.inputs.quality is not None
    _, evaluation, _ = raer_result_for_candidate(
        _facts(common_quality=_common_quality(coverage_ratio=0.59)),
        evaluation_date=_DATE,
        price_freshness=V.NORMAL,
        policy=_POLICY,
        coefficients=_coefficients(),
    )
    quality_axis = next(a for a in evaluation.axes if a.axis is A.COMPANY_QUALITY)
    assert quality_axis.status is AxisStatus.UNAVAILABLE
    assert quality_axis.reasons == (AxisReasonCode.COVERAGE_BELOW_MINIMUM,)


def test_the_coverage_exactly_at_the_minimum_is_applied() -> None:
    _, evaluation, _ = raer_result_for_candidate(
        _facts(common_quality=_common_quality(coverage_ratio=0.6)),
        evaluation_date=_DATE,
        price_freshness=V.NORMAL,
        policy=_POLICY,
        coefficients=_coefficients(),
    )
    quality_axis = next(a for a in evaluation.axes if a.axis is A.COMPANY_QUALITY)
    assert quality_axis.status is AxisStatus.APPLIED


def test_the_minimum_coverage_comes_from_the_policy_not_from_the_assembler() -> None:
    strict = AssemblerPolicy(
        company_quality_minimum=D("0.9"),
        trend_weights=_POLICY.trend_weights,
        trend_min_coverage_required=_POLICY.trend_min_coverage_required,
    )
    quality = _assemble(policy=strict).inputs.quality
    assert quality is not None
    assert quality.min_coverage == D("0.9")


# =================================================================================================
# (2) 軸 3 事業・業種リスク
# =================================================================================================

_RISK_CODES = [flag.value for flag in RiskFlag]
_NON_RISK_CODES = [
    "high_valuation_dispersion",
    "very_high_valuation_dispersion",
    "industry_model_not_applied",
    "volatile_earnings",
    "data_quality_warning",
]


def test_the_six_risk_words_equal_the_codes_the_buy_decision_detects() -> None:
    # 買い判定の検出(margin_of_safety の 11 コード)の部分集合であること(語彙の同型)
    detected = {code for code in _ADJUSTMENT_CATEGORY_MAP_KEYS}
    assert set(_RISK_CODES) <= detected
    assert len(_RISK_CODES) == 6


def _margin_codes() -> set[str]:
    tree = ast.parse((_SRC / "domain" / "valuation" / "margin_of_safety.py").read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == (
            "_ADJUSTMENT_CATEGORY_MAP"
        ):
            assert isinstance(node.value, ast.Dict)
            return {
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    raise AssertionError("_ADJUSTMENT_CATEGORY_MAP not found")


_ADJUSTMENT_CATEGORY_MAP_KEYS = _margin_codes()


def test_only_the_six_risk_words_are_taken_from_the_adjustment_codes() -> None:
    codes = (*_NON_RISK_CODES, "cyclical_industry", "small_cap_or_low_liquidity")
    flags = _assemble(_facts(margin_adjustment_codes=codes)).inputs.risk_flags
    assert flags == frozenset({RiskFlag.CYCLICAL_INDUSTRY, RiskFlag.SMALL_CAP_OR_LOW_LIQUIDITY})


def test_all_detected_codes_are_taken_even_when_the_buy_decision_superseded_one() -> None:
    # 買い判定の dedupe(同一カテゴリは大きい方だけ)は別の規則。RAER は検出された全部を渡す
    flags = _assemble(
        _facts(margin_adjustment_codes=("cyclical_industry", "major_customer_dependency"))
    ).inputs.risk_flags
    assert flags == frozenset({RiskFlag.CYCLICAL_INDUSTRY, RiskFlag.MAJOR_CUSTOMER_DEPENDENCY})


def test_checked_no_risk_is_an_empty_set_and_not_observed_is_none() -> None:
    checked = _assemble(_facts(margin_computed=True, margin_adjustment_codes=()))
    assert checked.inputs.risk_flags == frozenset()
    assert checked.provenance.entry(PI.BUSINESS_RISK).derived
    unobserved = _assemble(_facts(margin_computed=False, margin_adjustment_codes=()))
    assert unobserved.inputs.risk_flags is None
    entry = unobserved.provenance.entry(PI.BUSINESS_RISK)
    assert not entry.derived
    assert entry.reason is PR.MARGIN_NOT_COMPUTED


def test_a_not_observed_risk_leaves_the_axis_unavailable_with_its_reason() -> None:
    _, evaluation, _ = raer_result_for_candidate(
        _facts(margin_computed=False),
        evaluation_date=_DATE,
        price_freshness=V.NORMAL,
        policy=_POLICY,
        coefficients=_coefficients(),
    )
    risk_axis = next(a for a in evaluation.axes if a.axis is A.BUSINESS_RISK)
    assert risk_axis.status is AxisStatus.UNAVAILABLE
    assert risk_axis.reasons == (AxisReasonCode.RISK_FLAGS_NOT_OBSERVED,)


def test_an_unobserved_risk_is_not_taken_from_codes_that_happen_to_be_present() -> None:
    flags = _assemble(
        _facts(margin_computed=False, margin_adjustment_codes=("cyclical_industry",))
    ).inputs.risk_flags
    assert flags is None


# =================================================================================================
# (2) 軸 5 営業トレンド
# =================================================================================================


def test_the_trend_uses_only_the_two_operating_components_and_the_existing_weights() -> None:
    trend = _assemble().inputs.operating_trend
    assert isinstance(trend, OperatingTrendScore)
    assert trend.value == D("12.857143")
    assert trend.components_used == frozenset(
        {TrendComponent.OPERATING_INCOME, TrendComponent.OPERATING_CASHFLOW}
    )
    assert trend.earnings_trend_model_version == "earnings_trend_v3"
    assert trend.min_coverage_required == D("0.3")


def test_a_dividend_or_acceleration_component_in_the_record_is_ignored() -> None:
    metrics = _trend_metrics(dividend_direction_component=-100.0, acceleration_component=-100.0)
    plain = _assemble().inputs.operating_trend
    noisy = _assemble(_facts(earnings_trend_metrics=metrics)).inputs.operating_trend
    assert plain == noisy


@pytest.mark.parametrize(
    ("state", "status", "reason"),
    [
        ("NOT_EVALUATED", AxisStatus.UNAVAILABLE, AxisReasonCode.COVERAGE_BELOW_MINIMUM),
        ("NOT_APPLICABLE", AxisStatus.NOT_APPLIED, AxisReasonCode.NOT_APPLICABLE_TO_STOCK),
    ],
)
def test_a_not_evaluated_or_not_applicable_state_never_makes_a_score(
    state: str, status: AxisStatus, reason: AxisReasonCode
) -> None:
    assembled = _assemble(_facts(earnings_trend_metrics=_trend_metrics(state=state)))
    trend = assembled.inputs.operating_trend
    assert isinstance(trend, TrendUnavailable)
    assert (trend.status, trend.reason) == (status, reason)
    entry = assembled.provenance.entry(PI.EARNINGS_TREND)
    assert not entry.derived
    assert entry.reason is PR.SOURCE_NOT_EVALUATED


def test_a_partial_coverage_uses_the_available_component_and_records_which() -> None:
    only_income = _assemble(
        _facts(earnings_trend_metrics=_trend_metrics(operating_cashflow_trend_component=None))
    ).inputs.operating_trend
    assert isinstance(only_income, OperatingTrendScore)
    assert only_income.components_used == frozenset({TrendComponent.OPERATING_INCOME})
    assert only_income.value == D("37.5")


def test_the_partial_coverage_boundary_is_exact_and_inclusive() -> None:
    # 営業 CF だけ = 0.75 / 2.5 = 0.3(下限ちょうど = 適用)。下限を上げると適用されない
    only_cf = _facts(earnings_trend_metrics=_trend_metrics(operating_income_trend_component=None))
    assert isinstance(_assemble(only_cf).inputs.operating_trend, OperatingTrendScore)
    strict = AssemblerPolicy(
        company_quality_minimum=_POLICY.company_quality_minimum,
        trend_weights=_POLICY.trend_weights,
        trend_min_coverage_required=D("0.31"),
    )
    assert isinstance(_assemble(only_cf, policy=strict).inputs.operating_trend, TrendUnavailable)


def test_an_evaluated_state_without_any_component_is_missing_input() -> None:
    metrics = _trend_metrics(
        operating_income_trend_component=None, operating_cashflow_trend_component=None
    )
    trend = _assemble(_facts(earnings_trend_metrics=metrics)).inputs.operating_trend
    assert isinstance(trend, TrendUnavailable)
    assert trend.reason is AxisReasonCode.INPUT_MISSING


def test_a_failed_or_missing_observation_makes_no_trend() -> None:
    failed = _assemble(
        _facts(
            earnings_trend_metrics={"shadow_state": "COMPUTATION_FAILED", "error_type": "KeyError"}
        )
    )
    assert failed.inputs.operating_trend is None
    assert failed.provenance.entry(PI.EARNINGS_TREND).reason is PR.SOURCE_COMPUTATION_FAILED
    missing = _assemble(_facts(earnings_trend_metrics=None))
    assert missing.inputs.operating_trend is None
    assert missing.provenance.entry(PI.EARNINGS_TREND).reason is PR.SOURCE_MISSING


@pytest.mark.parametrize(
    "metrics",
    [
        _trend_metrics(state="SOMETHING_NEW"),
        _trend_metrics(state=3),
        _trend_metrics(operating_income_trend_component=150.0),
        _trend_metrics(operating_income_trend_component=True),
        _trend_metrics(operating_cashflow_trend_component="x"),
        _trend_metrics(operating_income_trend_component=float("nan")),
    ],
    ids=repr,
)
def test_an_invalid_record_makes_no_trend_and_is_never_corrected(
    metrics: dict[str, object],
) -> None:
    assembled = _assemble(_facts(earnings_trend_metrics=metrics))
    assert assembled.inputs.operating_trend is None
    assert assembled.provenance.entry(PI.EARNINGS_TREND).reason is PR.INVALID_VALUE


def test_the_real_producer_output_is_accepted_end_to_end() -> None:
    # 生産側(earnings_trend_result_to_metrics)の実出力をそのまま渡す(キー名の同型)
    produced = earnings_trend_result_to_metrics(
        EarningsTrendResult(
            state=ES.EVALUATED,
            score=10.0,
            coverage=1.0,
            operating_income_trend_component=37.5,
            operating_cashflow_trend_component=-20.0,
            dividend_direction_component=0.0,
            evaluated_at=dt.datetime(2026, 10, 13, tzinfo=dt.UTC),
            model_version="earnings_trend_v3",
        )
    )
    trend = _assemble(_facts(earnings_trend_metrics=produced)).inputs.operating_trend
    assert isinstance(trend, OperatingTrendScore)
    assert trend.value == D("12.857143")


# =================================================================================================
# (3) 軸ごとの失敗は他の軸を巻き込まない・例外を投げない
# =================================================================================================


def test_one_broken_axis_does_not_disturb_the_others() -> None:
    broken = _assemble(
        _facts(
            common_quality=_common_quality(score=999),
            earnings_trend_metrics=_trend_metrics(state="SOMETHING_NEW"),
        )
    )
    assert broken.inputs.quality is None
    assert broken.inputs.operating_trend is None
    assert broken.inputs.risk_flags == frozenset()
    assert broken.inputs.expected_return.upside.value is not None
    assert broken.provenance.entry(PI.BUSINESS_RISK).derived
    assert broken.provenance.entry(PI.UPSIDE).derived
    assert broken.provenance.entry(PI.INCOME).derived


def test_the_assembler_never_raises_for_garbage_in_the_two_dictionaries() -> None:
    rng = random.Random(20261013)
    junk: list[object] = [
        None,
        True,
        False,
        "x",
        "EVALUATED",
        "COMPUTED",
        -1,
        0,
        0.5,
        30.0,
        55,
        1e308,
        float("nan"),
        float("inf"),
        D("1.5"),
        [],
        {},
        (1,),
    ]
    quality_keys = ["shadow_state", "score", "coverage_ratio", "items"]
    trend_keys = [
        "state",
        "shadow_state",
        "operating_income_trend_component",
        "operating_cashflow_trend_component",
    ]
    for _ in range(400):
        quality = {k: rng.choice(junk) for k in quality_keys if rng.random() < 0.8}
        trend = {k: rng.choice(junk) for k in trend_keys if rng.random() < 0.8}
        assembled = _assemble(_facts(common_quality=quality, earnings_trend_metrics=trend))
        assert len(assembled.provenance.entries) == len(ProvenanceItem)
        # 導けなかった項目は値を持たない(代替値なし)
        if not assembled.provenance.entry(PI.COMPANY_QUALITY).derived:
            assert assembled.inputs.quality is None
        if not assembled.provenance.entry(PI.EARNINGS_TREND).derived:
            assert not isinstance(assembled.inputs.operating_trend, OperatingTrendScore)


def test_the_result_is_deterministic_and_independent_of_the_order_of_the_codes() -> None:
    codes = ["cyclical_industry", "small_cap_or_low_liquidity", "earnings_within_3_business_days"]
    first = _assemble(_facts(margin_adjustment_codes=tuple(codes))).inputs.risk_flags
    second = _assemble(_facts(margin_adjustment_codes=tuple(reversed(codes)))).inputs.risk_flags
    assert first == second
    assert _assemble() == _assemble()


# =================================================================================================
# (5) 生産側との契約(同型 sweep)
# =================================================================================================


def _recommendation(**overrides: Any) -> Recommendation:
    values: dict[str, Any] = {
        "price_at_recommendation": D("1000"),
        "fair_value_neutral": D("1059"),
        "fair_value_overall_confidence": CL.MEDIUM,
        "fair_value_usable_for_trading_judgment": True,
        "dividend_yield_pct_at_recommendation": 3.0,
        "total_yield_pct_at_recommendation": 3.0,
        "required_margin_of_safety_entry": D("0.2"),
        "margin_adjustments": (
            MarginAdjustment(
                code="cyclical_industry",
                adjustment=D("0.05"),
                reason="x",
                category=MarginRiskCategory.INDUSTRY_AND_BUSINESS,
            ),
        ),
        "buy_score_input_facts": {"common_quality_shadow": _common_quality()},
        "earnings_trend_metrics": _trend_metrics(),
    }
    values.update(overrides)
    return build_recommendation(**values)


def test_the_facts_are_read_from_the_persisted_recommendation_fields() -> None:
    facts = facts_from_recommendation(_recommendation())
    assert facts.price == D("1000")
    assert facts.fair_value_neutral == D("1059")
    assert facts.fair_value_confidence is CL.MEDIUM
    assert facts.fair_value_usable is True
    assert facts.dividend_yield_pct == 3.0
    assert facts.total_yield_pct == 3.0
    assert facts.margin_computed is True
    assert facts.margin_adjustment_codes == ("cyclical_industry",)
    assert facts.common_quality == _common_quality()
    assert facts.earnings_trend_metrics == _trend_metrics()


def test_a_superseded_risk_code_that_is_counted_is_noted_in_the_provenance() -> None:
    # 買い側は同一カテゴリで大きい方だけを採用する(superseded)。RAER は検出された全部を渡す。
    # 景気業種 + 大口顧客依存は RAER では両方数える(承認済み)。その事実を記録し、観測で数えられる
    both = _assemble(
        _facts(
            margin_adjustment_codes=("cyclical_industry", "major_customer_dependency"),
            margin_superseded_codes=("major_customer_dependency",),
        )
    )
    assert ProvenanceNote.SUPERSEDED_CODES_INCLUDED in both.provenance.entry(PI.BUSINESS_RISK).notes
    assert both.inputs.risk_flags == frozenset(
        {RiskFlag.CYCLICAL_INDUSTRY, RiskFlag.MAJOR_CUSTOMER_DEPENDENCY}
    )
    # RAER の語彙でない code が superseded でも注記しない
    other = _assemble(
        _facts(
            margin_adjustment_codes=("cyclical_industry", "industry_model_not_applied"),
            margin_superseded_codes=("industry_model_not_applied",),
        )
    )
    other_notes = other.provenance.entry(PI.BUSINESS_RISK).notes
    assert ProvenanceNote.SUPERSEDED_CODES_INCLUDED not in other_notes
    plain_notes = _assemble().provenance.entry(PI.BUSINESS_RISK).notes
    assert ProvenanceNote.SUPERSEDED_CODES_INCLUDED not in plain_notes


def test_the_candidate_side_always_records_that_unobserved_words_were_treated_as_not_flagged() -> (
    None
):
    # 買い判定の『不明 → 該当しない』を引き継ぐ既知の限界(R1)。候補の『リスクなし』には、
    # 観測できなかった語を該当なしとして扱ったものが含まれうる。
    # その印を、導けたすべての記録に必ず残す(観測できる形)
    note = ProvenanceNote.CANDIDATE_UNOBSERVED_TREATED_AS_NOT_FLAGGED
    assert note in _assemble().provenance.entry(PI.BUSINESS_RISK).notes
    with_codes = _assemble(_facts(margin_adjustment_codes=("cyclical_industry",)))
    assert note in with_codes.provenance.entry(PI.BUSINESS_RISK).notes
    # 観測そのものが無い(None)ときは『該当なしとして扱った』のではないので付けない
    unobserved = _assemble(_facts(margin_computed=False)).provenance.entry(PI.BUSINESS_RISK)
    assert note not in unobserved.notes


def test_the_borrowed_coverage_minimum_is_noted_on_every_derived_quality_input() -> None:
    entry = _assemble().provenance.entry(PI.COMPANY_QUALITY)
    assert ProvenanceNote.COVERAGE_MINIMUM_BORROWED_FROM_HOLDING_SIDE in entry.notes
    # 下限に満たない(軸 2 が COVERAGE_BELOW_MINIMUM になる)入力にも付く = 率を数えられる
    low = _assemble(_facts(common_quality=_common_quality(coverage_ratio=0.1)))
    assert ProvenanceNote.COVERAGE_MINIMUM_BORROWED_FROM_HOLDING_SIDE in (
        low.provenance.entry(PI.COMPANY_QUALITY).notes
    )
    # 導けなかったときは付かない(借用していない)
    missing = _assemble(_facts(common_quality=None)).provenance.entry(PI.COMPANY_QUALITY)
    assert missing.notes == ()


# ---- 旧版の記録(field が無い / None / 旧いキー名)の組合せ -------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"fair_value_neutral": None},
        {"fair_value_overall_confidence": None},
        {"fair_value_usable_for_trading_judgment": None},
        {"fair_value_unusable_reason_code": None, "fair_value_usable_for_trading_judgment": False},
        {"dividend_yield_pct_at_recommendation": None, "total_yield_pct_at_recommendation": None},
        {"shareholder_benefit_yield_pct_at_recommendation": None},
        {"total_yield_pct_at_recommendation": None},
        {"required_margin_of_safety_entry": None},
        {"margin_adjustments": ()},
        {"buy_score_input_facts": None},
        {"buy_score_input_facts": {}},
        {"buy_score_input_facts": {"common_quality_shadow": None}},
        {"buy_score_input_facts": {"old_name_quality": {"score": 30.0}}},
        {"earnings_trend_metrics": {}},
        {"earnings_trend_metrics": {"old_state": "EVALUATED"}},
    ],
    ids=lambda o: ",".join(sorted(o)) + "=" + repr(list(o.values())),
)
def test_an_older_or_partial_recommendation_never_raises_and_never_fills_a_missing_value(
    overrides: dict[str, Any],
) -> None:
    rec = _recommendation(**overrides)
    assembled = _assemble(facts_from_recommendation(rec))
    assert len(assembled.provenance.entries) == len(ProvenanceItem)
    for entry in assembled.provenance.entries:
        assert entry.derived or entry.reason is not None
    if not assembled.provenance.entry(PI.UPSIDE).derived:
        assert assembled.inputs.expected_return.upside.value is None
    if not assembled.provenance.entry(PI.INCOME).derived:
        assert assembled.inputs.expected_return.income.value is None


def test_the_assembler_adds_no_field_to_the_recommendation_entity() -> None:
    # Recommendation は extra="forbid"(新しい field を足すと旧版へ戻せない)。読み取り専用で済ませる
    names = set(Recommendation.model_fields)
    assert not [n for n in names if "raer" in n or "risk_flag" in n]
    tree = ast.parse(_MODULE_PATH.read_text("utf-8"))
    # 推奨への書き込み(属性への代入)が無い
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                assert not (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "rec"
                )


def test_only_the_extraction_function_references_the_recommendation_fields() -> None:
    tree = ast.parse(_MODULE_PATH.read_text("utf-8"))
    for function in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        uses = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "rec"
        ]
        if function.name != "facts_from_recommendation":
            assert uses == [], function.name


def test_the_provenance_carries_no_value_code_amount_or_holding_identifier() -> None:
    # 記録の語彙は固定の語だけ(出所のフィールド名・理由・注記)。
    # 値・数字・銘柄・保有の識別子を持たない
    assembled = _assemble(
        facts_from_recommendation(
            _recommendation(stock_code="1234", price_at_recommendation=D("2345.5"))
        )
    )
    for entry in assembled.provenance.entries:
        text = f"{entry.item} {entry.source} {entry.reason} {entry.notes}"
        assert not any(ch.isdigit() for ch in text), text
        assert "1234" not in text


# ---- H1: 検出の記録(buy_score_input_facts[raer_risk_detection])と RiskFlagView ----------------

_ALL_OBSERVED = {"earnings_date": True, "liquidity": True, "industry": True, "eps": True}


def _detection(flags: list[str] | None = None, **observed: bool) -> dict[str, object]:
    return {
        "version": 1,
        "flags": list(flags or []),
        "observed": {**_ALL_OBSERVED, **observed},
    }


def test_the_detection_record_contract_literals_are_fixed_for_the_producer() -> None:
    # 生産側(買い判定の記録。H2)は domain の raer 系 module を import できず、同じ literal を持つ。
    # 変えるときは生産側と一緒に(契約テストが両方を見る)
    assert RISK_DETECTION_FACTS_KEY == "raer_risk_detection"
    assert ri.RISK_DETECTION_VERSION == 1
    assert ri._OBSERVED_KEYS == ("earnings_date", "liquidity", "industry", "eps")


def test_a_word_that_was_detected_is_observed_even_when_its_input_was_not() -> None:
    # 該当(True)は観測できた証拠。観測できなかった語は『該当しない』と言えない語だけ。
    # 例: 業種の文字列が取れなくても、景気型の銘柄分類で景気業種に該当すると確定する
    view = risk_flag_view(_facts(risk_detection=_detection(["cyclical_industry"], industry=False)))
    assert view.source is RiskFlagSource.DETECTION_RECORD
    assert RiskFlag.CYCLICAL_INDUSTRY in view.flags
    assert RiskFlag.CYCLICAL_INDUSTRY not in view.unobserved
    assert RiskFlag.MAJOR_CUSTOMER_DEPENDENCY in view.unobserved  # 業種が不明なので言えない
    small = risk_flag_view(
        _facts(risk_detection=_detection(["small_cap_or_low_liquidity"], liquidity=False))
    )
    assert small.flags == frozenset({RiskFlag.SMALL_CAP_OR_LOW_LIQUIDITY})
    assert small.unobserved == frozenset()


def test_a_detection_record_with_everything_observed_gives_a_fully_observed_view() -> None:
    view = risk_flag_view(_facts(risk_detection=_detection(["cyclical_industry"])))
    assert view.source is RiskFlagSource.DETECTION_RECORD
    assert view.flags == frozenset({RiskFlag.CYCLICAL_INDUSTRY})
    assert view.unobserved == frozenset()
    assert view.observed_all()


@pytest.mark.parametrize(
    ("observed", "expected_unobserved"),
    [
        (
            {"earnings_date": False},
            {RiskFlag.EARNINGS_WITHIN_3_BUSINESS_DAYS, RiskFlag.EARNINGS_WITHIN_7_BUSINESS_DAYS},
        ),
        ({"liquidity": False}, {RiskFlag.SMALL_CAP_OR_LOW_LIQUIDITY}),
        (
            {"industry": False},
            {
                RiskFlag.CYCLICAL_INDUSTRY,
                RiskFlag.MAJOR_CUSTOMER_DEPENDENCY,
                RiskFlag.TEMPORARY_EARNINGS_BOOST_RISK,
            },
        ),
        # eps は『景気業種でない』と確定していれば要らない(一時増益のリスクは景気業種のときだけ)
        ({"eps": False}, set()),
    ],
    ids=["earnings", "liquidity", "industry", "eps-not-needed"],
)
def test_each_unobserved_input_makes_exactly_its_words_unobserved(
    observed: dict[str, bool], expected_unobserved: set[RiskFlag]
) -> None:
    view = risk_flag_view(_facts(risk_detection=_detection(**observed)))
    assert view.unobserved == frozenset(expected_unobserved)
    assert view.flags == frozenset()
    assert view.observed_all() is (not expected_unobserved)


def test_the_temporary_boost_needs_the_eps_observation_only_for_a_cyclical_stock() -> None:
    cyclical_without_eps = risk_flag_view(
        _facts(risk_detection=_detection(["cyclical_industry"], eps=False))
    )
    assert RiskFlag.TEMPORARY_EARNINGS_BOOST_RISK in cyclical_without_eps.unobserved
    cyclical_with_eps = risk_flag_view(
        _facts(risk_detection=_detection(["cyclical_industry"], eps=True))
    )
    assert RiskFlag.TEMPORARY_EARNINGS_BOOST_RISK not in cyclical_with_eps.unobserved
    flagged = risk_flag_view(
        _facts(risk_detection=_detection(["cyclical_industry", "temporary_earnings_boost_risk"]))
    )
    assert RiskFlag.TEMPORARY_EARNINGS_BOOST_RISK in flagged.flags


def test_the_view_lets_the_caller_check_only_the_words_it_needs() -> None:
    high_risk = frozenset(
        {
            RiskFlag.MAJOR_CUSTOMER_DEPENDENCY,
            RiskFlag.TEMPORARY_EARNINGS_BOOST_RISK,
            RiskFlag.SMALL_CAP_OR_LOW_LIQUIDITY,
        }
    )
    earnings_unknown = risk_flag_view(_facts(risk_detection=_detection(earnings_date=False)))
    assert not earnings_unknown.observed_all()
    assert earnings_unknown.observed_all(
        high_risk
    )  # 決算日が不明でも high-risk の 3 語は観測できた
    liquidity_unknown = risk_flag_view(_facts(risk_detection=_detection(liquidity=False)))
    assert not liquidity_unknown.observed_all(high_risk)


_FLAGGED_SMALL = ["small_cap_or_low_liquidity"]


@pytest.mark.parametrize(
    "record",
    [
        {"version": 2, "flags": [], "observed": _ALL_OBSERVED},
        {"version": True, "flags": [], "observed": _ALL_OBSERVED},
        {"flags": [], "observed": _ALL_OBSERVED},
        {"version": 1, "flags": "cyclical_industry", "observed": _ALL_OBSERVED},
        {"version": 1, "flags": ["no_such_word"], "observed": _ALL_OBSERVED},
        {"version": 1, "flags": [1], "observed": _ALL_OBSERVED},
        {"version": 1, "flags": [], "observed": {"earnings_date": True}},
        {"version": 1, "flags": [], "observed": {**_ALL_OBSERVED, "extra": True}},
        {"version": 1, "flags": [], "observed": {**_ALL_OBSERVED, "liquidity": "yes"}},
        {"version": 1, "flags": [], "observed": {**_ALL_OBSERVED, "liquidity": 1}},
        {"version": 1, "flags": [], "observed": None},
    ],
    ids=repr,
)
def test_an_invalid_detection_record_is_never_corrected_and_falls_back_to_the_margin_record(
    record: dict[str, object],
) -> None:
    fallback = risk_flag_view(
        _facts(
            risk_detection=record,
            margin_computed=True,
            margin_adjustment_codes=("small_cap_or_low_liquidity",),
        )
    )
    assert fallback.source is RiskFlagSource.MARGIN_ADJUSTMENTS
    assert fallback.flags == frozenset({RiskFlag.SMALL_CAP_OR_LOW_LIQUIDITY})
    assert not fallback.observed_all()  # 旧い記録は『確認できた』と言えない
    unobserved = risk_flag_view(_facts(risk_detection=record, margin_computed=False))
    assert unobserved.source is RiskFlagSource.NOT_OBSERVED
    assert unobserved.unobserved == frozenset(RiskFlag)


def test_the_detection_record_is_used_even_when_the_margin_was_not_computed() -> None:
    # Fair Value の信頼度が LOW で安全余裕率が算出されない銘柄でも、検出の記録があれば読める
    view = risk_flag_view(
        _facts(
            risk_detection=_detection(_FLAGGED_SMALL),
            margin_computed=False,
            margin_adjustment_codes=(),
        )
    )
    assert view.source is RiskFlagSource.DETECTION_RECORD
    assert view.flags == frozenset({RiskFlag.SMALL_CAP_OR_LOW_LIQUIDITY})
    assembled = _assemble(_facts(risk_detection=_detection(_FLAGGED_SMALL), margin_computed=False))
    assert assembled.inputs.risk_flags == frozenset({RiskFlag.SMALL_CAP_OR_LOW_LIQUIDITY})
    assert assembled.provenance.entry(PI.BUSINESS_RISK).derived


def test_the_old_margin_record_cannot_prove_observation_so_it_is_not_fully_observed() -> None:
    view = risk_flag_view(_facts(margin_computed=True, margin_adjustment_codes=()))
    assert view.source is RiskFlagSource.MARGIN_ADJUSTMENTS
    assert view.flags == frozenset()
    assert view.unobserved == frozenset()  # 区別できない
    assert not view.observed_all()  # ただし『確認できた』とは言わない(保有側は代替値を置かない)


def test_the_candidate_provenance_distinguishes_fully_observed_from_treated_as_not_flagged() -> (
    None
):
    full = _assemble(_facts(risk_detection=_detection()))
    notes = full.provenance.entry(PI.BUSINESS_RISK).notes
    assert ProvenanceNote.RISK_ALL_WORDS_OBSERVED in notes
    assert ProvenanceNote.CANDIDATE_UNOBSERVED_TREATED_AS_NOT_FLAGGED not in notes
    partial = _assemble(_facts(risk_detection=_detection(liquidity=False)))
    notes = partial.provenance.entry(PI.BUSINESS_RISK).notes
    assert ProvenanceNote.CANDIDATE_UNOBSERVED_TREATED_AS_NOT_FLAGGED in notes
    assert ProvenanceNote.RISK_ALL_WORDS_OBSERVED not in notes
    assert partial.inputs.risk_flags == frozenset()  # R1: 候補側は未観測を『該当なし』として扱う
    old = _assemble(_facts(margin_computed=True))
    assert ProvenanceNote.CANDIDATE_UNOBSERVED_TREATED_AS_NOT_FLAGGED in (
        old.provenance.entry(PI.BUSINESS_RISK).notes
    )


def test_the_view_is_the_same_function_for_a_candidate_and_a_held_stock() -> None:
    rec = _recommendation(
        buy_score_input_facts={
            "common_quality_shadow": _common_quality(),
            RISK_DETECTION_FACTS_KEY: _detection(["major_customer_dependency"]),
        }
    )
    view = risk_flag_view_from_recommendation(rec)
    assert view == risk_flag_view(facts_from_recommendation(rec))
    assert view.flags == frozenset({RiskFlag.MAJOR_CUSTOMER_DEPENDENCY})
    assert isinstance(view, RiskFlagView)


# ---- 保有銘柄の市場の事実(価格・価格時点・業種)--------------------------------------------------


def test_the_holding_market_view_is_read_from_the_recommendation_without_amounts_or_shares() -> (
    None
):
    rec = _recommendation(
        price_at_recommendation=D("2345.5"),
        price_as_of_date=date(2026, 10, 13),
        buy_industry_sector=BuyIndustrySector.AUTOMOTIVE_PARTS,
    )
    view = holding_market_view_from_recommendation(rec)
    assert view == HoldingMarketView(
        price=D("2345.5"),
        price_as_of_date=date(2026, 10, 13),
        buy_industry_sector=BuyIndustrySector.AUTOMOTIVE_PARTS,
    )
    assert {f.name for f in dataclasses.fields(view)} == {
        "price",
        "price_as_of_date",
        "buy_industry_sector",
    }  # 株数・取得単価・損益・保有 ID を持たない


def test_a_missing_market_fact_stays_none_and_an_unknown_sector_is_one_representation() -> None:
    rec = _recommendation(price_as_of_date=None, buy_industry_sector=None)
    view = holding_market_view_from_recommendation(rec)
    assert view.price_as_of_date is None
    assert view.buy_industry_sector is BuyIndustrySector.UNKNOWN  # None を作らない
    assert view.price == D("1000")
    explicit = holding_market_view_from_recommendation(
        _recommendation(buy_industry_sector=BuyIndustrySector.UNKNOWN)
    )
    assert explicit.buy_industry_sector is BuyIndustrySector.UNKNOWN


@pytest.mark.parametrize("price", [D("0"), D("-1")])
def test_a_non_positive_price_is_not_usable_and_becomes_none(price: Decimal) -> None:
    assert holding_market_view(_facts(price=price)).price is None
    assert holding_market_view(_facts(price=None)).price is None


def test_the_market_view_uses_the_same_extraction_function_as_the_candidate() -> None:
    rec = _recommendation()
    assert holding_market_view_from_recommendation(rec) == holding_market_view(
        facts_from_recommendation(rec)
    )


def test_the_view_rejects_a_flag_that_is_also_unobserved() -> None:
    with pytest.raises(ValueError):
        RiskFlagView(
            frozenset({RiskFlag.CYCLICAL_INDUSTRY}),
            frozenset({RiskFlag.CYCLICAL_INDUSTRY}),
            RiskFlagSource.DETECTION_RECORD,
        )


def test_the_facts_keep_a_code_the_buy_decision_marked_as_superseded() -> None:
    # 買い判定は同一カテゴリで大きい方だけを採用する(superseded_by)。RAER は別の重複除外を
    # 持つので、検出された全部を渡す
    superseded = MarginAdjustment(
        code="major_customer_dependency",
        adjustment=D("0.03"),
        reason="x",
        category=MarginRiskCategory.INDUSTRY_AND_BUSINESS,
        superseded_by="cyclical_industry",
    )
    adopted = MarginAdjustment(
        code="cyclical_industry",
        adjustment=D("0.05"),
        reason="x",
        category=MarginRiskCategory.INDUSTRY_AND_BUSINESS,
    )
    facts = facts_from_recommendation(_recommendation(margin_adjustments=(adopted, superseded)))
    assert set(facts.margin_adjustment_codes) == {"cyclical_industry", "major_customer_dependency"}


def test_a_recommendation_without_a_computed_margin_is_not_observed_for_risk() -> None:
    facts = facts_from_recommendation(_recommendation(required_margin_of_safety_entry=None))
    assert facts.margin_computed is False


def test_absent_or_empty_records_become_none_not_empty_dictionaries() -> None:
    bare = facts_from_recommendation(
        _recommendation(buy_score_input_facts=None, earnings_trend_metrics={})
    )
    assert bare.common_quality is None
    assert bare.earnings_trend_metrics is None
    no_key = facts_from_recommendation(_recommendation(buy_score_input_facts={"other": 1}))
    assert no_key.common_quality is None
    not_a_mapping = facts_from_recommendation(
        _recommendation(buy_score_input_facts={"common_quality_shadow": "oops"})
    )
    assert not_a_mapping.common_quality is None


def test_a_recommendation_round_trips_into_the_same_raer_as_the_hand_built_facts() -> None:
    from_rec = _assemble(facts_from_recommendation(_recommendation()))
    by_hand = _assemble(_facts(margin_adjustment_codes=("cyclical_industry",)))
    assert from_rec == by_hand


def test_every_recommendation_field_the_assembler_reads_exists_on_the_entity() -> None:
    read = {
        "price_at_recommendation",
        "price_as_of_date",
        "buy_industry_sector",
        "fair_value_neutral",
        "fair_value_overall_confidence",
        "fair_value_usable_for_trading_judgment",
        "fair_value_unusable_reason_code",
        "dividend_yield_pct_at_recommendation",
        "shareholder_benefit_yield_pct_at_recommendation",
        "total_yield_pct_at_recommendation",
        "required_margin_of_safety_entry",
        "margin_adjustments",
        "buy_score_input_facts",
        "earnings_trend_metrics",
    }
    assert read <= set(Recommendation.model_fields)
    tree = ast.parse(_MODULE_PATH.read_text("utf-8"))
    attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "rec"
    }
    assert attributes <= read, attributes - read  # 読む名前が上の一覧から漏れていない


def test_the_computed_state_equals_the_producer_constant() -> None:
    assert ri.SHADOW_STATE_COMPUTED == SHADOW_STATE_COMPUTED


def test_the_producer_writes_the_keys_the_assembler_reads() -> None:
    tree = ast.parse((_SRC / "services" / "buy_signal_service.py").read_text("utf-8"))
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_observe_common_quality_shadow"
    )
    keys: set[str] = set()
    for node in ast.walk(method):
        if isinstance(node, ast.Dict):
            keys |= {
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    assert {"shadow_state", "score", "coverage_ratio"} <= keys
    stored = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == ri.COMMON_QUALITY_FACTS_KEY
    ]
    assert stored  # 推奨の buy_score_input_facts へ同じキーで書かれている


def test_the_shared_quality_scale_is_zero_to_fifty_as_the_assembler_assumes() -> None:
    from jstock_advisor.config.models import CompanyQualityWeights

    weights = CompanyQualityWeights.model_fields
    assert weights  # 生産側の重みの定義が存在する
    config = yaml.safe_load((_ROOT / "config" / "holding_decision_rules.yaml").read_text("utf-8"))
    total = sum(float(v) for v in config["company_quality_weights"].values())
    assert total == 50.0  # 満点 = 50(保有側と同じ関数・同じ重み)


# ---- 既存の config からの政策の組み立て ----


def test_the_policy_is_built_from_the_existing_config_values_only() -> None:
    rules = EarningsTrendRulesConfig(
        **yaml.safe_load((_ROOT / "config" / "earnings_trend_rules.yaml").read_text("utf-8"))
    )
    holding = yaml.safe_load((_ROOT / "config" / "holding_decision_rules.yaml").read_text("utf-8"))
    minimum = holding["coverage_thresholds"]["company_quality_minimum"]
    policy = AssemblerPolicy.from_rules(company_quality_minimum=minimum, trend_rules=rules)
    assert policy.company_quality_minimum == D("0.6")
    assert policy.trend_weights == TrendWeights(D("1.0"), D("0.75"), D("0.5"), D("0.25"))
    assert policy.trend_min_coverage_required == D("0.3")
    assert policy.trend_model_version == "earnings_trend_v3"


# =================================================================================================
# (6) 構造
# =================================================================================================


def test_the_module_has_no_float_arithmetic_clock_or_io_dependency() -> None:
    tree = ast.parse(_MODULE_PATH.read_text("utf-8"))
    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    forbidden = ("services", "infrastructure", "lambda_handlers", "exit_architecture")
    assert not [m for m in imported if any(f in m for f in forbidden)]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            assert name not in {"float", "now", "today", "utcnow", "time", "open", "print"}, name
    text = _MODULE_PATH.read_text("utf-8")
    assert "import time" not in text
    assert "logging" not in text


def test_nothing_in_src_imports_the_assembler_yet() -> None:
    importers = [
        path
        for path in _SRC.rglob("*.py")
        if path != _MODULE_PATH and "raer_inputs" in path.read_text("utf-8")
    ]
    assert importers == []


def test_the_assembler_is_not_wired_into_the_buy_flow_names() -> None:
    # 配線されていない = 買い判定・通知の戻り値に RAER の入力の組み立てが出てこない
    text = (_SRC / "services" / "buy_signal_service.py").read_text("utf-8")
    assert "assemble_raer_inputs" not in text
