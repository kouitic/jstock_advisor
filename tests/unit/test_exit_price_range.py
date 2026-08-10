"""domain/signals/exit_price_range.pyのテスト(判定精度向上機能次フェーズ
STEP2: Exit Price Range Shadow)。

neutral/bull fair valueへHistorical Valuation/Timing adjustmentを同一適用
したadjusted_neutral_fv/adjusted_bull_fvを起点にした3価格算出、
average_purchase_price基準のdownside_review/exit_review price(3価格には
一切影響しない別系統)、およびExitPriceRangeResultのEntity不変条件
(model_validator)を検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from jstock_advisor.config.models import (
    ExitPriceRangeConfig,
    HistoricalValuationExitAdjustmentConfig,
    TimingExitAdjustmentConfig,
)
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    HistoricalValuationCategory,
    HistoricalValuationEvaluationState,
    PriceRangeEvaluationState,
    TimingScoreCategory,
    TimingScoreEvaluationState,
)
from jstock_advisor.domain.entities.exit_price_range import ExitPriceRangeResult
from jstock_advisor.domain.entities.historical_valuation import HistoricalValuationResult
from jstock_advisor.domain.entities.timing_score import TimingScoreResult
from jstock_advisor.domain.entities.valuation import FairValueRange
from jstock_advisor.domain.signals.exit_price_range import (
    REASON_BULL_FAIR_VALUE_UNAVAILABLE,
    REASON_COVERAGE_BELOW_MINIMUM,
    REASON_NEUTRAL_FAIR_VALUE_UNAVAILABLE,
    evaluate_exit_price_range,
    exit_price_range_config_values,
)

_NOW = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)


def _config(**overrides: object) -> ExitPriceRangeConfig:
    defaults: dict[str, object] = dict(
        model_version="exit_price_range_v1",
        historical_valuation_adjustment_fraction=HistoricalValuationExitAdjustmentConfig(
            historically_very_cheap=0.02,
            cheap=0.01,
            normal=0.0,
            expensive=-0.02,
            very_expensive=-0.04,
        ),
        timing_adjustment_fraction=TimingExitAdjustmentConfig(
            strong_tailwind=0.02, tailwind=0.01, neutral=0.0, headwind=-0.01, strong_headwind=-0.02
        ),
        partial_zone_width_fraction=0.015,
        min_price_gap_fraction=0.02,
        loss_tolerance_fraction=0.08,
        review_return_threshold_fraction=0.05,
        historical_valuation_overlay_weight=0.5,
        timing_overlay_weight=0.5,
        min_coverage_required=0.5,
        coverage_high_threshold=0.9,
        coverage_medium_threshold=0.5,
    )
    defaults.update(overrides)
    return ExitPriceRangeConfig.model_validate(defaults)


_CONFIG = _config()


def _fair_value_range(
    *,
    neutral: Decimal | None = Decimal("1200"),
    bull: Decimal | None = Decimal("1500"),
    bear: Decimal | None = Decimal("900"),
    overall_confidence: ConfidenceLevel = ConfidenceLevel.HIGH,
    usable_for_trading_judgment: bool = True,
) -> FairValueRange:
    return FairValueRange(
        bear=bear,
        neutral=neutral,
        bull=bull,
        overall_confidence=overall_confidence,
        methods_used=[],
        methods_excluded=[],
        usable_for_trading_judgment=usable_for_trading_judgment,
    )


def _historical_valuation(
    category: HistoricalValuationCategory | None,
) -> HistoricalValuationResult:
    if category is None:
        return HistoricalValuationResult(
            state=HistoricalValuationEvaluationState.NOT_EVALUATED,
            evaluated_at=_NOW,
            model_version="test-fixture",
        )
    return HistoricalValuationResult(
        state=HistoricalValuationEvaluationState.EVALUATED,
        score=0.0,
        category=category,
        confidence=ConfidenceLevel.HIGH,
        coverage=1.0,
        evaluated_at=_NOW,
        model_version="test-fixture",
    )


def _timing(category: TimingScoreCategory | None) -> TimingScoreResult:
    if category is None:
        return TimingScoreResult(
            state=TimingScoreEvaluationState.NOT_EVALUATED,
            evaluated_at=_NOW,
            model_version="test-fixture",
        )
    return TimingScoreResult(
        state=TimingScoreEvaluationState.EVALUATED,
        score=0.0,
        category=category,
        confidence=ConfidenceLevel.HIGH,
        coverage=1.0,
        evaluated_at=_NOW,
        model_version="test-fixture",
    )


# --- 必須ゲート(neutral/bull両方必須) ------------------------------------


def test_not_evaluated_when_neutral_missing() -> None:
    result = evaluate_exit_price_range(
        _fair_value_range(neutral=None),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.NEUTRAL),
        Decimal("1000"),
        Decimal("1100"),
        _NOW,
        _CONFIG,
    )
    assert result.state == PriceRangeEvaluationState.NOT_EVALUATED
    assert REASON_NEUTRAL_FAIR_VALUE_UNAVAILABLE in result.reason_codes
    assert result.partial_profit_take_low_price is None
    assert result.downside_review_price is None
    assert result.exit_review_price is None


def test_not_evaluated_when_bull_missing() -> None:
    result = evaluate_exit_price_range(
        _fair_value_range(bull=None),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.NEUTRAL),
        Decimal("1000"),
        Decimal("1100"),
        _NOW,
        _CONFIG,
    )
    assert result.state == PriceRangeEvaluationState.NOT_EVALUATED
    assert REASON_BULL_FAIR_VALUE_UNAVAILABLE in result.reason_codes
    # average_purchase_priceのみから技術的に算出可能でも、Exit Price Range
    # 全体が評価不能な場合は一律None(コードレビュー対応STEP2 §11)。
    assert result.downside_review_price is None
    assert result.exit_review_price is None


def test_not_evaluated_when_coverage_below_minimum() -> None:
    result = evaluate_exit_price_range(
        _fair_value_range(),
        _historical_valuation(None),
        _timing(None),
        Decimal("1000"),
        Decimal("1100"),
        _NOW,
        _CONFIG,
    )
    assert result.state == PriceRangeEvaluationState.NOT_EVALUATED
    assert REASON_COVERAGE_BELOW_MINIMUM in result.reason_codes


# --- 3価格算出とordering ---------------------------------------------------


def test_evaluated_result_satisfies_ordering() -> None:
    result = evaluate_exit_price_range(
        _fair_value_range(),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.NEUTRAL),
        Decimal("1000"),
        Decimal("1100"),
        _NOW,
        _CONFIG,
    )
    assert result.state == PriceRangeEvaluationState.EVALUATED
    assert result.partial_profit_take_low_price is not None
    assert result.partial_profit_take_high_price is not None
    assert result.strong_profit_take_price is not None
    assert (
        result.partial_profit_take_low_price
        <= result.partial_profit_take_high_price
        <= result.strong_profit_take_price
    )


def test_historical_valuation_and_timing_adjustments_apply_identically_to_neutral_and_bull() -> (
    None
):
    normal = evaluate_exit_price_range(
        _fair_value_range(),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.NEUTRAL),
        Decimal("1000"),
        Decimal("1100"),
        _NOW,
        _CONFIG,
    )
    cheap = evaluate_exit_price_range(
        _fair_value_range(),
        _historical_valuation(HistoricalValuationCategory.HISTORICALLY_VERY_CHEAP),
        _timing(TimingScoreCategory.NEUTRAL),
        Decimal("1000"),
        Decimal("1100"),
        _NOW,
        _CONFIG,
    )
    assert normal.strong_profit_take_price is not None
    assert cheap.strong_profit_take_price is not None
    # CHEAP系はadjustmentが正(遅める)なのでstrongが高くなる
    assert cheap.strong_profit_take_price > normal.strong_profit_take_price
    assert normal.partial_profit_take_low_price is not None
    assert cheap.partial_profit_take_low_price is not None
    assert cheap.partial_profit_take_low_price > normal.partial_profit_take_low_price


@pytest.mark.parametrize("category", list(HistoricalValuationCategory))
def test_historical_valuation_category_mapping_is_exhaustive(
    category: HistoricalValuationCategory,
) -> None:
    result = evaluate_exit_price_range(
        _fair_value_range(),
        _historical_valuation(category),
        _timing(TimingScoreCategory.NEUTRAL),
        Decimal("1000"),
        Decimal("1100"),
        _NOW,
        _CONFIG,
    )
    assert result.state == PriceRangeEvaluationState.EVALUATED


@pytest.mark.parametrize("category", list(TimingScoreCategory))
def test_timing_category_mapping_is_exhaustive(category: TimingScoreCategory) -> None:
    result = evaluate_exit_price_range(
        _fair_value_range(),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(category),
        Decimal("1000"),
        Decimal("1100"),
        _NOW,
        _CONFIG,
    )
    assert result.state == PriceRangeEvaluationState.EVALUATED


# --- average_purchase_price基準の別系統(3価格には無影響) -----------------


def test_average_purchase_price_only_changes_review_prices() -> None:
    a = evaluate_exit_price_range(
        _fair_value_range(),
        _historical_valuation(HistoricalValuationCategory.EXPENSIVE),
        _timing(TimingScoreCategory.HEADWIND),
        Decimal("1000"),
        Decimal("1150"),
        _NOW,
        _CONFIG,
    )
    b = evaluate_exit_price_range(
        _fair_value_range(),
        _historical_valuation(HistoricalValuationCategory.EXPENSIVE),
        _timing(TimingScoreCategory.HEADWIND),
        Decimal("1300"),  # average_purchase_priceのみ変更
        Decimal("1150"),
        _NOW,
        _CONFIG,
    )
    assert a.partial_profit_take_low_price == b.partial_profit_take_low_price
    assert a.partial_profit_take_high_price == b.partial_profit_take_high_price
    assert a.strong_profit_take_price == b.strong_profit_take_price
    assert a.downside_review_price != b.downside_review_price
    assert a.exit_review_price != b.exit_review_price


def test_downside_and_exit_review_price_formulas() -> None:
    result = evaluate_exit_price_range(
        _fair_value_range(),
        _historical_valuation(HistoricalValuationCategory.NORMAL),
        _timing(TimingScoreCategory.NEUTRAL),
        Decimal("1000"),
        Decimal("1100"),
        _NOW,
        _CONFIG,
    )
    assert result.downside_review_price == Decimal("920")  # 1000 * (1 - 0.08)
    assert result.exit_review_price == Decimal("1050")  # 1000 * (1 + 0.05)


# --- Entity不変条件(model_validator) ------------------------------------


def test_entity_rejects_ordering_violation() -> None:
    with pytest.raises(ValidationError):
        ExitPriceRangeResult(
            state=PriceRangeEvaluationState.EVALUATED,
            current_price=Decimal("1100"),
            partial_profit_take_low_price=Decimal("1200"),
            partial_profit_take_high_price=Decimal("1150"),  # lowより低い(逆転)
            strong_profit_take_price=Decimal("1400"),
            confidence=ConfidenceLevel.MEDIUM,
            evaluated_at=_NOW,
            model_version="exit_price_range_v1",
        )


def test_entity_rejects_evaluated_with_missing_price() -> None:
    with pytest.raises(ValidationError):
        ExitPriceRangeResult(
            state=PriceRangeEvaluationState.EVALUATED,
            current_price=Decimal("1100"),
            partial_profit_take_low_price=Decimal("1150"),
            partial_profit_take_high_price=Decimal("1180"),
            strong_profit_take_price=None,  # EVALUATEDなのに欠損
            confidence=ConfidenceLevel.MEDIUM,
            evaluated_at=_NOW,
            model_version="exit_price_range_v1",
        )


def test_entity_rejects_not_evaluated_with_nonnull_core_price() -> None:
    with pytest.raises(ValidationError):
        ExitPriceRangeResult(
            state=PriceRangeEvaluationState.NOT_EVALUATED,
            current_price=Decimal("1100"),
            partial_profit_take_low_price=Decimal("1150"),
            evaluated_at=_NOW,
            model_version="exit_price_range_v1",
        )


def test_entity_rejects_not_evaluated_with_nonnull_review_price() -> None:
    """コードレビュー対応STEP2 §11: average_purchase_priceのみから算出できる
    downside_review_price/exit_review_priceも、state=NOT_EVALUATEDでは
    一律Noneでなければならない。"""
    with pytest.raises(ValidationError):
        ExitPriceRangeResult(
            state=PriceRangeEvaluationState.NOT_EVALUATED,
            current_price=Decimal("1100"),
            downside_review_price=Decimal("1000"),
            evaluated_at=_NOW,
            model_version="exit_price_range_v1",
        )


def test_entity_rejects_evaluated_without_anchors() -> None:
    with pytest.raises(ValidationError):
        ExitPriceRangeResult(
            state=PriceRangeEvaluationState.EVALUATED,
            current_price=Decimal("1100"),
            neutral_anchor=None,  # EVALUATEDなのに欠損
            bull_anchor=None,
            partial_profit_take_low_price=Decimal("1150"),
            partial_profit_take_high_price=Decimal("1180"),
            strong_profit_take_price=Decimal("1300"),
            downside_review_price=Decimal("1000"),
            exit_review_price=Decimal("1150"),
            confidence=ConfidenceLevel.MEDIUM,
            evaluated_at=_NOW,
            model_version="exit_price_range_v1",
        )


def test_entity_rejects_nonpositive_anchor() -> None:
    with pytest.raises(ValidationError):
        ExitPriceRangeResult(
            state=PriceRangeEvaluationState.EVALUATED,
            current_price=Decimal("1100"),
            neutral_anchor=Decimal("0"),
            bull_anchor=Decimal("1300"),
            partial_profit_take_low_price=Decimal("1150"),
            partial_profit_take_high_price=Decimal("1180"),
            strong_profit_take_price=Decimal("1300"),
            downside_review_price=Decimal("1000"),
            exit_review_price=Decimal("1150"),
            confidence=ConfidenceLevel.MEDIUM,
            evaluated_at=_NOW,
            model_version="exit_price_range_v1",
        )


def test_entity_rejects_evaluated_without_review_prices() -> None:
    with pytest.raises(ValidationError):
        ExitPriceRangeResult(
            state=PriceRangeEvaluationState.EVALUATED,
            current_price=Decimal("1100"),
            neutral_anchor=Decimal("1200"),
            bull_anchor=Decimal("1300"),
            partial_profit_take_low_price=Decimal("1150"),
            partial_profit_take_high_price=Decimal("1180"),
            strong_profit_take_price=Decimal("1300"),
            downside_review_price=None,  # EVALUATEDなのに欠損
            exit_review_price=None,
            confidence=ConfidenceLevel.MEDIUM,
            evaluated_at=_NOW,
            model_version="exit_price_range_v1",
        )


def test_entity_rejects_nonpositive_review_price() -> None:
    with pytest.raises(ValidationError):
        ExitPriceRangeResult(
            state=PriceRangeEvaluationState.EVALUATED,
            current_price=Decimal("1100"),
            neutral_anchor=Decimal("1200"),
            bull_anchor=Decimal("1300"),
            partial_profit_take_low_price=Decimal("1150"),
            partial_profit_take_high_price=Decimal("1180"),
            strong_profit_take_price=Decimal("1300"),
            downside_review_price=Decimal("0"),
            exit_review_price=Decimal("1150"),
            confidence=ConfidenceLevel.MEDIUM,
            evaluated_at=_NOW,
            model_version="exit_price_range_v1",
        )


def test_entity_accepts_not_evaluated_with_all_none() -> None:
    result = ExitPriceRangeResult(
        state=PriceRangeEvaluationState.NOT_EVALUATED,
        current_price=Decimal("1100"),
        evaluated_at=_NOW,
        model_version="exit_price_range_v1",
    )
    assert result.partial_profit_take_low_price is None
    assert result.downside_review_price is None
    assert result.confidence is None


# --- config_values ----------------------------------------------------------


def test_exit_price_range_config_values_includes_all_settings() -> None:
    values = exit_price_range_config_values(_CONFIG)
    assert values["model_version"] == "exit_price_range_v1"
    assert "historical_valuation_adjustment_fraction" in values
    assert "timing_adjustment_fraction" in values
    assert values["loss_tolerance_fraction"] == _CONFIG.loss_tolerance_fraction
    assert values["review_return_threshold_fraction"] == _CONFIG.review_return_threshold_fraction
