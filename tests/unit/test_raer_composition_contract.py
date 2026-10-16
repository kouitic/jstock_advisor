"""Issue #602 PR-B2: RAER の組合せ(承認済みの暫定式 A-1〜A-5)の契約テスト。

固定するもの(設計 rev1 …6137191422 / rev2 …6137343422 / rev3 …6137442140)
  (1) 式: 手計算の golden(厳密な Decimal)・恒等式(土台 − Σ寄与度 = value)・悪化だけ減点
  (2) 欠測を 0 で補完しない: 欠測の軸は UNAVAILABLE(理由つき)・土台が不明なら RAER 全体が算出不能
  (3) 軸 5 の適用条件(R1)・軸 2 の充足度(R2)・Decimal の取り込みと丸めの 1 か所(R3)・不変条件(R4)
  (4) 信頼度のラベル(X-2。到達可能な値は LOW / MEDIUM)・PROVISIONAL は 5 軸と比較できない
  (5) 係数の識別子(係数・モデル版が変われば変わる)
  (6) 構造: float 演算なし・時計なし・どこからも import されない(dormant)
"""

from __future__ import annotations

import ast
import itertools
import random
from datetime import date
from decimal import Decimal, Inexact, localcontext
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import ConfidenceLevel, EarningsTrendEvaluationState
from jstock_advisor.domain.valuation import raer_composition as rc
from jstock_advisor.domain.valuation.expected_return import (
    ComponentResult,
    ExpectedReturnResult,
    ReasonCode,
)
from jstock_advisor.domain.valuation.raer import (
    AXIS_ORDER,
    COMBINED_MODEL_VERSION,
    MODEL_VERSION,
    AxisAdjustment,
    AxisCode,
    AxisContribution,
    AxisReasonCode,
    AxisStatus,
    RaerComposition,
    RaerEvaluation,
    RaerReasonCode,
    RaerState,
    RiskContributionStatus,
    RiskFlag,
    RiskItem,
    SpecFlag,
    are_comparable,
    exact_decimal_context,
    to_raer_result,
)
from jstock_advisor.domain.valuation.raer_composition import (
    OperatingTrendScore,
    QualityInput,
    RaerCoefficients,
    RaerInputs,
    TrendComponent,
    TrendUnavailable,
    TrendWeights,
    compose_raer,
    resolve_raer_confidence,
    to_decimal,
    trend_score_for_raer,
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"
_MODULE_PATH = _SRC / "domain" / "valuation" / "raer_composition.py"
_DATE = date(2026, 10, 13)

D = Decimal
CL = ConfidenceLevel
ES = EarningsTrendEvaluationState
F = RiskFlag
A = AxisCode


# --- 承認済みの初期値(#602 …6114608473。YAML は PR-E。ここはテスト用の固定値)-----------------


def approved_coefficients(**overrides: Any) -> RaerCoefficients:
    values: dict[str, Any] = {
        "fv_confidence_factor": {CL.HIGH: D("1.00"), CL.MEDIUM: D("0.85"), CL.LOW: D("0.60")},
        "quality_deduction_max": D(4),
        "quality_max_score": D(50),
        "risk_deductions": {
            F.EARNINGS_WITHIN_3_BUSINESS_DAYS: D("3.0"),
            F.EARNINGS_WITHIN_7_BUSINESS_DAYS: D("1.5"),
            F.CYCLICAL_INDUSTRY: D("1.5"),
            F.MAJOR_CUSTOMER_DEPENDENCY: D("1.5"),
            F.TEMPORARY_EARNINGS_BOOST_RISK: D("2.0"),
            F.SMALL_CAP_OR_LOW_LIQUIDITY: D("2.0"),
        },
        "risk_deduction_cap": D(6),
        "trend_deduction_scale": D(3),
        "min_applied_axes": 3,
        "required_axes": frozenset({A.FAIR_VALUE_CONFIDENCE}),
    }
    values.update(overrides)
    return RaerCoefficients(**values)


#: 承認済みの初期値から作った識別子の literal(golden)。値を 1 つ変えるとこのテストが落ちる =
#: 係数の変更は『意図した変更』としてテストと承認の記録を一緒に更新することを強制する(A-4)
_APPROVED_COEFFICIENT_SET_ID = "a5ae8a5a937b4e8e-c1"

_WEIGHTS = TrendWeights(
    D("1.0"), D("0.75"), D("0.5"), D("0.25")
)  # 既存の earnings_trend_rules.yaml


def _er(
    upside: float | None = 5.9,
    income: float | None = 3.0,
    confidence: CL | None = CL.MEDIUM,
    evaluation_date: date = _DATE,
) -> ExpectedReturnResult:
    def comp(value: float | None, reason: ReasonCode) -> ComponentResult:
        return (
            ComponentResult(value=value)
            if value is not None
            else ComponentResult(reasons=(reason,))
        )

    return ExpectedReturnResult(
        model_version="er-components-v1",
        evaluation_date=evaluation_date,
        upside=comp(upside, ReasonCode.FAIR_VALUE_UNAVAILABLE),
        income=comp(income, ReasonCode.DIVIDEND_UNKNOWN),
        revision=ComponentResult(reasons=(ReasonCode.REVISION_NOT_IMPLEMENTED,)),
        composite=ComponentResult(reasons=(ReasonCode.COMPOSITION_RULE_NOT_DECIDED,)),
        annualized=ComponentResult(reasons=(ReasonCode.HORIZON_NOT_ESTIMATED,)),
        fair_value_confidence=confidence,
    )


def _trend(
    income: object = 37.5,
    cashflow: object = -20.0,
    state: ES = ES.EVALUATED,
    minimum: object = 0.3,
) -> OperatingTrendScore | TrendUnavailable:
    return trend_score_for_raer(
        evaluation_state=state,
        operating_income_component=income,
        operating_cashflow_component=cashflow,
        weights=_WEIGHTS,
        min_coverage_required=minimum,
        earnings_trend_model_version="earnings_trend_v3",
    )


def _quality(score: object = 30, coverage: object = 0.8, minimum: object = 0.6) -> QualityInput:
    return QualityInput(to_decimal(score), to_decimal(coverage), to_decimal(minimum), "v1")


def _inputs(**overrides: Any) -> RaerInputs:
    values: dict[str, Any] = {
        "expected_return": _er(),
        "quality": _quality(),
        "risk_flags": frozenset(),
        "operating_trend": _trend(),
    }
    values.update(overrides)
    return RaerInputs(**values)


def _compose(inputs: RaerInputs | None = None, **coef: Any) -> RaerEvaluation:
    return compose_raer(
        evaluation_date=_DATE,
        inputs=inputs or _inputs(),
        coefficients=approved_coefficients(**coef),
    )


def _contribution(evaluation: RaerEvaluation, axis: AxisCode) -> D:
    assert evaluation.composition is not None
    (found,) = [c for c in evaluation.composition.contributions if c.axis is axis]
    return found.pct_points


def _value(evaluation: RaerEvaluation) -> D:
    assert evaluation.composition is not None
    return evaluation.composition.value


# =================================================================================================
# (1) 式: 手計算の golden
# =================================================================================================


def test_the_worked_example_matches_the_hand_calculation() -> None:
    """U = 5.9・I = 3.0・信頼度 MEDIUM(f1 = 0.85)・Q = 30・s = +12.857143。景気 + 一時増益 + 大口。

    土台 = 8.9 / 軸 1 = 5.9 x 0.15 = 0.885 / 軸 2 = 4 x (1 - 30/50) = 1.6 /
    軸 3 = 一時増益 2.0(景気 1.5 は重複で除外)+ 大口 1.5 = 3.5 / 軸 5 = 0(s >= 0)/
    値 = 8.9 - 5.985 = 2.915
    """
    evaluation = _compose(
        _inputs(
            risk_flags={
                F.CYCLICAL_INDUSTRY,
                F.TEMPORARY_EARNINGS_BOOST_RISK,
                F.MAJOR_CUSTOMER_DEPENDENCY,
            }
        )
    )
    composition = evaluation.composition
    assert composition is not None
    assert composition.base_pct == D("8.9")
    assert _contribution(evaluation, A.FAIR_VALUE_CONFIDENCE) == D("0.885")
    assert _contribution(evaluation, A.COMPANY_QUALITY) == D("1.6")
    assert _contribution(evaluation, A.BUSINESS_RISK) == D("3.5")
    assert _contribution(evaluation, A.EARNINGS_TREND) == D(0)
    assert composition.value == D("2.915")
    assert composition.state is RaerState.PROVISIONAL
    assert composition.confidence is CL.MEDIUM


@pytest.mark.parametrize(
    ("upside", "income", "confidence", "expected_value"),
    [
        (10.0, 2.0, CL.HIGH, D("12.0") - D(0) - D("1.6")),  # f1 = 1.00(割引なし)
        (10.0, 2.0, CL.MEDIUM, D("12.0") - D("1.5") - D("1.6")),  # 10 x 0.15
        (10.0, 2.0, CL.LOW, D("12.0") - D("4.0") - D("1.6")),  # 10 x 0.40
        (-6.0, 3.0, CL.LOW, D("-3.0") - D(0) - D("1.6")),  # U < 0 は割り引かない
        (0.0, 3.0, CL.LOW, D("3.0") - D(0) - D("1.6")),  # U = 0
    ],
)
def test_the_fair_value_confidence_discounts_only_a_positive_upside(
    upside: float, income: float, confidence: CL, expected_value: D
) -> None:
    evaluation = _compose(
        _inputs(
            expected_return=_er(upside, income, confidence),
            # 軸 2 = 1.6(Q = 30)・軸 3 = 0(リスクなし)・軸 5 = 0(s >= 0)
            operating_trend=_trend(10.0, 10.0),
        )
    )
    assert _value(evaluation) == expected_value
    assert evaluation.composition is not None
    if upside <= 0:
        assert _contribution(evaluation, A.FAIR_VALUE_CONFIDENCE) == D(0)  # 適用済みで寄与 0


def test_the_income_is_never_discounted_by_the_fair_value_confidence() -> None:
    low = _value(
        _compose(_inputs(expected_return=_er(0.0, 4.0, CL.LOW), operating_trend=_trend(5.0, 5.0)))
    )
    high = _value(
        _compose(_inputs(expected_return=_er(0.0, 4.0, CL.HIGH), operating_trend=_trend(5.0, 5.0)))
    )
    assert low == high  # U = 0 のとき信頼度は値に影響しない(I は割り引かない)


@pytest.mark.parametrize(
    ("quality", "deduction"),
    [(0, D(4)), (25, D(2)), (50, D(0)), (30, D("1.6")), (12.5, D("3.0"))],
)
def test_the_quality_deduction_is_four_times_one_minus_q_over_fifty(
    quality: float, deduction: D
) -> None:
    evaluation = _compose(_inputs(quality=_quality(score=quality)))
    assert _contribution(evaluation, A.COMPANY_QUALITY) == deduction


@pytest.mark.parametrize(
    ("trend_income", "trend_cashflow", "expected_s", "deduction"),
    [
        (-100.0, -100.0, D("-100.000000"), D(3)),
        (-50.0, -50.0, D("-50.000000"), D("1.5")),
        (0.0, 0.0, D("0.000000"), D(0)),
        (50.0, 50.0, D("50.000000"), D(0)),  # 改善は加点しない
        (100.0, 100.0, D("100.000000"), D(0)),
    ],
)
def test_only_deterioration_is_deducted_and_improvement_is_not_credited(
    trend_income: float, trend_cashflow: float, expected_s: D, deduction: D
) -> None:
    trend = _trend(trend_income, trend_cashflow)
    assert isinstance(trend, OperatingTrendScore) and trend.value == expected_s
    evaluation = _compose(_inputs(operating_trend=trend))
    assert _contribution(evaluation, A.EARNINGS_TREND) == deduction


def test_an_improving_trend_gives_the_same_value_as_a_flat_one() -> None:
    flat = _value(_compose(_inputs(operating_trend=_trend(0.0, 0.0))))
    improving = _value(_compose(_inputs(operating_trend=_trend(80.0, 80.0))))
    assert flat == improving


def test_the_base_is_the_plain_sum_of_upside_and_income_with_no_annualisation() -> None:
    composition = _compose().composition
    assert composition is not None
    assert (composition.upside_pct, composition.income_pct, composition.base_pct) == (
        D("5.9"),
        D("3.0"),
        D("8.9"),
    )
    assert SpecFlag.HORIZON_MISMATCH_PROVISIONAL in composition.spec_flags


def test_the_value_is_an_exact_decimal_not_a_float_artifact() -> None:
    """U = 5.9 x f1 = 0.85 は厳密には 5.015。float の積は 5.015000000000001(変異『float』)。"""
    assert repr(5.9 * 0.85) == "5.015000000000001"
    evaluation = _compose(
        _inputs(
            expected_return=_er(5.9, 0.0, CL.MEDIUM),
            quality=_quality(score=50),
            operating_trend=_trend(0.0, 0.0),
        )
    )
    composition = evaluation.composition
    assert composition is not None
    assert composition.value == D("5.015")
    assert _contribution(evaluation, A.FAIR_VALUE_CONFIDENCE) == D("0.885")


# =================================================================================================
# 恒等式・性質(乱数 + 総当たり)
# =================================================================================================

_ALL_FLAGS = list(RiskFlag)


def _random_inputs(rng: random.Random) -> RaerInputs:
    flags = {f for f in _ALL_FLAGS if rng.random() < 0.4}
    return _inputs(
        expected_return=_er(
            round(rng.uniform(-30, 60), 3), round(rng.uniform(0, 8), 3), rng.choice(list(CL))
        ),
        quality=_quality(
            score=round(rng.uniform(0, 50), 2), coverage=round(rng.uniform(0.6, 1), 2)
        ),
        risk_flags=flags,
        operating_trend=_trend(round(rng.uniform(-100, 100), 2), round(rng.uniform(-100, 100), 2)),
    )


def test_the_identity_base_minus_contributions_equals_the_value_for_random_inputs() -> None:
    rng = random.Random(602)
    for _ in range(1500):
        evaluation = _compose(_random_inputs(rng))
        composition = evaluation.composition
        assert composition is not None
        with localcontext(exact_decimal_context()):
            total = sum((c.pct_points for c in composition.contributions), start=D(0))
            assert composition.value == composition.base_pct - total
        assert {c.axis for c in composition.contributions} == evaluation.axes_applied


def test_the_result_is_deterministic_and_independent_of_the_order_of_flags() -> None:
    rng = random.Random(7)
    for _ in range(200):
        inputs = _random_inputs(rng)
        flags = list(inputs.risk_flags or ())
        rng.shuffle(flags)
        shuffled = RaerInputs(
            inputs.expected_return, inputs.quality, tuple(flags), inputs.operating_trend
        )
        assert _compose(inputs) == _compose(shuffled)
        assert _compose(inputs) == _compose(inputs)


def test_adding_a_risk_never_raises_the_value_and_a_better_quality_never_lowers_it() -> None:
    rng = random.Random(11)
    for _ in range(300):
        inputs = _random_inputs(rng)
        flags = set(inputs.risk_flags or ())
        extra = rng.choice([f for f in _ALL_FLAGS if f not in flags] or _ALL_FLAGS)
        more = RaerInputs(
            inputs.expected_return, inputs.quality, flags | {extra}, inputs.operating_trend
        )
        assert _value(_compose(more)) <= _value(_compose(inputs))
        quality = inputs.quality
        assert quality is not None
        better_q = QualityInput(
            min(D(50), quality.holding_quality_score_0_50 + D(5)),
            quality.coverage_ratio,
            quality.min_coverage,
        )
        better = RaerInputs(
            inputs.expected_return, better_q, inputs.risk_flags, inputs.operating_trend
        )
        assert _value(_compose(better)) >= _value(_compose(inputs))


def test_a_lower_fair_value_confidence_never_raises_the_value() -> None:
    rng = random.Random(13)
    for _ in range(200):
        inputs = _random_inputs(rng)
        er = inputs.expected_return
        values = []
        for level in (CL.HIGH, CL.MEDIUM, CL.LOW):
            changed = RaerInputs(
                _er(er.upside.value, er.income.value, level),
                inputs.quality,
                inputs.risk_flags,
                inputs.operating_trend,
            )
            values.append(_value(_compose(changed)))
        assert values[0] >= values[1] >= values[2]


def test_a_worse_trend_never_raises_the_value() -> None:
    values = [
        _value(_compose(_inputs(operating_trend=_trend(s, s))))
        for s in (80.0, 20.0, 0.0, -20.0, -80.0)
    ]
    assert values == sorted(values, reverse=True) or all(
        a >= b for a, b in itertools.pairwise(values)
    )


# =================================================================================================
# (2) 欠測を 0 で補完しない・算出不能
# =================================================================================================


def _status(evaluation: RaerEvaluation, axis: AxisCode) -> AxisAdjustment:
    return evaluation.axes[AXIS_ORDER.index(axis)]


@pytest.mark.parametrize(
    ("upside", "income", "expected"),
    [
        (None, 3.0, {RaerReasonCode.EXPECTED_RETURN_UPSIDE_UNDETERMINED}),
        (5.9, None, {RaerReasonCode.EXPECTED_RETURN_INCOME_UNDETERMINED}),
        (
            None,
            None,
            {
                RaerReasonCode.EXPECTED_RETURN_UPSIDE_UNDETERMINED,
                RaerReasonCode.EXPECTED_RETURN_INCOME_UNDETERMINED,
            },
        ),
    ],
)
def test_a_missing_base_makes_the_raer_unavailable_and_never_sums_one_side(
    upside: float | None, income: float | None, expected: set[RaerReasonCode]
) -> None:
    evaluation = _compose(_inputs(expected_return=_er(upside, income)))
    assert evaluation.composition is None
    assert evaluation.state is RaerState.UNAVAILABLE
    assert set(evaluation.reasons) == expected | {RaerReasonCode.BASE_UNDETERMINED}
    assert len(evaluation.axes) == 5  # 算出不能でも 5 軸の状態を必ず記録する
    result = to_raer_result(evaluation)
    assert (
        result.state is RaerState.UNAVAILABLE
        and result.value is None
        and result.comparison_key is None
    )


def test_a_missing_required_axis_makes_the_raer_unavailable() -> None:
    evaluation = _compose(_inputs(expected_return=_er(confidence=None)))
    assert evaluation.composition is None
    assert RaerReasonCode.REQUIRED_AXIS_MISSING in evaluation.reasons
    assert _status(evaluation, A.FAIR_VALUE_CONFIDENCE).status is AxisStatus.UNAVAILABLE
    assert _status(evaluation, A.FAIR_VALUE_CONFIDENCE).reasons == (AxisReasonCode.INPUT_MISSING,)


def test_fewer_applied_axes_than_the_minimum_makes_the_raer_unavailable() -> None:
    # 軸 1 だけ適用(軸 2・3・5 が欠測 = 適用 1 軸 < 3)
    evaluation = _compose(_inputs(quality=None, risk_flags=None, operating_trend=None))
    assert evaluation.composition is None
    assert RaerReasonCode.AXES_BELOW_MINIMUM in evaluation.reasons
    assert evaluation.axes_applied == {A.FAIR_VALUE_CONFIDENCE}


@pytest.mark.parametrize("applied_extra", [0, 1, 2])
def test_the_minimum_applied_axes_boundary(applied_extra: int) -> None:
    extras = [
        {"quality": None, "risk_flags": None, "operating_trend": None},
        {"quality": None, "operating_trend": None},
        {"operating_trend": None},
    ][applied_extra]
    evaluation = _compose(_inputs(**extras))
    applied = 1 + applied_extra
    assert (evaluation.composition is not None) is (applied >= 3)


def test_the_minimum_can_be_changed_by_the_coefficient_and_the_boundary_is_inclusive() -> None:
    evaluation = _compose(_inputs(quality=None, operating_trend=None), min_applied_axes=2)
    assert evaluation.composition is not None and len(evaluation.axes_applied) == 2


def test_a_missing_axis_is_recorded_with_its_reason_and_never_filled_with_zero() -> None:
    evaluation = _compose(_inputs(quality=None))
    assert _status(evaluation, A.COMPANY_QUALITY).status is AxisStatus.UNAVAILABLE
    assert _status(evaluation, A.COMPANY_QUALITY).reasons == (AxisReasonCode.INPUT_MISSING,)
    composition = evaluation.composition
    assert composition is not None
    assert A.COMPANY_QUALITY not in {c.axis for c in composition.contributions}
    # 『欠測を 0 点として算入した』のと同じ結果にならない: Q = 0 は最大の減点 4(欠測は減点なし)
    zero_quality = _compose(_inputs(quality=_quality(score=0)))
    assert _value(zero_quality) < composition.value


def test_the_data_quality_axis_is_never_applied() -> None:
    adjustment = _status(_compose(), A.DATA_QUALITY)
    assert adjustment.status is AxisStatus.UNAVAILABLE
    assert adjustment.reasons == (AxisReasonCode.AXIS_NOT_IMPLEMENTED,)


def test_a_risk_observation_none_is_unknown_and_an_empty_set_is_checked_no_risk() -> None:
    unknown = _compose(_inputs(risk_flags=None))
    assert _status(unknown, A.BUSINESS_RISK).reasons == (AxisReasonCode.RISK_FLAGS_NOT_OBSERVED,)
    assert unknown.risk_flags_available is False
    checked = _compose(_inputs(risk_flags=frozenset()))
    assert _status(checked, A.BUSINESS_RISK).status is AxisStatus.APPLIED
    assert checked.risk_flags_available is True
    assert _contribution(checked, A.BUSINESS_RISK) == D(0)


def test_the_state_is_always_provisional_while_the_data_quality_axis_is_not_implemented() -> None:
    rng = random.Random(3)
    for _ in range(100):
        composition = _compose(_random_inputs(rng)).composition
        assert composition is not None and composition.state is RaerState.PROVISIONAL


# =================================================================================================
# 軸 3: 重複の除外と上限
# =================================================================================================


def _risk(flags: set[RiskFlag], **coef: Any) -> tuple[D, dict[RiskFlag, RiskContributionStatus], D]:
    evaluation = _compose(_inputs(risk_flags=flags), **coef)
    composition = evaluation.composition
    assert composition is not None
    return (
        _contribution(evaluation, A.BUSINESS_RISK),
        {i.flag: i.status for i in composition.risk_items},
        composition.risk_cap_cut_pct,
    )


def test_the_three_day_earnings_window_excludes_the_seven_day_one() -> None:
    value, statuses, _ = _risk(
        {F.EARNINGS_WITHIN_3_BUSINESS_DAYS, F.EARNINGS_WITHIN_7_BUSINESS_DAYS}
    )
    assert value == D("3.0")
    assert statuses[F.EARNINGS_WITHIN_7_BUSINESS_DAYS] is RiskContributionStatus.EXCLUDED_DUPLICATE
    assert statuses[F.EARNINGS_WITHIN_3_BUSINESS_DAYS] is RiskContributionStatus.COUNTED


def test_the_seven_day_window_alone_is_counted() -> None:
    assert _risk({F.EARNINGS_WITHIN_7_BUSINESS_DAYS})[0] == D("1.5")


def test_cyclical_and_temporary_boost_count_only_the_larger() -> None:
    value, statuses, _ = _risk({F.CYCLICAL_INDUSTRY, F.TEMPORARY_EARNINGS_BOOST_RISK})
    assert value == D("2.0")
    assert statuses[F.CYCLICAL_INDUSTRY] is RiskContributionStatus.EXCLUDED_DUPLICATE


def test_a_tie_between_cyclical_and_temporary_boost_keeps_the_earlier_flag() -> None:
    deductions = {**dict(approved_coefficients().risk_deductions), F.CYCLICAL_INDUSTRY: D("2.0")}
    value, statuses, _ = _risk(
        {F.CYCLICAL_INDUSTRY, F.TEMPORARY_EARNINGS_BOOST_RISK}, risk_deductions=deductions
    )
    assert value == D("2.0")
    assert statuses[F.CYCLICAL_INDUSTRY] is RiskContributionStatus.COUNTED
    assert statuses[F.TEMPORARY_EARNINGS_BOOST_RISK] is RiskContributionStatus.EXCLUDED_DUPLICATE


def test_cyclical_and_major_customer_are_both_counted() -> None:
    """既存の MarginRiskCategory(同一カテゴリは最大値のみ)とは別の規則。承認済みの暫定式に従う。"""
    value, statuses, _ = _risk({F.CYCLICAL_INDUSTRY, F.MAJOR_CUSTOMER_DEPENDENCY})
    assert value == D("3.0")
    assert set(statuses.values()) == {RiskContributionStatus.COUNTED}


def test_the_cap_cuts_the_total_and_records_the_cut_amount() -> None:
    value, _, cut = _risk(set(RiskFlag))
    # 3.0(3 日)+ 1.5(大口)+ 2.0(一時増益。景気は除外)+ 2.0(小型)= 8.5 -> 上限 6
    assert value == D(6) and cut == D("2.5")


def test_the_total_exactly_at_the_cap_is_not_cut() -> None:
    value, _, cut = _risk(
        {
            F.EARNINGS_WITHIN_3_BUSINESS_DAYS,
            F.MAJOR_CUSTOMER_DEPENDENCY,
            F.SMALL_CAP_OR_LOW_LIQUIDITY,
        }
        - {F.SMALL_CAP_OR_LOW_LIQUIDITY},
        risk_deduction_cap=D("4.5"),
    )
    assert value == D("4.5") and cut == D(0)


def test_every_observed_flag_is_recorded_in_the_vocabulary_order() -> None:
    evaluation = _compose(_inputs(risk_flags=set(RiskFlag)))
    composition = evaluation.composition
    assert composition is not None
    assert [i.flag for i in composition.risk_items] == list(RiskFlag)
    assert evaluation.risk_flags == frozenset(RiskFlag)


# =================================================================================================
# (3) R1: 軸 5 の適用条件
# =================================================================================================


def test_a_not_evaluated_earnings_trend_never_yields_a_score() -> None:
    result = _trend(state=ES.NOT_EVALUATED)
    assert result == TrendUnavailable(AxisStatus.UNAVAILABLE, AxisReasonCode.COVERAGE_BELOW_MINIMUM)
    evaluation = _compose(_inputs(operating_trend=result))
    assert _status(evaluation, A.EARNINGS_TREND).reasons == (AxisReasonCode.COVERAGE_BELOW_MINIMUM,)
    assert A.EARNINGS_TREND not in {c.axis for c in evaluation.composition.contributions}  # type: ignore[union-attr]


def test_a_not_applicable_earnings_trend_is_not_applied() -> None:
    result = _trend(state=ES.NOT_APPLICABLE)
    assert result == TrendUnavailable(
        AxisStatus.NOT_APPLIED, AxisReasonCode.NOT_APPLICABLE_TO_STOCK
    )
    evaluation = _compose(_inputs(operating_trend=result))
    assert _status(evaluation, A.EARNINGS_TREND).status is AxisStatus.NOT_APPLIED


def test_components_are_ignored_when_the_evaluation_state_is_not_evaluated() -> None:
    """既存機構は NOT_EVALUATED でも成分を載せ続ける。成分があっても s
    を作らない(評価不能から減点を作らない)。"""
    result = _trend(income=-100.0, cashflow=-100.0, state=ES.NOT_EVALUATED)
    assert isinstance(result, TrendUnavailable)


@pytest.mark.parametrize(
    ("income", "cashflow", "components", "coverage"),
    [
        (
            10.0,
            20.0,
            {TrendComponent.OPERATING_INCOME, TrendComponent.OPERATING_CASHFLOW},
            D("0.700000"),
        ),
        (10.0, None, {TrendComponent.OPERATING_INCOME}, D("0.400000")),
        (
            None,
            20.0,
            {TrendComponent.OPERATING_CASHFLOW},
            D("0.300000"),
        ),  # 既存の『< 0.3』と同じ境界で適用
    ],
)
def test_the_partial_coverage_and_the_components_used_are_recorded(
    income: float | None, cashflow: float | None, components: set[TrendComponent], coverage: D
) -> None:
    result = _trend(income, cashflow)
    assert isinstance(result, OperatingTrendScore)
    assert result.components_used == components
    assert result.partial_coverage == coverage


def test_no_component_is_unavailable_for_missing_input() -> None:
    assert _trend(None, None) == TrendUnavailable(
        AxisStatus.UNAVAILABLE, AxisReasonCode.INPUT_MISSING
    )


def test_the_partial_coverage_gate_uses_the_existing_threshold_exactly() -> None:
    # 営業 CF のみ = 0.75 / 2.5 = 0.3。下限 0.3 は適用 / 0.30001 は非適用(除算せず厳密に比べる)
    assert isinstance(_trend(None, 20.0, minimum=0.3), OperatingTrendScore)
    assert _trend(None, 20.0, minimum=0.30001) == TrendUnavailable(
        AxisStatus.UNAVAILABLE, AxisReasonCode.COVERAGE_BELOW_MINIMUM
    )
    assert _trend(10.0, None, minimum=0.4) != TrendUnavailable(
        AxisStatus.UNAVAILABLE, AxisReasonCode.COVERAGE_BELOW_MINIMUM
    )


def test_the_score_is_the_weighted_average_rounded_once_to_six_places() -> None:
    result = _trend(37.5, -20.0)  # (1.0 x 37.5 + 0.75 x -20.0) / 1.75 = 12.857142857...
    assert isinstance(result, OperatingTrendScore)
    assert result.value == D("12.857143")
    tie = _trend(0.0000005, 0.0000005)  # 偶数丸め
    assert isinstance(tie, OperatingTrendScore) and tie.value == D("0.000000")


def test_the_score_assembly_records_the_weights_and_the_model_version() -> None:
    result = _trend()
    assert isinstance(result, OperatingTrendScore)
    assert result.weights == _WEIGHTS and result.earnings_trend_model_version == "earnings_trend_v3"
    provenance = dict(
        _contribution_provenance(_compose(_inputs(operating_trend=result)), A.EARNINGS_TREND)
    )
    assert provenance["components_used"] == "OPERATING_CASHFLOW,OPERATING_INCOME"
    assert provenance["partial_coverage"] == "0.7"
    assert provenance["min_coverage_required"] == "0.3"
    assert provenance["weight_operating_income"] == "1"
    assert provenance["earnings_trend_model_version"] == "earnings_trend_v3"


def _contribution_provenance(
    evaluation: RaerEvaluation, axis: AxisCode
) -> tuple[tuple[str, str], ...]:
    assert evaluation.composition is not None
    (found,) = [c for c in evaluation.composition.contributions if c.axis is axis]
    return found.provenance


@pytest.mark.parametrize("bad", [101.0, -100.5, float("nan"), float("inf"), True, "10", object()])
def test_an_invalid_trend_component_is_rejected(bad: object) -> None:
    with pytest.raises(ValueError):
        _trend(income=bad)


def test_the_trend_function_does_not_accept_the_dividend_or_acceleration_components() -> None:
    import inspect

    names = set(inspect.signature(trend_score_for_raer).parameters)
    assert not {n for n in names if "dividend" in n or "acceleration" in n}


def test_the_evaluation_state_is_mandatory_and_typed() -> None:
    import inspect

    parameter = inspect.signature(trend_score_for_raer).parameters["evaluation_state"]
    assert (
        parameter.default is inspect.Parameter.empty
        and parameter.kind is inspect.Parameter.KEYWORD_ONLY
    )
    with pytest.raises(ValueError):
        trend_score_for_raer(
            evaluation_state="EVALUATED",  # type: ignore[arg-type]
            operating_income_component=1.0,
            operating_cashflow_component=1.0,
            weights=_WEIGHTS,
            min_coverage_required=0.3,
        )


# =================================================================================================
# (3) R2: 軸 2 の充足度
# =================================================================================================


def test_a_quality_below_the_coverage_minimum_is_unavailable_not_low() -> None:
    evaluation = _compose(_inputs(quality=_quality(score=5, coverage=0.59, minimum=0.6)))
    assert _status(evaluation, A.COMPANY_QUALITY).status is AxisStatus.UNAVAILABLE
    assert _status(evaluation, A.COMPANY_QUALITY).reasons == (
        AxisReasonCode.COVERAGE_BELOW_MINIMUM,
    )


def test_the_quality_coverage_boundary_is_inclusive() -> None:
    evaluation = _compose(_inputs(quality=_quality(score=5, coverage=0.6, minimum=0.6)))
    assert _status(evaluation, A.COMPANY_QUALITY).status is AxisStatus.APPLIED


def test_an_applied_quality_records_its_coverage_and_threshold() -> None:
    provenance = dict(_contribution_provenance(_compose(), A.COMPANY_QUALITY))
    assert provenance["coverage_ratio"] == "0.8" and provenance["min_coverage"] == "0.6"
    assert provenance["quality_score_0_50"] == "30"


@pytest.mark.parametrize("score", [62, 50.1, -0.1, 100, float("nan"), float("inf"), True, "30"])
def test_a_quality_outside_zero_to_fifty_is_rejected_so_the_buy_side_scale_cannot_slip_in(
    score: object,
) -> None:
    with pytest.raises(ValueError):
        QualityInput(score, D("0.8"), D("0.6"))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("coverage", "minimum"), [(1.1, 0.6), (-0.1, 0.6), (0.8, 1.1), (0.8, -0.1)]
)
def test_the_coverage_and_its_threshold_must_be_ratios(coverage: float, minimum: float) -> None:
    with pytest.raises(ValueError):
        QualityInput(D(30), to_decimal(coverage), to_decimal(minimum))


def test_the_quality_input_requires_the_coverage_and_the_threshold() -> None:
    with pytest.raises(TypeError):
        QualityInput(D(30))  # type: ignore[call-arg]


# =================================================================================================
# (3) R3: Decimal の取り込みと丸め
# =================================================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0.1, D("0.1")), (5.015000000000001, D("5.015000000000001")), (3, D(3)), (D("1.5"), D("1.5"))],
)
def test_to_decimal_imports_a_float_through_its_repr(raw: object, expected: D) -> None:
    assert to_decimal(raw) == expected


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), True, "1.0", None, [1]])
def test_to_decimal_rejects_non_numbers_and_non_finite_values(bad: object) -> None:
    with pytest.raises(ValueError):
        to_decimal(bad)


def test_only_the_score_division_may_round() -> None:
    """丸めを許す context はスコアの除算の 1 か所だけ。他の演算は Inexact をトラップする context
    で厳密。"""
    strict = exact_decimal_context()
    assert strict.traps[Inexact]
    with localcontext(strict), pytest.raises(Inexact):
        D(1) / D(3)
    assert not exact_decimal_context(allow_inexact=True).traps[Inexact]
    rng = random.Random(5)
    for _ in range(300):
        _compose(_random_inputs(rng))  # 式の計算が Inexact を起こさない(起これば例外)


def test_a_quality_max_score_that_does_not_divide_exactly_is_rejected() -> None:
    with pytest.raises(ValueError):
        approved_coefficients(quality_max_score=D(30))
    assert approved_coefficients(quality_max_score=D(40)).quality_max_score == D(40)


# =================================================================================================
# (3) R4: 不変条件 I1〜I4
# =================================================================================================


def _composition_kwargs(**overrides: Any) -> dict[str, Any]:
    base = _compose().composition
    assert base is not None
    values: dict[str, Any] = {
        "upside_pct": base.upside_pct,
        "income_pct": base.income_pct,
        "base_pct": base.base_pct,
        "value": base.value,
        "contributions": base.contributions,
        "risk_items": base.risk_items,
        "risk_cap_cut_pct": base.risk_cap_cut_pct,
        "spec_flags": base.spec_flags,
        "coefficient_set_id": base.coefficient_set_id,
        "confidence": base.confidence,
        "state": base.state,
    }
    values.update(overrides)
    return values


def test_a_well_formed_composition_can_be_rebuilt() -> None:
    RaerComposition(**_composition_kwargs())


def test_i4_the_identity_is_enforced_including_a_flipped_sign() -> None:
    kwargs = _composition_kwargs()
    with pytest.raises(ValueError):
        RaerComposition(**{**kwargs, "value": kwargs["value"] + D("0.000001")})
    flipped = kwargs["base_pct"] + sum((c.pct_points for c in kwargs["contributions"]), start=D(0))
    with pytest.raises(ValueError):
        RaerComposition(**{**kwargs, "value": flipped})


def test_i4_the_base_must_be_the_sum_of_upside_and_income() -> None:
    kwargs = _composition_kwargs()
    with pytest.raises(ValueError):
        RaerComposition(**{**kwargs, "base_pct": kwargs["base_pct"] + D(1)})


def test_i2_a_negative_contribution_is_rejected() -> None:
    with pytest.raises(ValueError):
        AxisContribution(A.COMPANY_QUALITY, D("-0.1"))


def test_i1_a_contribution_for_a_not_applied_axis_is_rejected_even_when_zero() -> None:
    """未適用の軸に 0 の寄与度を足しても恒等式は成り立つが、『減点 0
    と判定した』と区別できなくなる。"""
    composition = _compose(_inputs(quality=None)).composition
    assert composition is not None
    padded = (*composition.contributions, AxisContribution(A.DATA_QUALITY, D(0)))
    padded = tuple(sorted(padded, key=lambda c: AXIS_ORDER.index(c.axis)))
    kwargs = _composition_kwargs(contributions=padded)
    bad = RaerComposition(
        **{
            **kwargs,
            "value": composition.value,
            "risk_items": composition.risk_items,
            "risk_cap_cut_pct": composition.risk_cap_cut_pct,
            "confidence": composition.confidence,
            "base_pct": composition.base_pct,
            "upside_pct": composition.upside_pct,
            "income_pct": composition.income_pct,
        }
    )
    evaluation = _compose(_inputs(quality=None))
    with pytest.raises(ValueError, match="寄与度を持つ軸"):
        RaerEvaluation(
            model_version=evaluation.model_version,
            evaluation_date=_DATE,
            expected_return=evaluation.expected_return,
            axes=evaluation.axes,
            risk_flags=evaluation.risk_flags,
            risk_flags_available=evaluation.risk_flags_available,
            reasons=(),
            composition=bad,
        )


def test_i1_dropping_an_applied_axis_contribution_is_rejected() -> None:
    evaluation = _compose()
    composition = evaluation.composition
    assert composition is not None
    kept = tuple(c for c in composition.contributions if c.axis is not A.COMPANY_QUALITY)
    total = sum((c.pct_points for c in kept), start=D(0))
    shrunk = RaerComposition(
        **{**_composition_kwargs(contributions=kept), "value": composition.base_pct - total}
    )
    with pytest.raises(ValueError, match="寄与度を持つ軸"):
        RaerEvaluation(
            model_version=evaluation.model_version,
            evaluation_date=_DATE,
            expected_return=evaluation.expected_return,
            axes=evaluation.axes,
            risk_flags=evaluation.risk_flags,
            risk_flags_available=evaluation.risk_flags_available,
            reasons=(),
            composition=shrunk,
        )


def test_applied_zero_contributions_are_allowed_and_distinguishable_from_not_applied() -> None:
    evaluation = _compose(
        _inputs(
            expected_return=_er(-4.0, 3.0),
            risk_flags=frozenset(),
            operating_trend=_trend(10.0, 10.0),
        )
    )
    composition = evaluation.composition
    assert composition is not None
    zero_axes = {c.axis for c in composition.contributions if c.pct_points == 0}
    assert zero_axes == {A.FAIR_VALUE_CONFIDENCE, A.BUSINESS_RISK, A.EARNINGS_TREND}
    assert A.DATA_QUALITY not in {c.axis for c in composition.contributions}


def test_i3_the_risk_contribution_must_match_the_counted_items_minus_the_cut() -> None:
    composition = _compose(_inputs(risk_flags=set(RiskFlag))).composition
    assert composition is not None
    kwargs = _composition_kwargs(
        contributions=composition.contributions,
        risk_items=composition.risk_items,
        risk_cap_cut_pct=composition.risk_cap_cut_pct,
        value=composition.value,
        base_pct=composition.base_pct,
        upside_pct=composition.upside_pct,
        income_pct=composition.income_pct,
        confidence=composition.confidence,
    )
    RaerComposition(**kwargs)
    with pytest.raises(ValueError):
        RaerComposition(**{**kwargs, "risk_cap_cut_pct": composition.risk_cap_cut_pct + D("0.1")})
    excluded_counted = tuple(
        RiskItem(i.flag, i.deduction_pct, RiskContributionStatus.COUNTED)
        for i in composition.risk_items
    )
    with pytest.raises(ValueError):
        RaerComposition(**{**kwargs, "risk_items": excluded_counted})


def test_a_composition_requires_the_horizon_flag_the_identifier_and_a_valid_state() -> None:
    with pytest.raises(ValueError):
        RaerComposition(**_composition_kwargs(spec_flags=frozenset()))
    with pytest.raises(ValueError):
        RaerComposition(**_composition_kwargs(coefficient_set_id=""))
    with pytest.raises(ValueError):
        RaerComposition(**_composition_kwargs(state=RaerState.UNAVAILABLE))
    with pytest.raises(ValueError):
        RaerComposition(
            **_composition_kwargs(state=RaerState.AVAILABLE)
        )  # 軸 4 が無いので 5 軸ではない


def test_an_evaluation_with_a_composition_must_use_the_combined_model_version() -> None:
    evaluation = _compose()
    with pytest.raises(ValueError):
        RaerEvaluation(
            model_version=MODEL_VERSION,
            evaluation_date=_DATE,
            expected_return=evaluation.expected_return,
            axes=evaluation.axes,
            risk_flags=evaluation.risk_flags,
            risk_flags_available=evaluation.risk_flags_available,
            reasons=(),
            composition=evaluation.composition,
        )


@pytest.mark.parametrize(
    "reasons",
    [
        (),
        (RaerReasonCode.COMPOSITION_RULE_NOT_DECIDED,),
        (RaerReasonCode.EXPECTED_RETURN_UPSIDE_UNDETERMINED,),  # 決定的な理由が無い
    ],
)
def test_a_combined_unavailable_evaluation_needs_a_decisive_reason(
    reasons: tuple[RaerReasonCode, ...],
) -> None:
    evaluation = _compose(_inputs(expected_return=_er(None, 3.0)))
    with pytest.raises(ValueError):
        RaerEvaluation(
            model_version=COMBINED_MODEL_VERSION,
            evaluation_date=_DATE,
            expected_return=evaluation.expected_return,
            axes=evaluation.axes,
            risk_flags=evaluation.risk_flags,
            risk_flags_available=evaluation.risk_flags_available,
            reasons=reasons,
        )


def test_the_b1_structure_contract_is_unchanged() -> None:
    """構造版(composition なし)は従来どおり: 値を作らず、COMPOSITION_RULE_NOT_DECIDED を持つ。"""
    from jstock_advisor.domain.valuation.raer import evaluate_raer

    evaluation = evaluate_raer(
        evaluation_date=_DATE, expected_return=_er(), axes={}, risk_flags=None
    )
    assert evaluation.model_version == MODEL_VERSION and evaluation.composition is None
    assert evaluation.state is RaerState.UNAVAILABLE
    assert RaerReasonCode.COMPOSITION_RULE_NOT_DECIDED in evaluation.reasons


# =================================================================================================
# (4) 信頼度のラベル(X-2)・比較可能性
# =================================================================================================


@pytest.mark.parametrize(
    ("fair_value", "applied", "expected"),
    [
        (CL.HIGH, 4, CL.MEDIUM),  # PROVISIONAL の間は最大 MEDIUM
        (CL.MEDIUM, 4, CL.MEDIUM),
        (CL.LOW, 4, CL.LOW),
        (CL.HIGH, 3, CL.LOW),  # 適用軸 3 以下は LOW
        (CL.MEDIUM, 3, CL.LOW),
        (CL.LOW, 3, CL.LOW),
        (CL.HIGH, 2, CL.LOW),
    ],
)
def test_the_confidence_label_rule(fair_value: CL, applied: int, expected: CL) -> None:
    assert (
        resolve_raer_confidence(fair_value, state=RaerState.PROVISIONAL, applied_axes=applied)
        is expected
    )


def test_high_is_unreachable_so_only_low_and_medium_are_observed() -> None:
    reachable = {
        resolve_raer_confidence(level, state=RaerState.PROVISIONAL, applied_axes=n)
        for level in CL
        for n in range(1, 5)
    }
    assert reachable == {CL.LOW, CL.MEDIUM}
    rng = random.Random(17)
    seen = {_compose(_random_inputs(rng)).composition.confidence for _ in range(300)}  # type: ignore[union-attr]
    assert seen <= {CL.LOW, CL.MEDIUM}


def test_the_confidence_label_never_changes_the_value() -> None:
    rng = random.Random(19)
    for _ in range(100):
        inputs = _random_inputs(rng)
        evaluation = _compose(inputs)
        assert evaluation.composition is not None
        assert evaluation.composition.confidence in {CL.LOW, CL.MEDIUM}


def test_the_confidence_resolver_rejects_an_unavailable_state_and_a_wrong_type() -> None:
    with pytest.raises(ValueError):
        resolve_raer_confidence(CL.LOW, state=RaerState.UNAVAILABLE, applied_axes=4)
    with pytest.raises(ValueError):
        resolve_raer_confidence(
            "LOW",  # type: ignore[arg-type]
            state=RaerState.PROVISIONAL,
            applied_axes=4,
        )


def test_a_provisional_result_is_not_comparable_with_a_result_that_applies_five_axes() -> None:
    """PROVISIONAL は未適用軸の減点を含まず上振れしうる。comparison_key
    が適用軸を含むため混ざらない。"""
    four = to_raer_result(_compose())
    three = to_raer_result(_compose(_inputs(operating_trend=None)))
    assert four.comparison_key != three.comparison_key
    assert not are_comparable(four, three)
    same = to_raer_result(_compose(_inputs(expected_return=_er(2.0, 1.0))))
    assert are_comparable(four, same)


def test_the_raer_result_projection_carries_the_value_and_the_records() -> None:
    evaluation = _compose(_inputs(risk_flags={F.MAJOR_CUSTOMER_DEPENDENCY}))
    result = to_raer_result(evaluation)
    assert result.state is RaerState.PROVISIONAL and result.value == _value(evaluation)
    assert result.confidence is CL.MEDIUM
    assert result.model_version == COMBINED_MODEL_VERSION
    assert result.comparison_key == evaluation.comparison_key
    assert (
        result.risk_flags == {"major_customer_dependency"} and result.risk_flags_available is True
    )
    assert "DATA_QUALITY:AXIS_NOT_IMPLEMENTED" in result.reason_codes
    assert result.axes_applied == {a.value for a in evaluation.axes_applied}


def test_the_combined_model_version_is_a_pinned_literal() -> None:
    assert COMBINED_MODEL_VERSION == "raer-combined-v1-provisional-axes-y"
    assert _compose().model_version == "raer-combined-v1-provisional-axes-y"


# =================================================================================================
# (5) 係数と識別子
# =================================================================================================


def test_the_coefficient_set_id_of_the_approved_values_is_a_pinned_literal() -> None:
    assert approved_coefficients().coefficient_set_id == _APPROVED_COEFFICIENT_SET_ID
    assert _compose().composition.coefficient_set_id == _APPROVED_COEFFICIENT_SET_ID  # type: ignore[union-attr]


def test_the_identifier_does_not_depend_on_key_order_or_on_how_a_number_is_written() -> None:
    reordered = approved_coefficients(
        fv_confidence_factor={CL.LOW: D("0.600"), CL.HIGH: D("1"), CL.MEDIUM: D("0.850")},
        risk_deductions=dict(reversed(list(approved_coefficients().risk_deductions.items()))),
        quality_deduction_max=D("4.0"),
    )
    assert reordered.coefficient_set_id == _APPROVED_COEFFICIENT_SET_ID


@pytest.mark.parametrize(
    "override",
    [
        {"fv_confidence_factor": {CL.HIGH: D("1.00"), CL.MEDIUM: D("0.86"), CL.LOW: D("0.60")}},
        {"quality_deduction_max": D(5)},
        {"quality_max_score": D(40)},
        {"risk_deduction_cap": D(7)},
        {"trend_deduction_scale": D(4)},
        {"min_applied_axes": 2},
        {"required_axes": frozenset({A.FAIR_VALUE_CONFIDENCE, A.COMPANY_QUALITY})},
        {
            "risk_deductions": {
                **dict(approved_coefficients().risk_deductions),
                F.CYCLICAL_INDUSTRY: D("1.6"),
            }
        },
    ],
    ids=lambda o: next(iter(o)),
)
def test_changing_any_coefficient_changes_the_identifier(override: dict[str, Any]) -> None:
    assert approved_coefficients(**override).coefficient_set_id != _APPROVED_COEFFICIENT_SET_ID


def test_the_identifier_includes_the_model_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc, "COMBINED_MODEL_VERSION", "raer-combined-v2-provisional-axes-y")
    assert approved_coefficients().coefficient_set_id != _APPROVED_COEFFICIENT_SET_ID


@pytest.mark.parametrize(
    "override",
    [
        {"fv_confidence_factor": {CL.HIGH: D(1), CL.MEDIUM: D("0.85")}},  # LOW 欠落
        {
            "fv_confidence_factor": {CL.HIGH: D("0.5"), CL.MEDIUM: D("0.85"), CL.LOW: D("0.6")}
        },  # 逆順
        {
            "fv_confidence_factor": {CL.HIGH: D("1.1"), CL.MEDIUM: D("0.85"), CL.LOW: D("0.6")}
        },  # > 1
        {"fv_confidence_factor": {CL.HIGH: D(1), CL.MEDIUM: D("0.85"), CL.LOW: D(0)}},  # 0
        {"quality_deduction_max": D(-1)},
        {"quality_max_score": D(0)},
        {"risk_deduction_cap": D(-1)},
        {"trend_deduction_scale": D(-1)},
        {"min_applied_axes": 0},
        {"min_applied_axes": 6},
        {"min_applied_axes": True},
        {"required_axes": frozenset({"MADE_UP"})},
        {"risk_deductions": {F.CYCLICAL_INDUSTRY: D(1)}},  # 6 語が揃わない(0 で補わない)
        {
            "risk_deductions": {
                **dict(approved_coefficients().risk_deductions),
                F.CYCLICAL_INDUSTRY: D(-1),
            }
        },
        {"quality_deduction_max": float("nan")},
        {"risk_deduction_cap": "6"},
    ],
    ids=lambda o: f"{next(iter(o))}-{str(list(o.values())[0])[:20]}",
)
def test_an_invalid_coefficient_is_rejected(override: dict[str, Any]) -> None:
    with pytest.raises((ValueError, TypeError)):
        approved_coefficients(**override)


# =================================================================================================
# 入力の不正(ValueError)
# =================================================================================================


def test_the_evaluation_date_must_match_the_expected_return() -> None:
    with pytest.raises(ValueError):
        compose_raer(
            evaluation_date=date(2026, 10, 14),
            inputs=_inputs(),
            coefficients=approved_coefficients(),
        )


def test_the_inputs_and_coefficients_must_be_the_expected_types() -> None:
    with pytest.raises(ValueError):
        compose_raer(evaluation_date=_DATE, inputs=object(), coefficients=approved_coefficients())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        compose_raer(evaluation_date=_DATE, inputs=_inputs(), coefficients=object())  # type: ignore[arg-type]


def test_a_risk_flag_outside_the_vocabulary_is_rejected() -> None:
    with pytest.raises(ValueError):
        _compose(_inputs(risk_flags={"cyclical_industry"}))


def test_an_unexpected_trend_or_quality_type_is_rejected() -> None:
    with pytest.raises(ValueError):
        _compose(_inputs(operating_trend=12.5))
    with pytest.raises(ValueError):
        _compose(_inputs(quality=30))


def test_a_non_confidence_level_in_the_expected_return_is_rejected() -> None:
    bad = _er()
    object.__setattr__(bad, "fair_value_confidence", "MEDIUM")
    with pytest.raises(ValueError):
        _compose(_inputs(expected_return=bad))


def test_trend_weights_and_unavailable_markers_are_validated() -> None:
    with pytest.raises(ValueError):
        TrendWeights(D(0), D(0), D(0), D(0))
    with pytest.raises(ValueError):
        TrendWeights(D(-1), D(1), D(1), D(1))
    with pytest.raises(ValueError):
        TrendUnavailable(AxisStatus.APPLIED, AxisReasonCode.INPUT_MISSING)
    with pytest.raises(ValueError):
        OperatingTrendScore(
            D("1.0000001"),
            frozenset({TrendComponent.OPERATING_INCOME}),
            D("0.7"),
            D("0.3"),
            _WEIGHTS,
        )
    with pytest.raises(ValueError):
        OperatingTrendScore(D(1), frozenset(), D("0.7"), D("0.3"), _WEIGHTS)


# =================================================================================================
# (6) 構造: float 演算なし・時計なし・dormant
# =================================================================================================


def _tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def test_the_module_has_no_float_literal_and_no_float_call() -> None:
    tree = _tree()
    assert [
        n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, float)
    ] == []
    calls = {
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert not calls & {"float", "round", "mean", "fsum"}
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "math" not in imported and "random" not in imported and "statistics" not in imported


def test_a_decimal_is_built_from_a_float_only_inside_the_single_entry_point() -> None:
    """外部の数の取り込み口は to_decimal だけ。他では Decimal(整数 / 文字列の literal)
    だけを作る。"""
    tree = _tree()
    allowed_function = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "to_decimal"
    )
    inside = {id(n) for n in ast.walk(allowed_function)}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "Decimal"
            and id(node) not in inside
        ):
            assert len(node.args) == 1, ast.dump(node)
            argument = node.args[0]
            if isinstance(argument, ast.UnaryOp) and isinstance(argument.op, ast.USub):
                argument = argument.operand  # Decimal(-100) のような負の整数 literal
            assert isinstance(argument, ast.Constant), ast.dump(node)
            assert isinstance(argument.value, int | str)


def test_the_module_reads_no_clock_and_has_no_io() -> None:
    tree = _tree()
    called = {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert not called & {
        "now",
        "today",
        "utcnow",
        "time",
        "monotonic",
        "open",
        "print",
        "getLogger",
    }
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    forbidden = (
        "services",
        "infrastructure",
        "lambda_handlers",
        "boto3",
        "logging",
        "datetime.datetime",
    )
    assert not [m for m in imported if any(f in m for f in forbidden)]
    allowed_roots = (
        "__future__",
        "hashlib",
        "json",
        "collections.abc",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "types",
        "typing",
        "jstock_advisor.domain.entities.enums",
        "jstock_advisor.domain.valuation.expected_return",
        "jstock_advisor.domain.valuation.raer",
    )
    assert sorted(m for m in imported if m not in allowed_roots) == []


def test_the_module_does_not_know_available_cash_or_acquisition_prices() -> None:
    text = _MODULE_PATH.read_text(encoding="utf-8").lower()
    for name in ("available_cash", "acquisition", "average_price", "cost_basis", "notification"):
        assert name not in text.replace("notifications", ""), name


def test_the_type_names_defined_here_exist_only_in_this_module() -> None:
    names = {
        n.name
        for n in ast.walk(_tree())
        if isinstance(n, ast.ClassDef) and not n.name.startswith("_")
    }
    assert {"RaerCoefficients", "RaerInputs", "QualityInput", "OperatingTrendScore"} <= names
    elsewhere = []
    for path in sorted(_SRC.rglob("*.py")):
        if path == _MODULE_PATH:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef) and node.name in names:
                elsewhere.append(f"{path.relative_to(_SRC).as_posix()}:{node.name}")
    assert elsewhere == []


def test_nothing_in_src_imports_the_composition_module() -> None:
    importers = [
        path.relative_to(_SRC).as_posix()
        for path in sorted(_SRC.rglob("*.py"))
        if path != _MODULE_PATH
        and ("valuation.raer_composition" in path.read_text(encoding="utf-8"))
    ]
    # dormant。例外は、同じく未配線の dormant な module だけ: #602 PR-G1(入力の組み立て)と
    # #602 PR-E(係数の loader)。配線は #603 PR-F 系(別の USER 承認の後)
    allowed = {
        "domain/valuation/raer_coefficients_config.py",
        "domain/valuation/raer_inputs.py",
        "services/allocation_shadow_compute.py",  # #603 PR-F2-2a(RAER の計算を呼ぶ。dormant)
        # #934 BT-2a(RAER の式の再現。dormant。借りるのは RaerCoefficients の型 1 つだけ。
        # 式は本 module を呼ばず独立に再実装する)
        "domain/signals/allocation_shadow_raer_replay.py",
    }
    assert set(importers) <= allowed, importers


def test_the_composition_module_is_one_of_the_names_the_connection_gate_treats_as_unconnected() -> (
    None
):
    assert (
        "raer" in "jstock_advisor.domain.valuation.raer_composition"
    )  # #606 の名前トークンが自動で捕らえる


def test_the_contributions_are_one_per_axis_in_the_fixed_order() -> None:
    composition = _compose().composition
    assert composition is not None
    contributions = composition.contributions
    first = contributions[0]
    total = sum((c.pct_points for c in contributions), start=D(0))
    duplicated = (first, *contributions)  # 同じ軸が 2 件
    with pytest.raises(ValueError, match="1 件・固定の順序"):
        RaerComposition(
            **{
                **_composition_kwargs(contributions=duplicated),
                "value": composition.base_pct - total - first.pct_points,
            }
        )
    reversed_order = tuple(reversed(contributions))  # 固定の順序でない
    with pytest.raises(ValueError, match="1 件・固定の順序"):
        RaerComposition(**_composition_kwargs(contributions=reversed_order))
