"""判定精度向上機能次フェーズSTEP2: Exit Price Range Shadow。

fair_value_range.neutral/bullへHistorical Valuation/Timingの調整量を同一
適用したadjusted_neutral_fv/adjusted_bull_fvを起点に、一部利確ゾーン
(partial_low/high)と強気利確価格(strong)を算出する。downside_review_price/
exit_review_priceはaverage_purchase_price基準の別系統であり、上記3価格には
一切影響しない。

既存のSELL(legacy)判定・ProfitTaking判定(sell_prices)には一切依存せず、
また一切影響しない、DecisionSnapshot記録専用の独立したShadow計測。

外部I/Oを一切行わない純関数(domain/signals/momentum.pyと同じパターン)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from jstock_advisor.config.models import ExitPriceRangeConfig
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    HistoricalValuationCategory,
    PriceRangeEvaluationState,
    TimingScoreCategory,
)
from jstock_advisor.domain.entities.exit_price_range import ExitPriceRangeResult
from jstock_advisor.domain.entities.historical_valuation import HistoricalValuationResult
from jstock_advisor.domain.entities.timing_score import TimingScoreResult
from jstock_advisor.domain.entities.valuation import FairValueRange
from jstock_advisor.domain.jst import require_timezone_aware
from jstock_advisor.domain.signals._price_range_shared import weaker_confidence
from jstock_advisor.domain.valuation.fair_value import round_yen

REASON_NEUTRAL_FAIR_VALUE_UNAVAILABLE = "NEUTRAL_FAIR_VALUE_UNAVAILABLE"
REASON_BULL_FAIR_VALUE_UNAVAILABLE = "BULL_FAIR_VALUE_UNAVAILABLE"
REASON_FAIR_VALUE_CONFIDENCE_TOO_LOW = "FAIR_VALUE_CONFIDENCE_TOO_LOW"
REASON_HISTORICAL_VALUATION_UNAVAILABLE = "HISTORICAL_VALUATION_UNAVAILABLE"
REASON_TIMING_UNAVAILABLE = "TIMING_UNAVAILABLE"
REASON_COVERAGE_BELOW_MINIMUM = "COVERAGE_BELOW_MINIMUM"
REASON_EXIT_ORDER_ADJUSTED = "EXIT_ORDER_ADJUSTED"
REASON_UNKNOWN_HISTORICAL_VALUATION_CATEGORY = "UNKNOWN_HISTORICAL_VALUATION_CATEGORY_NO_ADJUSTMENT"
REASON_UNKNOWN_TIMING_CATEGORY = "UNKNOWN_TIMING_CATEGORY_NO_ADJUSTMENT"

_HISTORICAL_VALUATION_ADJUSTMENT_FIELD: dict[HistoricalValuationCategory, str] = {
    HistoricalValuationCategory.HISTORICALLY_VERY_CHEAP: "historically_very_cheap",
    HistoricalValuationCategory.CHEAP: "cheap",
    HistoricalValuationCategory.NORMAL: "normal",
    HistoricalValuationCategory.EXPENSIVE: "expensive",
    HistoricalValuationCategory.VERY_EXPENSIVE: "very_expensive",
}
_TIMING_ADJUSTMENT_FIELD: dict[TimingScoreCategory, str] = {
    TimingScoreCategory.STRONG_TAILWIND: "strong_tailwind",
    TimingScoreCategory.TAILWIND: "tailwind",
    TimingScoreCategory.NEUTRAL: "neutral",
    TimingScoreCategory.HEADWIND: "headwind",
    TimingScoreCategory.STRONG_HEADWIND: "strong_headwind",
}


def _historical_valuation_adjustment(
    category: HistoricalValuationCategory | None, config: ExitPriceRangeConfig
) -> tuple[float, str | None]:
    if category is None:
        return 0.0, None
    field = _HISTORICAL_VALUATION_ADJUSTMENT_FIELD.get(category)
    if field is None:
        return 0.0, REASON_UNKNOWN_HISTORICAL_VALUATION_CATEGORY
    return getattr(config.historical_valuation_adjustment_fraction, field), None


def _timing_adjustment(
    category: TimingScoreCategory | None, config: ExitPriceRangeConfig
) -> tuple[float, str | None]:
    if category is None:
        return 0.0, None
    field = _TIMING_ADJUSTMENT_FIELD.get(category)
    if field is None:
        return 0.0, REASON_UNKNOWN_TIMING_CATEGORY
    return getattr(config.timing_adjustment_fraction, field), None


def _adjusted_fair_values(
    neutral_anchor: Decimal, bull_anchor: Decimal, hv_adjustment: float, timing_adjustment: float
) -> tuple[Decimal, Decimal]:
    multiplier = Decimal(1) + Decimal(str(hv_adjustment)) + Decimal(str(timing_adjustment))
    return neutral_anchor * multiplier, bull_anchor * multiplier


def _overlay_coverage(
    historical_valuation_category: HistoricalValuationCategory | None,
    timing_category: TimingScoreCategory | None,
    config: ExitPriceRangeConfig,
) -> tuple[float, float, float]:
    """戻り値は(coverage, hv_availability, timing_availability)。"""
    hv_availability = 1.0 if historical_valuation_category is not None else 0.0
    timing_availability = 1.0 if timing_category is not None else 0.0
    coverage = (
        config.historical_valuation_overlay_weight * hv_availability
        + config.timing_overlay_weight * timing_availability
    )
    return coverage, hv_availability, timing_availability


def _overlay_confidence(coverage: float, config: ExitPriceRangeConfig) -> ConfidenceLevel:
    if coverage >= config.coverage_high_threshold:
        return ConfidenceLevel.HIGH
    if coverage >= config.coverage_medium_threshold:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


def evaluate_exit_price_range(
    fair_value_range: FairValueRange,
    historical_valuation: HistoricalValuationResult,
    timing: TimingScoreResult,
    average_purchase_price: Decimal,
    current_price: Decimal,
    evaluated_at: dt.datetime,
    config: ExitPriceRangeConfig,
) -> ExitPriceRangeResult:
    """Exit Price Range(一部利確ゾーン・強気利確価格・取得単価基準レビュー
    ラインの5価格)をShadow計測として算出する。

    必須ゲート: fair_value_range.neutral**と**bullの両方が存在し、
    overall_confidenceがHIGH/MEDIUMで、usable_for_trading_judgmentがTrueで
    あること。不成立ならstate=NOT_EVALUATEDとし、downside_review_price/
    exit_review_priceを含む5価格全てをNoneとする(average_purchase_price
    のみから技術的に算出可能な2価格も、Exit Price Range全体が評価不能な
    場合は一律Noneとする。コードレビュー対応STEP2 §11)。
    """
    require_timezone_aware(evaluated_at)
    reason_codes: set[str] = set()

    if fair_value_range.neutral is None:
        reason_codes.add(REASON_NEUTRAL_FAIR_VALUE_UNAVAILABLE)
    if fair_value_range.bull is None:
        reason_codes.add(REASON_BULL_FAIR_VALUE_UNAVAILABLE)
    if (
        fair_value_range.overall_confidence == ConfidenceLevel.LOW
        or not fair_value_range.usable_for_trading_judgment
    ):
        reason_codes.add(REASON_FAIR_VALUE_CONFIDENCE_TOO_LOW)

    gate_ok = (
        fair_value_range.neutral is not None
        and fair_value_range.bull is not None
        and fair_value_range.overall_confidence in (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM)
        and fair_value_range.usable_for_trading_judgment
    )
    if not gate_ok:
        return ExitPriceRangeResult(
            state=PriceRangeEvaluationState.NOT_EVALUATED,
            current_price=current_price,
            reason_codes=tuple(sorted(reason_codes)),
            evaluated_at=evaluated_at,
            model_version=config.model_version,
        )

    neutral_anchor = fair_value_range.neutral
    bull_anchor = fair_value_range.bull
    assert neutral_anchor is not None  # noqa: S101 gate_okで保証済み(mypy対応)
    assert bull_anchor is not None  # noqa: S101 gate_okで保証済み(mypy対応)

    if historical_valuation.category is None:
        reason_codes.add(REASON_HISTORICAL_VALUATION_UNAVAILABLE)
    if timing.category is None:
        reason_codes.add(REASON_TIMING_UNAVAILABLE)

    coverage, _, _ = _overlay_coverage(historical_valuation.category, timing.category, config)

    if coverage < config.min_coverage_required:
        reason_codes.add(REASON_COVERAGE_BELOW_MINIMUM)
        return ExitPriceRangeResult(
            state=PriceRangeEvaluationState.NOT_EVALUATED,
            current_price=current_price,
            coverage=coverage,
            reason_codes=tuple(sorted(reason_codes)),
            evaluated_at=evaluated_at,
            model_version=config.model_version,
        )

    hv_adjustment, hv_reason = _historical_valuation_adjustment(
        historical_valuation.category, config
    )
    if hv_reason:
        reason_codes.add(hv_reason)
    timing_adjustment, timing_reason = _timing_adjustment(timing.category, config)
    if timing_reason:
        reason_codes.add(timing_reason)

    adjusted_neutral_fv, adjusted_bull_fv = _adjusted_fair_values(
        neutral_anchor, bull_anchor, hv_adjustment, timing_adjustment
    )

    width = Decimal(str(config.partial_zone_width_fraction))
    raw_partial_low = round_yen(adjusted_neutral_fv * (Decimal(1) - width))
    raw_partial_high = round_yen(adjusted_neutral_fv * (Decimal(1) + width))
    raw_strong = round_yen(adjusted_bull_fv)

    gap = Decimal(str(config.min_price_gap_fraction))
    one_plus_gap = Decimal(1) + gap
    final_partial_low = raw_partial_low
    final_partial_high = max(raw_partial_high, round_yen(final_partial_low * one_plus_gap))
    final_strong = max(raw_strong, round_yen(final_partial_high * one_plus_gap))

    ordering_adjusted = final_partial_high != raw_partial_high or final_strong != raw_strong
    if ordering_adjusted:
        reason_codes.add(REASON_EXIT_ORDER_ADJUSTED)

    downside_review_price = round_yen(
        average_purchase_price * (Decimal(1) - Decimal(str(config.loss_tolerance_fraction)))
    )
    exit_review_price = round_yen(
        average_purchase_price
        * (Decimal(1) + Decimal(str(config.review_return_threshold_fraction)))
    )

    overlay_confidence = _overlay_confidence(coverage, config)
    final_confidence = weaker_confidence(fair_value_range.overall_confidence, overlay_confidence)

    return ExitPriceRangeResult(
        state=PriceRangeEvaluationState.EVALUATED,
        current_price=current_price,
        neutral_anchor=round_yen(neutral_anchor),
        bull_anchor=round_yen(bull_anchor),
        partial_profit_take_low_price=final_partial_low,
        partial_profit_take_high_price=final_partial_high,
        strong_profit_take_price=final_strong,
        downside_review_price=downside_review_price,
        exit_review_price=exit_review_price,
        confidence=final_confidence,
        coverage=coverage,
        reason_codes=tuple(sorted(reason_codes)),
        ordering_adjusted=ordering_adjusted,
        evaluated_at=evaluated_at,
        model_version=config.model_version,
    )


def exit_price_range_result_to_metrics(
    result: ExitPriceRangeResult,
    fair_value_range: FairValueRange | None = None,
    historical_valuation: HistoricalValuationResult | None = None,
    timing: TimingScoreResult | None = None,
    average_purchase_price: Decimal | None = None,
    config: ExitPriceRangeConfig | None = None,
) -> dict[str, Any]:
    """ExitPriceRangeResultを、Recommendation.exit_price_range_metrics
    (延いてはDecisionSnapshot.exit_price_range_metrics)へ保存する監査用
    dictへ変換する。取得不能値はNone(0で穴埋めしない)。"""
    metrics: dict[str, Any] = {
        "state": result.state.value,
        "neutral_anchor": str(result.neutral_anchor) if result.neutral_anchor is not None else None,
        "bull_anchor": str(result.bull_anchor) if result.bull_anchor is not None else None,
        "partial_profit_take_low_price": (
            str(result.partial_profit_take_low_price)
            if result.partial_profit_take_low_price is not None
            else None
        ),
        "partial_profit_take_high_price": (
            str(result.partial_profit_take_high_price)
            if result.partial_profit_take_high_price is not None
            else None
        ),
        "strong_profit_take_price": (
            str(result.strong_profit_take_price)
            if result.strong_profit_take_price is not None
            else None
        ),
        "downside_review_price": (
            str(result.downside_review_price) if result.downside_review_price is not None else None
        ),
        "exit_review_price": str(result.exit_review_price)
        if result.exit_review_price is not None
        else None,
        "final_confidence": result.confidence.value if result.confidence is not None else None,
        "overlay_coverage": result.coverage,
        "ordering_adjusted": result.ordering_adjusted,
        "reason_codes": list(result.reason_codes),
        "model_version": result.model_version,
    }

    if (
        fair_value_range is None
        or config is None
        or result.state != PriceRangeEvaluationState.EVALUATED
    ):
        metrics["valuation_anchor_neutral"] = None
        metrics["valuation_anchor_bull"] = None
        metrics["fair_value_confidence"] = (
            fair_value_range.overall_confidence.value if fair_value_range else None
        )
        return metrics

    hv_category = historical_valuation.category if historical_valuation is not None else None
    timing_category = timing.category if timing is not None else None

    metrics["valuation_anchor_neutral"] = (
        str(fair_value_range.neutral) if fair_value_range.neutral is not None else None
    )
    metrics["valuation_anchor_bull"] = (
        str(fair_value_range.bull) if fair_value_range.bull is not None else None
    )
    metrics["fair_value_confidence"] = fair_value_range.overall_confidence.value

    hv_adjustment, _ = _historical_valuation_adjustment(hv_category, config)
    timing_adjustment, _ = _timing_adjustment(timing_category, config)
    metrics["historical_adjustment_fraction"] = hv_adjustment
    metrics["timing_adjustment_fraction"] = timing_adjustment
    metrics["partial_zone_width_fraction"] = config.partial_zone_width_fraction
    metrics["loss_tolerance_fraction"] = config.loss_tolerance_fraction
    metrics["review_return_threshold_fraction"] = config.review_return_threshold_fraction

    if fair_value_range.neutral is not None and fair_value_range.bull is not None:
        adjusted_neutral_fv, adjusted_bull_fv = _adjusted_fair_values(
            fair_value_range.neutral, fair_value_range.bull, hv_adjustment, timing_adjustment
        )
        metrics["adjusted_neutral_fair_value"] = str(round_yen(adjusted_neutral_fv))
        metrics["adjusted_bull_fair_value"] = str(round_yen(adjusted_bull_fv))
        width = Decimal(str(config.partial_zone_width_fraction))
        metrics["partial_low_pre_normalization"] = str(
            round_yen(adjusted_neutral_fv * (Decimal(1) - width))
        )
        metrics["partial_high_pre_normalization"] = str(
            round_yen(adjusted_neutral_fv * (Decimal(1) + width))
        )
        metrics["strong_pre_normalization"] = str(round_yen(adjusted_bull_fv))
        coverage, hv_avail, timing_avail = _overlay_coverage(hv_category, timing_category, config)
        metrics["coverage_historical_valuation"] = hv_avail
        metrics["coverage_timing"] = timing_avail
        metrics["overlay_confidence"] = _overlay_confidence(coverage, config).value

    if average_purchase_price is not None:
        metrics["average_purchase_price_used"] = str(average_purchase_price)
        if average_purchase_price > 0:
            metrics["unrealized_return_pct_used"] = float(
                (result.current_price / average_purchase_price - 1) * 100
            )

    return metrics


def exit_price_range_config_values(config: ExitPriceRangeConfig) -> dict[str, Any]:
    """判定当時に実際に使用したExit Price Range設定値
    (Recommendation.config_values_used["exit_price_range"]として保存する)。"""
    return {
        "model_version": config.model_version,
        "historical_valuation_adjustment_fraction": (
            config.historical_valuation_adjustment_fraction.model_dump()
        ),
        "timing_adjustment_fraction": config.timing_adjustment_fraction.model_dump(),
        "partial_zone_width_fraction": config.partial_zone_width_fraction,
        "min_price_gap_fraction": config.min_price_gap_fraction,
        "loss_tolerance_fraction": config.loss_tolerance_fraction,
        "review_return_threshold_fraction": config.review_return_threshold_fraction,
        "historical_valuation_overlay_weight": config.historical_valuation_overlay_weight,
        "timing_overlay_weight": config.timing_overlay_weight,
        "min_coverage_required": config.min_coverage_required,
        "coverage_high_threshold": config.coverage_high_threshold,
        "coverage_medium_threshold": config.coverage_medium_threshold,
    }
