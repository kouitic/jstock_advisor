"""domain/signals/earnings_trend.pyのテスト(判定精度向上機能Phase C)。"""

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
    EarningsDateStatus,
    EarningsReleaseConfirmationState,
    EarningsTrendCategory,
    EarningsTrendEvaluationState,
)
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


def _evaluate(
    incomes: list[Decimal],
    cashflows: list[Decimal] | None = None,
    dividend_comparison_outcome: DividendComparisonOutcome | None = None,
    earnings_date_status: EarningsDateStatus = EarningsDateStatus.UNAVAILABLE,
    release_confirmation_state: EarningsReleaseConfirmationState = (
        EarningsReleaseConfirmationState.NOT_APPLICABLE
    ),
    config: EarningsTrendRulesConfig | None = None,
):
    return evaluate_earnings_trend(
        incomes,
        cashflows if cashflows is not None else [],
        dividend_comparison_outcome,
        earnings_date_status,
        release_confirmation_state,
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


# ===== 営業利益/営業CFトレンド成分の段階評価 =====


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


def test_trend_component_unavailable_when_previous_is_zero() -> None:
    result = _evaluate([Decimal("0"), Decimal("100")])
    assert result.operating_income_trend_component is None


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


# ===== NOT_APPLICABLE(決算反映未確認)ゲート =====


@pytest.mark.parametrize(
    "state",
    [
        EarningsReleaseConfirmationState.AWAITING_CONFIRMATION,
        EarningsReleaseConfirmationState.DELAYED,
    ],
)
def test_not_applicable_when_awaiting_earnings_confirmation(
    state: EarningsReleaseConfirmationState,
) -> None:
    result = _evaluate(
        [Decimal("100"), Decimal("110")],
        earnings_date_status=EarningsDateStatus.STALE_PAST_DATE,
        release_confirmation_state=state,
    )
    assert result.state == EarningsTrendEvaluationState.NOT_APPLICABLE
    assert result.reason_codes == ("AWAITING_EARNINGS_CONFIRMATION",)
    assert result.score is None


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


# ===== 監査情報 =====


def test_result_to_metrics_contains_components() -> None:
    result = _evaluate(
        [Decimal("100"), Decimal("120")],
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
    )
    metrics = earnings_trend_result_to_metrics(result)
    assert metrics["operating_income_trend_component"] == 100.0
    assert metrics["dividend_direction_component"] == 80.0
    assert metrics["state"] == "EVALUATED"


def test_config_values_include_category_thresholds() -> None:
    values = earnings_trend_config_values(_CONFIG)
    assert values["category_thresholds"] == _CONFIG.category_thresholds.model_dump()


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
