"""domain/signals/earnings_trend.pyのテスト(判定精度向上機能Phase C、
コードレビュー対応でv2/v3へ再設計: 符号跨ぎバグ修正・raw metrics追加・
recent_periods_source反映・EarningsDecisionRelevance統合・period_end監査)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from jstock_advisor.config.models import (
    EarningsTrendCategoryThresholds,
    EarningsTrendRulesConfig,
)
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    DividendComparisonOutcome,
    EarningsDecisionRelevance,
    EarningsReleaseConfirmationState,
    EarningsTrendCategory,
    EarningsTrendEvaluationState,
    PeriodType,
    RecentPeriodsSource,
)
from jstock_advisor.domain.financial_series import FinancialPeriodValue
from jstock_advisor.domain.signals.earnings_trend import (
    earnings_trend_config_values,
    earnings_trend_result_to_metrics,
    evaluate_earnings_trend,
)

_NOW = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)


def _config(**overrides: object) -> EarningsTrendRulesConfig:
    defaults: dict[str, object] = dict(
        model_version="test-fixture",
        operating_income_trend_weight=1.0,
        operating_cashflow_trend_weight=0.75,
        dividend_direction_weight=0.5,
        acceleration_weight=0.25,
        trend_strong_decline_pct=-15.0,
        trend_decline_pct=-5.0,
        trend_improve_pct=5.0,
        trend_strong_improve_pct=15.0,
        acceleration_full_scale_pct=10.0,
        dividend_actual_cut_score=-100.0,
        dividend_forecast_cut_score=-50.0,
        dividend_maintained_score=0.0,
        dividend_increase_score=80.0,
        min_coverage_required=0.3,
        coverage_high_threshold=0.9,
        coverage_medium_threshold=0.5,
        category_thresholds=EarningsTrendCategoryThresholds(
            strong_improving=50.0, improving=15.0, deteriorating=-15.0, strong_deteriorating=-50.0
        ),
    )
    defaults.update(overrides)
    return EarningsTrendRulesConfig.model_validate(defaults)


_CONFIG = _config()


def _periods(
    values: list[Decimal],
    period_type: PeriodType = PeriodType.TTM,
    start: dt.date = dt.date(2025, 3, 31),
) -> list[FinancialPeriodValue]:
    """コードレビュー対応(v3): テスト用に、値と対応するperiod_end/period_type
    を組にしたFinancialPeriodValueの系列を組み立てる(indexだけに頼らず、
    実運用と同じ「値と期間が対になった」入力形状でevaluate_earnings_trend()を
    検証するため)。"""
    result: list[FinancialPeriodValue] = []
    period_end = start
    for value in values:
        result.append(
            FinancialPeriodValue(value=value, period_end=period_end, period_type=period_type)
        )
        period_end = period_end + dt.timedelta(days=91)
    return result


def _evaluate(
    incomes: list[Decimal],
    cashflows: list[Decimal] | None = None,
    dividend_comparison_outcome: DividendComparisonOutcome | None = None,
    recent_periods_source: RecentPeriodsSource = RecentPeriodsSource.QUARTERLY,
    release_confirmation_state: EarningsReleaseConfirmationState = (
        EarningsReleaseConfirmationState.NOT_APPLICABLE
    ),
    decision_relevance: EarningsDecisionRelevance = EarningsDecisionRelevance.NOT_RELEVANT,
    config: EarningsTrendRulesConfig | None = None,
    income_period_type: PeriodType = PeriodType.TTM,
    cashflow_period_type: PeriodType = PeriodType.TTM,
):
    return evaluate_earnings_trend(
        _periods(incomes, income_period_type),
        _periods(cashflows if cashflows is not None else [], cashflow_period_type),
        dividend_comparison_outcome,
        recent_periods_source,
        release_confirmation_state,
        decision_relevance,
        _NOW,
        config or _CONFIG,
    )


# ===== 正常系: 全成分利用可能 =====


def test_all_components_evaluated_for_improving_trend() -> None:
    incomes = [Decimal("100"), Decimal("105"), Decimal("112"), Decimal("120"), Decimal("140")]
    cashflows = [Decimal("90"), Decimal("93"), Decimal("96"), Decimal("100"), Decimal("108")]
    result = _evaluate(
        incomes, cashflows, dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE
    )
    assert result.state == EarningsTrendEvaluationState.EVALUATED
    assert result.operating_income_trend_component == 100.0  # (140/120-1)*100≈16.7%>=15%
    assert result.operating_cashflow_trend_component == 50.0  # (108/100-1)*100=8%>=5%
    assert result.dividend_direction_component == 80.0
    assert result.acceleration_component is not None
    assert result.coverage == 1.0
    assert result.confidence == ConfidenceLevel.HIGH


# ===== 営業利益/営業CFトレンド成分の段階評価(通常、正数→正数) =====


@pytest.mark.parametrize(
    ("previous", "latest", "expected"),
    [
        (Decimal("100"), Decimal("120"), 100.0),  # +20% >= strong_improve(15%)
        (Decimal("100"), Decimal("108"), 50.0),  # +8% >= improve(5%)
        (Decimal("100"), Decimal("100"), 0.0),  # 0%
        (Decimal("100"), Decimal("92"), -50.0),  # -8% > strong_decline(-15%)
        (Decimal("100"), Decimal("80"), -100.0),  # -20% <= strong_decline(-15%)
    ],
)
def test_trend_component_bands(previous: Decimal, latest: Decimal, expected: float) -> None:
    result = _evaluate([previous, latest])
    assert result.operating_income_trend_component == expected


def test_trend_component_unavailable_with_single_quarter() -> None:
    result = _evaluate([Decimal("100")])
    assert result.operating_income_trend_component is None
    assert "OPERATING_INCOME_TREND_UNAVAILABLE" in result.reason_codes


# ===== コードレビュー対応(v2): 赤字・マイナスCF時の符号跨ぎ =====


@pytest.mark.parametrize(
    ("previous", "latest", "expect_positive"),
    [
        (Decimal("-100"), Decimal("-50"), True),  # 赤字縮小 = 改善
        (Decimal("-50"), Decimal("-100"), False),  # 赤字拡大 = 悪化
        (Decimal("-100"), Decimal("10"), True),  # 黒字転換 = 強い改善
        (Decimal("100"), Decimal("-10"), False),  # 赤字転落 = 強い悪化
    ],
)
def test_operating_income_sign_crossing_directions(
    previous: Decimal, latest: Decimal, expect_positive: bool
) -> None:
    result = _evaluate([previous, latest])
    assert result.operating_income_trend_component is not None
    if expect_positive:
        assert result.operating_income_trend_component > 0
    else:
        assert result.operating_income_trend_component < 0


@pytest.mark.parametrize(
    ("previous", "latest", "expect_positive"),
    [
        (Decimal("-100"), Decimal("-50"), True),  # CFマイナス縮小 = 改善
        (Decimal("-50"), Decimal("-100"), False),  # CFマイナス拡大 = 悪化
        (Decimal("-100"), Decimal("10"), True),  # CFプラス転換 = 強い改善
        (Decimal("100"), Decimal("-10"), False),  # CFマイナス転落 = 強い悪化
    ],
)
def test_operating_cashflow_sign_crossing_directions(
    previous: Decimal, latest: Decimal, expect_positive: bool
) -> None:
    result = _evaluate([Decimal("0"), Decimal("0")], [previous, latest])
    assert result.operating_cashflow_trend_component is not None
    if expect_positive:
        assert result.operating_cashflow_trend_component > 0
    else:
        assert result.operating_cashflow_trend_component < 0


def test_black_turn_is_strong_improvement() -> None:
    result = _evaluate([Decimal("-100"), Decimal("10")])
    assert result.operating_income_trend_component == 100.0


def test_red_fall_is_strong_deterioration() -> None:
    result = _evaluate([Decimal("100"), Decimal("-10")])
    assert result.operating_income_trend_component == -100.0


def test_loss_shrink_is_improvement() -> None:
    result = _evaluate([Decimal("-100"), Decimal("-50")])
    assert result.operating_income_trend_component == 100.0  # +50% >= strong_improve


def test_loss_expand_is_deterioration() -> None:
    result = _evaluate([Decimal("-50"), Decimal("-100")])
    assert result.operating_income_trend_component == -100.0  # -100% <= strong_decline


# ===== previous=0の明示評価(コードレビュー対応v2) =====


def test_previous_zero_latest_positive_is_explicit_improvement() -> None:
    result = _evaluate([Decimal("0"), Decimal("50")])
    assert result.operating_income_trend_component == 50.0


def test_previous_zero_latest_negative_is_explicit_deterioration() -> None:
    result = _evaluate([Decimal("0"), Decimal("-50")])
    assert result.operating_income_trend_component == -50.0


def test_previous_zero_latest_zero_is_neutral() -> None:
    result = _evaluate([Decimal("0"), Decimal("0")])
    assert result.operating_income_trend_component == 0.0


# ===== acceleration成分 =====


def test_acceleration_component_positive_when_growth_accelerates() -> None:
    # 1本目→2本目: +5%、2本目→3本目: +20% (加速)
    incomes = [Decimal("100"), Decimal("105"), Decimal("126")]
    result = _evaluate(incomes)
    assert result.acceleration_component is not None
    assert result.acceleration_component > 0


def test_acceleration_component_negative_when_growth_decelerates() -> None:
    # 1本目→2本目: +20%、2本目→3本目: +5% (減速)
    incomes = [Decimal("100"), Decimal("120"), Decimal("126")]
    result = _evaluate(incomes)
    assert result.acceleration_component is not None
    assert result.acceleration_component < 0


def test_acceleration_component_unavailable_with_two_quarters() -> None:
    result = _evaluate([Decimal("100"), Decimal("110")])
    assert result.acceleration_component is None
    assert "ACCELERATION_UNAVAILABLE" in result.reason_codes


def test_acceleration_component_clamped_to_range() -> None:
    incomes = [Decimal("100"), Decimal("101"), Decimal("1000")]
    result = _evaluate(incomes)
    assert result.acceleration_component is not None
    assert -100.0 <= result.acceleration_component <= 100.0


def test_acceleration_with_sign_crossing_is_unavailable_not_extreme() -> None:
    """コードレビュー対応(v2): 黒字/赤字の符号跨ぎを含む3四半期では、
    2階差分が比較可能な意味を持たないため誤って強い正負スコアにならず、
    評価不能(None)として扱う。"""
    incomes = [Decimal("-100"), Decimal("-50"), Decimal("10")]
    result = _evaluate(incomes)
    assert result.acceleration_component is None
    assert result.acceleration_raw_pct is None
    assert "ACCELERATION_UNAVAILABLE" in result.reason_codes


def test_acceleration_with_sign_crossing_in_first_pair_is_unavailable() -> None:
    incomes = [Decimal("100"), Decimal("-10"), Decimal("5")]
    result = _evaluate(incomes)
    assert result.acceleration_component is None


# ===== acceleration期間監査(コードレビュー対応v3) =====


def test_acceleration_period_ends_recorded_when_available() -> None:
    incomes = [Decimal("100"), Decimal("105"), Decimal("126")]
    result = _evaluate(incomes)
    assert result.acceleration_period_ends is not None
    assert len(result.acceleration_period_ends) == 3
    assert result.acceleration_period_ends[2] == result.latest_operating_income_period_end
    assert result.acceleration_period_ends[1] == result.previous_operating_income_period_end


def test_acceleration_period_ends_none_with_two_quarters() -> None:
    result = _evaluate([Decimal("100"), Decimal("110")])
    assert result.acceleration_period_ends is None


# ===== Dividend Direction成分 =====


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (DividendComparisonOutcome.ACTUAL_DIVIDEND_CUT, -100.0),
        (DividendComparisonOutcome.FORECAST_DIVIDEND_CUT, -50.0),
        (DividendComparisonOutcome.DIVIDEND_MAINTAINED, 0.0),
        (DividendComparisonOutcome.DIVIDEND_INCREASE, 80.0),
    ],
)
def test_dividend_direction_component_mapping(
    outcome: DividendComparisonOutcome, expected: float
) -> None:
    result = _evaluate([Decimal("100")], dividend_comparison_outcome=outcome)
    assert result.dividend_direction_component == expected


def test_dividend_direction_component_unavailable_when_none() -> None:
    result = _evaluate([Decimal("100")], dividend_comparison_outcome=None)
    assert result.dividend_direction_component is None
    assert "DIVIDEND_DIRECTION_UNAVAILABLE" in result.reason_codes


# ===== NOT_APPLICABLE(決算反映未確認)ゲート(コードレビュー対応v3:
# EarningsDecisionRelevance統合) =====


@pytest.mark.parametrize(
    "state",
    [
        EarningsReleaseConfirmationState.AWAITING_CONFIRMATION,
        EarningsReleaseConfirmationState.DELAYED,
    ],
)
def test_not_applicable_when_awaiting_earnings_confirmation_and_relevant(
    state: EarningsReleaseConfirmationState,
) -> None:
    """1. STALE_PAST_DATE + AWAITING_CONFIRMATION + RELEVANT → NOT_APPLICABLE
    2. STALE_PAST_DATE + DELAYED + RELEVANT → NOT_APPLICABLE"""
    result = _evaluate(
        [Decimal("100"), Decimal("110")],
        release_confirmation_state=state,
        decision_relevance=EarningsDecisionRelevance.RELEVANT,
    )
    assert result.state == EarningsTrendEvaluationState.NOT_APPLICABLE
    assert result.reason_codes == ("AWAITING_EARNINGS_CONFIRMATION",)
    assert result.score is None
    assert result.earnings_decision_relevance == EarningsDecisionRelevance.RELEVANT
    assert result.release_confirmation_state == state


def test_not_applicable_when_unknown_relevance_still_continues() -> None:
    """3. STALE_PAST_DATE + DELAYED + UNKNOWN → NOT_APPLICABLEにならず評価継続。"""
    result = _evaluate(
        [Decimal("100"), Decimal("120")],
        release_confirmation_state=EarningsReleaseConfirmationState.DELAYED,
        decision_relevance=EarningsDecisionRelevance.UNKNOWN,
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
    )
    assert result.state == EarningsTrendEvaluationState.EVALUATED
    assert result.earnings_decision_relevance == EarningsDecisionRelevance.UNKNOWN


def test_not_applicable_when_not_relevant_still_continues() -> None:
    """4. STALE_PAST_DATE + AWAITING_CONFIRMATION + NOT_RELEVANT → 評価継続。"""
    result = _evaluate(
        [Decimal("100"), Decimal("120")],
        release_confirmation_state=EarningsReleaseConfirmationState.AWAITING_CONFIRMATION,
        decision_relevance=EarningsDecisionRelevance.NOT_RELEVANT,
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
    )
    assert result.state == EarningsTrendEvaluationState.EVALUATED
    assert result.earnings_decision_relevance == EarningsDecisionRelevance.NOT_RELEVANT


def test_evaluated_when_data_updated_even_if_relevant() -> None:
    """5. DATA_UPDATED → 通常評価。"""
    result = _evaluate(
        [Decimal("100"), Decimal("120")],
        release_confirmation_state=EarningsReleaseConfirmationState.DATA_UPDATED,
        decision_relevance=EarningsDecisionRelevance.RELEVANT,
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
    )
    assert result.state == EarningsTrendEvaluationState.EVALUATED


# ===== coverage/confidence/NOT_EVALUATEDゲート =====


def test_not_evaluated_when_no_components_available() -> None:
    result = _evaluate([], [], dividend_comparison_outcome=None)
    assert result.state == EarningsTrendEvaluationState.NOT_EVALUATED
    assert result.coverage == 0.0
    assert result.score is None


# ===== カテゴリ分類 =====


def test_category_strong_improving() -> None:
    result = _evaluate(
        [Decimal("100"), Decimal("120")],
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
    )
    assert result.category == EarningsTrendCategory.STRONG_IMPROVING


def test_category_strong_deteriorating() -> None:
    result = _evaluate(
        [Decimal("100"), Decimal("70")],
        dividend_comparison_outcome=DividendComparisonOutcome.ACTUAL_DIVIDEND_CUT,
    )
    assert result.category == EarningsTrendCategory.STRONG_DETERIORATING


def test_category_stable() -> None:
    result = _evaluate(
        [Decimal("100"), Decimal("100")],
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_MAINTAINED,
    )
    assert result.category == EarningsTrendCategory.STABLE


# ===== RecentPeriodsSourceとconfidence(コードレビュー対応v2) =====


def _full_income_cashflow() -> tuple[list[Decimal], list[Decimal]]:
    incomes = [Decimal("100"), Decimal("105"), Decimal("112"), Decimal("120"), Decimal("140")]
    cashflows = [Decimal("90"), Decimal("93"), Decimal("96"), Decimal("100"), Decimal("108")]
    return incomes, cashflows


def test_quarterly_source_reaches_high_confidence() -> None:
    incomes, cashflows = _full_income_cashflow()
    result = _evaluate(
        incomes,
        cashflows,
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
        recent_periods_source=RecentPeriodsSource.QUARTERLY,
    )
    assert result.coverage == 1.0
    assert result.confidence == ConfidenceLevel.HIGH
    assert "ANNUAL_FALLBACK_USED" not in result.reason_codes


def test_annual_fallback_caps_confidence_at_medium() -> None:
    incomes, cashflows = _full_income_cashflow()
    result = _evaluate(
        incomes,
        cashflows,
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
        recent_periods_source=RecentPeriodsSource.ANNUAL_FALLBACK,
    )
    assert result.coverage == 1.0
    assert result.confidence == ConfidenceLevel.MEDIUM
    assert "ANNUAL_FALLBACK_USED" in result.reason_codes


def test_score_unchanged_between_quarterly_and_annual_fallback() -> None:
    incomes, cashflows = _full_income_cashflow()
    quarterly = _evaluate(
        incomes,
        cashflows,
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
        recent_periods_source=RecentPeriodsSource.QUARTERLY,
    )
    annual = _evaluate(
        incomes,
        cashflows,
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
        recent_periods_source=RecentPeriodsSource.ANNUAL_FALLBACK,
    )
    assert quarterly.score == annual.score
    assert quarterly.operating_income_trend_component == annual.operating_income_trend_component


def test_unavailable_source_forces_financial_components_unavailable_not_zero() -> None:
    incomes, cashflows = _full_income_cashflow()
    result = _evaluate(
        incomes,
        cashflows,
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
        recent_periods_source=RecentPeriodsSource.UNAVAILABLE,
    )
    assert result.operating_income_trend_component is None
    assert result.operating_cashflow_trend_component is None
    assert result.acceleration_component is None
    assert "OPERATING_INCOME_TREND_UNAVAILABLE" in result.reason_codes
    assert "OPERATING_CASHFLOW_TREND_UNAVAILABLE" in result.reason_codes
    assert "ACCELERATION_UNAVAILABLE" in result.reason_codes
    # 配当方向のみ残るためcoverage不足でNOT_EVALUATED(0点として加算しない)。
    assert result.state == EarningsTrendEvaluationState.NOT_EVALUATED
    # UNAVAILABLE時は期間情報も推測補完せずNoneのまま(コードレビュー対応v3)。
    assert result.latest_operating_income_period_end is None
    assert result.previous_operating_income_period_end is None


# ===== raw metrics(コードレビュー対応v2/v3) =====


def test_result_holds_raw_before_after_values() -> None:
    result = _evaluate(
        [Decimal("100"), Decimal("120")],
        [Decimal("50"), Decimal("60")],
        recent_periods_source=RecentPeriodsSource.QUARTERLY,
    )
    assert result.previous_operating_income == Decimal("100")
    assert result.latest_operating_income == Decimal("120")
    assert result.operating_income_change_pct == pytest.approx(20.0)
    assert result.previous_operating_cashflow == Decimal("50")
    assert result.latest_operating_cashflow == Decimal("60")
    assert result.operating_cashflow_change_pct == pytest.approx(20.0)
    assert result.recent_periods_source == RecentPeriodsSource.QUARTERLY


def test_result_to_metrics_contains_raw_values() -> None:
    result = _evaluate(
        [Decimal("100"), Decimal("120")],
        [Decimal("50"), Decimal("60")],
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
        recent_periods_source=RecentPeriodsSource.QUARTERLY,
        decision_relevance=EarningsDecisionRelevance.NOT_RELEVANT,
    )
    metrics = earnings_trend_result_to_metrics(result)
    assert metrics["latest_operating_income"] == "120"
    assert metrics["previous_operating_income"] == "100"
    assert metrics["operating_income_change_pct"] == pytest.approx(20.0)
    assert metrics["latest_operating_cashflow"] == "60"
    assert metrics["previous_operating_cashflow"] == "50"
    assert metrics["operating_cashflow_change_pct"] == pytest.approx(20.0)
    assert metrics["recent_periods_source"] == "QUARTERLY"
    assert metrics["state"] == "EVALUATED"
    # 6. metricsへearnings_decision_relevanceが保存される(コードレビュー対応v3)。
    assert metrics["earnings_decision_relevance"] == "NOT_RELEVANT"


# ===== release_confirmation_stateの監査保存(コードレビュー対応 第3回) =====


def test_release_confirmation_state_retained_when_not_applicable() -> None:
    result = _evaluate(
        [Decimal("100"), Decimal("110")],
        release_confirmation_state=EarningsReleaseConfirmationState.DELAYED,
        decision_relevance=EarningsDecisionRelevance.RELEVANT,
    )
    assert result.state == EarningsTrendEvaluationState.NOT_APPLICABLE
    assert result.release_confirmation_state == EarningsReleaseConfirmationState.DELAYED


def test_release_confirmation_state_retained_when_not_evaluated() -> None:
    result = _evaluate(
        [],
        [],
        dividend_comparison_outcome=None,
        release_confirmation_state=EarningsReleaseConfirmationState.AWAITING_CONFIRMATION,
        decision_relevance=EarningsDecisionRelevance.NOT_RELEVANT,
    )
    assert result.state == EarningsTrendEvaluationState.NOT_EVALUATED
    assert (
        result.release_confirmation_state
        == EarningsReleaseConfirmationState.AWAITING_CONFIRMATION
    )


def test_release_confirmation_state_retained_when_evaluated() -> None:
    result = _evaluate(
        [Decimal("100"), Decimal("120")],
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
        release_confirmation_state=EarningsReleaseConfirmationState.DATA_UPDATED,
        decision_relevance=EarningsDecisionRelevance.NOT_RELEVANT,
    )
    assert result.state == EarningsTrendEvaluationState.EVALUATED
    assert result.release_confirmation_state == EarningsReleaseConfirmationState.DATA_UPDATED


def test_result_to_metrics_contains_release_confirmation_state_and_decision_relevance() -> None:
    """2. earnings_trend_result_to_metrics()へrelease_confirmation_state・
    earnings_decision_relevanceの両方が保存される。"""
    result = _evaluate(
        [Decimal("100"), Decimal("120")],
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
        release_confirmation_state=EarningsReleaseConfirmationState.DATA_UPDATED,
        decision_relevance=EarningsDecisionRelevance.NOT_RELEVANT,
    )
    metrics = earnings_trend_result_to_metrics(result)
    assert metrics["release_confirmation_state"] == "DATA_UPDATED"
    assert metrics["earnings_decision_relevance"] == "NOT_RELEVANT"


def test_config_values_include_category_thresholds() -> None:
    values = earnings_trend_config_values(_CONFIG)
    assert values["category_thresholds"] == _CONFIG.category_thresholds.model_dump()


# ===== period_end/period_type監査(コードレビュー対応v3) =====


def test_period_ends_recorded_for_ttm_series() -> None:
    """1. TTM系列で、latest/previous operating income period_endがmetricsへ
    保存される。"""
    result = _evaluate(
        [Decimal("100"), Decimal("120")],
        income_period_type=PeriodType.TTM,
    )
    assert result.latest_operating_income_period_end == dt.date(2025, 6, 30)
    assert result.previous_operating_income_period_end == dt.date(2025, 3, 31)
    assert result.operating_income_period_type == PeriodType.TTM
    metrics = earnings_trend_result_to_metrics(result)
    assert metrics["latest_operating_income_period_end"] == "2025-06-30"
    assert metrics["previous_operating_income_period_end"] == "2025-03-31"
    assert metrics["operating_income_period_type"] == "TTM"


def test_period_type_annual_retained_for_annual_fallback() -> None:
    """2. ANNUAL_FALLBACKで、period_type=ANNUALが監査情報に残る。"""
    result = _evaluate(
        [Decimal("100"), Decimal("120")],
        recent_periods_source=RecentPeriodsSource.ANNUAL_FALLBACK,
        income_period_type=PeriodType.ANNUAL,
    )
    assert result.operating_income_period_type == PeriodType.ANNUAL
    metrics = earnings_trend_result_to_metrics(result)
    assert metrics["operating_income_period_type"] == "ANNUAL"


def test_cashflow_period_ends_recorded() -> None:
    """3. operating cashflowもperiod_endが保存される。"""
    result = _evaluate(
        [Decimal("100"), Decimal("120")],
        [Decimal("50"), Decimal("60")],
    )
    assert result.latest_operating_cashflow_period_end == dt.date(2025, 6, 30)
    assert result.previous_operating_cashflow_period_end == dt.date(2025, 3, 31)
    assert result.operating_cashflow_period_type == PeriodType.TTM


def test_missing_period_info_not_backfilled_with_current_date() -> None:
    """4. 欠損期間情報を現在日で補完しない(単一四半期のみの場合、trend自体が
    算出不可のためperiod_endもNoneのまま。evaluated_atや現在日を代入しない)。"""
    result = _evaluate([Decimal("100")])
    assert result.latest_operating_income_period_end is None
    assert result.previous_operating_income_period_end is None
    assert result.operating_income_period_type is None


def test_score_and_confidence_unchanged_by_period_info_addition() -> None:
    """6. 既存のscore/category/confidenceは期間情報追加だけでは変化しない
    (period_typeを変えてもTTM/ANNUALいずれでも同じ入力なら同じ結果になる)。"""
    incomes, cashflows = _full_income_cashflow()
    ttm_result = _evaluate(
        incomes,
        cashflows,
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
        income_period_type=PeriodType.TTM,
        cashflow_period_type=PeriodType.TTM,
    )
    annual_result = _evaluate(
        incomes,
        cashflows,
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
        income_period_type=PeriodType.ANNUAL,
        cashflow_period_type=PeriodType.ANNUAL,
    )
    assert ttm_result.score == annual_result.score
    assert ttm_result.category == annual_result.category
    assert ttm_result.confidence == annual_result.confidence


# ===== Config validation =====


def test_config_rejects_zero_weight_sum() -> None:
    with pytest.raises(ValidationError):
        _config(
            operating_income_trend_weight=0.0,
            operating_cashflow_trend_weight=0.0,
            dividend_direction_weight=0.0,
            acceleration_weight=0.0,
        )


def test_config_rejects_unordered_trend_boundaries() -> None:
    with pytest.raises(ValidationError):
        _config(trend_decline_pct=10.0, trend_improve_pct=5.0)


def test_config_rejects_non_positive_acceleration_full_scale_pct() -> None:
    with pytest.raises(ValidationError):
        _config(acceleration_full_scale_pct=0.0)


def test_config_rejects_unordered_dividend_scores() -> None:
    with pytest.raises(ValidationError):
        _config(dividend_forecast_cut_score=10.0, dividend_maintained_score=0.0)


def test_config_rejects_invalid_coverage_chain() -> None:
    with pytest.raises(ValidationError):
        _config(min_coverage_required=0.6, coverage_medium_threshold=0.5)


def test_config_category_thresholds_rejects_unordered() -> None:
    with pytest.raises(ValidationError):
        EarningsTrendCategoryThresholds(
            strong_improving=10.0,
            improving=15.0,
            deteriorating=-15.0,
            strong_deteriorating=-50.0,
        )
