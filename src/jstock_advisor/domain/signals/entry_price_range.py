"""判定精度向上機能次フェーズSTEP2: Entry Price Range Shadow。

fair_value_range.neutralを絶対上限(valuation_ceiling)とし、信頼度tier別の
base margin表 → Historical Valuation adjustment(4レベル全てへ同一加算、
floor 0) → Timing nudge(preferredのみ、target+strength方式) → top-down
正規化(max→starter→preferred→strongへmin()による一方向キャップ)の順で
4段階のEntry価格を算出する。

既存のBUY判定(entry_buy_price/standard_buy_price/strong_buy_price/
buy_prices、domain/valuation/margin_of_safety.py)には一切依存せず、また
一切影響しない、DecisionSnapshot記録専用の独立したShadow計測。

外部I/Oを一切行わない純関数(domain/signals/momentum.pyと同じパターン)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from jstock_advisor.config.models import (
    EntryMarginByConfidenceTier,
    EntryPriceRangeConfig,
)
from jstock_advisor.domain.entities.entry_price_range import EntryPriceRangeResult
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    HistoricalValuationCategory,
    PriceRangeEvaluationState,
    TimingScoreCategory,
)
from jstock_advisor.domain.entities.historical_valuation import HistoricalValuationResult
from jstock_advisor.domain.entities.momentum import MomentumSnapshot
from jstock_advisor.domain.entities.timing_score import TimingScoreResult
from jstock_advisor.domain.entities.valuation import FairValueRange
from jstock_advisor.domain.jst import require_timezone_aware
from jstock_advisor.domain.signals._price_range_shared import weaker_confidence
from jstock_advisor.domain.valuation.fair_value import round_yen

REASON_VALUATION_ANCHOR_UNAVAILABLE = "VALUATION_ANCHOR_UNAVAILABLE"
REASON_VALUATION_CONFIDENCE_TOO_LOW = "VALUATION_CONFIDENCE_TOO_LOW"
REASON_HISTORICAL_VALUATION_UNAVAILABLE = "HISTORICAL_VALUATION_UNAVAILABLE"
REASON_TIMING_UNAVAILABLE = "TIMING_UNAVAILABLE"
REASON_MA20_UNAVAILABLE = "MA20_UNAVAILABLE"
REASON_MA60_UNAVAILABLE = "MA60_UNAVAILABLE"
REASON_ENTRY_ORDER_ADJUSTED = "ENTRY_ORDER_ADJUSTED"
REASON_TIMING_NUDGE_TARGET_UNAVAILABLE = "TIMING_NUDGE_TARGET_UNAVAILABLE"
REASON_COVERAGE_BELOW_MINIMUM = "COVERAGE_BELOW_MINIMUM"
# コードレビュー対応(STEP2 §10): 将来Category値が追加された場合、無条件に
# NORMAL/NEUTRAL扱いにはせず、この理由コードとともに調整なし(0.0)とする。
REASON_UNKNOWN_HISTORICAL_VALUATION_CATEGORY = "UNKNOWN_HISTORICAL_VALUATION_CATEGORY_NO_ADJUSTMENT"
REASON_UNKNOWN_TIMING_CATEGORY = "UNKNOWN_TIMING_CATEGORY_NO_ADJUSTMENT"

# category(Enum)→config値のフィールド名への型安全な対応表(コードレビュー
# 対応STEP2 §10)。category.valueによる暗黙dictインデックスは行わない。
_HISTORICAL_VALUATION_ADJUSTMENT_FIELD: dict[HistoricalValuationCategory, str] = {
    HistoricalValuationCategory.HISTORICALLY_VERY_CHEAP: "historically_very_cheap",
    HistoricalValuationCategory.CHEAP: "cheap",
    HistoricalValuationCategory.NORMAL: "normal",
    HistoricalValuationCategory.EXPENSIVE: "expensive",
    HistoricalValuationCategory.VERY_EXPENSIVE: "very_expensive",
}
_TIMING_NUDGE_STRENGTH_FIELD: dict[TimingScoreCategory, str] = {
    TimingScoreCategory.STRONG_TAILWIND: "strong_tailwind",
    TimingScoreCategory.TAILWIND: "tailwind",
    TimingScoreCategory.NEUTRAL: "neutral",
    TimingScoreCategory.HEADWIND: "headwind",
    TimingScoreCategory.STRONG_HEADWIND: "strong_headwind",
}

_PRICE_LEVELS = ("max", "starter", "preferred", "strong")


def _historical_valuation_margin_adjustment(
    category: HistoricalValuationCategory | None, config: EntryPriceRangeConfig
) -> tuple[float, str | None]:
    """category=None(HV未評価)は調整なし(0.0)。未知のcategory値(将来の
    Enum追加)も暗黙にNORMAL扱いせず、調整なし+理由コードとする。"""
    if category is None:
        return 0.0, None
    field = _HISTORICAL_VALUATION_ADJUSTMENT_FIELD.get(category)
    if field is None:
        return 0.0, REASON_UNKNOWN_HISTORICAL_VALUATION_CATEGORY
    return getattr(config.historical_valuation_margin_adjustment_fraction, field), None


def _timing_nudge_strength(
    category: TimingScoreCategory | None, config: EntryPriceRangeConfig
) -> tuple[float, str | None]:
    """category=None(Timing未評価)は調整なし(0.0)。未知のcategory値も
    暗黙にNEUTRAL扱いせず、調整なし+理由コードとする。"""
    if category is None:
        return 0.0, None
    field = _TIMING_NUDGE_STRENGTH_FIELD.get(category)
    if field is None:
        return 0.0, REASON_UNKNOWN_TIMING_CATEGORY
    return getattr(config.timing_nudge_strength_fraction, field), None


def _select_margin_tier(
    overall_confidence: ConfidenceLevel, config: EntryPriceRangeConfig
) -> EntryMarginByConfidenceTier:
    return (
        config.margin_by_confidence_fraction.high
        if overall_confidence == ConfidenceLevel.HIGH
        else config.margin_by_confidence_fraction.medium
    )


def _base_margins(tier: EntryMarginByConfidenceTier) -> dict[str, float]:
    return {
        "max": tier.max,
        "starter": tier.starter,
        "preferred": tier.preferred,
        "strong": tier.strong,
    }


def _adjusted_margins(base_margins: dict[str, float], hv_adjustment: float) -> dict[str, float]:
    return {level: max(0.0, m + hv_adjustment) for level, m in base_margins.items()}


def _raw_prices(
    valuation_ceiling: Decimal, adjusted_margins: dict[str, float]
) -> dict[str, Decimal]:
    return {
        level: round_yen(valuation_ceiling * (Decimal(1) - Decimal(str(adjusted_margins[level]))))
        for level in _PRICE_LEVELS
    }


def _timing_nudge_target(
    timing_category: TimingScoreCategory | None,
    preferred_before_nudge: Decimal,
    raw_max: Decimal,
    current_price: Decimal,
    momentum: MomentumSnapshot,
) -> Decimal | None:
    """Timing categoryに応じたnudge目標価格を返す。TAILWIND系は現在値/
    raw_maxのうち低い方、HEADWIND系はpreferred未満のMA20/MA60のうち安い方
    (該当が無ければNone、nudge自体を適用しない)。"""
    if timing_category in (TimingScoreCategory.TAILWIND, TimingScoreCategory.STRONG_TAILWIND):
        return min(current_price, raw_max)
    if timing_category in (TimingScoreCategory.HEADWIND, TimingScoreCategory.STRONG_HEADWIND):
        candidates = [
            ma
            for ma in (momentum.ma20, momentum.ma60)
            if ma is not None and ma < preferred_before_nudge
        ]
        return min(candidates) if candidates else None
    return None


def _nudge_preferred(
    timing_category: TimingScoreCategory | None,
    preferred_before_nudge: Decimal,
    target: Decimal | None,
    strength: float,
) -> Decimal:
    if target is None:
        return preferred_before_nudge
    strength_decimal = Decimal(str(strength))
    if timing_category in (TimingScoreCategory.TAILWIND, TimingScoreCategory.STRONG_TAILWIND):
        if target <= preferred_before_nudge:
            return preferred_before_nudge
        return round_yen(
            preferred_before_nudge + strength_decimal * (target - preferred_before_nudge)
        )
    if timing_category in (TimingScoreCategory.HEADWIND, TimingScoreCategory.STRONG_HEADWIND):
        return round_yen(
            preferred_before_nudge - strength_decimal * (preferred_before_nudge - target)
        )
    return preferred_before_nudge


def _overlay_coverage(
    historical_valuation_category: HistoricalValuationCategory | None,
    timing_category: TimingScoreCategory | None,
    momentum: MomentumSnapshot,
    config: EntryPriceRangeConfig,
) -> tuple[float, float, float, float]:
    """戻り値は(coverage, hv_availability, timing_availability, ma_availability)。"""
    hv_availability = 1.0 if historical_valuation_category is not None else 0.0
    timing_availability = 1.0 if timing_category is not None else 0.0
    ma_availability = (int(momentum.ma20 is not None) + int(momentum.ma60 is not None)) / 2.0
    coverage = (
        config.historical_valuation_overlay_weight * hv_availability
        + config.timing_overlay_weight * timing_availability
        + config.technical_ma_overlay_weight * ma_availability
    )
    return coverage, hv_availability, timing_availability, ma_availability


def _overlay_confidence(coverage: float, config: EntryPriceRangeConfig) -> ConfidenceLevel:
    if coverage >= config.coverage_high_threshold:
        return ConfidenceLevel.HIGH
    if coverage >= config.coverage_medium_threshold:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


def evaluate_entry_price_range(
    fair_value_range: FairValueRange,
    historical_valuation: HistoricalValuationResult,
    timing: TimingScoreResult,
    momentum: MomentumSnapshot,
    current_price: Decimal,
    evaluated_at: dt.datetime,
    config: EntryPriceRangeConfig,
) -> EntryPriceRangeResult:
    """Entry Price Range(4段階の目安買付価格帯)をShadow計測として算出する。

    必須ゲート: fair_value_range.neutralが存在し、overall_confidenceが
    HIGH/MEDIUMで、usable_for_trading_judgmentがTrueであること。不成立なら
    state=NOT_EVALUATEDとし、4価格・confidence・stop_review_priceは全てNone
    とする(既存のFair Value算出ロジックには一切影響しない、読み取り専用の
    参照)。
    """
    require_timezone_aware(evaluated_at)
    reason_codes: set[str] = set()

    if fair_value_range.neutral is None:
        reason_codes.add(REASON_VALUATION_ANCHOR_UNAVAILABLE)
    if (
        fair_value_range.overall_confidence == ConfidenceLevel.LOW
        or not fair_value_range.usable_for_trading_judgment
    ):
        reason_codes.add(REASON_VALUATION_CONFIDENCE_TOO_LOW)

    gate_ok = (
        fair_value_range.neutral is not None
        and fair_value_range.overall_confidence in (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM)
        and fair_value_range.usable_for_trading_judgment
    )
    if not gate_ok:
        return EntryPriceRangeResult(
            state=PriceRangeEvaluationState.NOT_EVALUATED,
            current_price=current_price,
            reason_codes=tuple(sorted(reason_codes)),
            evaluated_at=evaluated_at,
            model_version=config.model_version,
        )

    valuation_ceiling = fair_value_range.neutral
    assert valuation_ceiling is not None  # noqa: S101 gate_okで保証済み(mypy対応)
    ceiling = round_yen(valuation_ceiling)

    if historical_valuation.category is None:
        reason_codes.add(REASON_HISTORICAL_VALUATION_UNAVAILABLE)
    if timing.category is None:
        reason_codes.add(REASON_TIMING_UNAVAILABLE)
    if momentum.ma20 is None:
        reason_codes.add(REASON_MA20_UNAVAILABLE)
    if momentum.ma60 is None:
        reason_codes.add(REASON_MA60_UNAVAILABLE)

    coverage, _, _, _ = _overlay_coverage(
        historical_valuation.category, timing.category, momentum, config
    )

    if coverage < config.min_coverage_required:
        reason_codes.add(REASON_COVERAGE_BELOW_MINIMUM)
        return EntryPriceRangeResult(
            state=PriceRangeEvaluationState.NOT_EVALUATED,
            current_price=current_price,
            coverage=coverage,
            reason_codes=tuple(sorted(reason_codes)),
            evaluated_at=evaluated_at,
            model_version=config.model_version,
        )

    hv_adjustment, hv_reason = _historical_valuation_margin_adjustment(
        historical_valuation.category, config
    )
    if hv_reason:
        reason_codes.add(hv_reason)
    timing_strength, timing_reason = _timing_nudge_strength(timing.category, config)
    if timing_reason:
        reason_codes.add(timing_reason)

    tier = _select_margin_tier(fair_value_range.overall_confidence, config)
    base_margins = _base_margins(tier)
    adjusted_margins = _adjusted_margins(base_margins, hv_adjustment)
    raw_prices = _raw_prices(valuation_ceiling, adjusted_margins)

    preferred_before_nudge = raw_prices["preferred"]
    nudge_target = _timing_nudge_target(
        timing.category, preferred_before_nudge, raw_prices["max"], current_price, momentum
    )
    if (
        timing.category in (TimingScoreCategory.HEADWIND, TimingScoreCategory.STRONG_HEADWIND)
        and nudge_target is None
    ):
        reason_codes.add(REASON_TIMING_NUDGE_TARGET_UNAVAILABLE)
    nudged_preferred = _nudge_preferred(
        timing.category, preferred_before_nudge, nudge_target, timing_strength
    )

    gap = Decimal(str(config.min_price_gap_fraction))
    one_plus_gap = Decimal(1) + gap
    final_max = min(raw_prices["max"], ceiling)
    final_starter = min(raw_prices["starter"], round_yen(final_max / one_plus_gap))
    final_preferred = min(nudged_preferred, round_yen(final_starter / one_plus_gap))
    final_strong = min(raw_prices["strong"], round_yen(final_preferred / one_plus_gap))

    ordering_adjusted = (
        final_max != raw_prices["max"]
        or final_starter != raw_prices["starter"]
        or final_preferred != nudged_preferred
        or final_strong != raw_prices["strong"]
    )
    if ordering_adjusted:
        reason_codes.add(REASON_ENTRY_ORDER_ADJUSTED)

    stop_review_price = (
        round_yen(fair_value_range.bear) if fair_value_range.bear is not None else None
    )

    overlay_confidence = _overlay_confidence(coverage, config)
    final_confidence = weaker_confidence(fair_value_range.overall_confidence, overlay_confidence)

    return EntryPriceRangeResult(
        state=PriceRangeEvaluationState.EVALUATED,
        current_price=current_price,
        valuation_ceiling=ceiling,
        starter_entry_price=final_starter,
        preferred_entry_price=final_preferred,
        strong_entry_price=final_strong,
        max_entry_price=final_max,
        stop_review_price=stop_review_price,
        confidence=final_confidence,
        coverage=coverage,
        reason_codes=tuple(sorted(reason_codes)),
        ordering_adjusted=ordering_adjusted,
        evaluated_at=evaluated_at,
        model_version=config.model_version,
    )


def entry_price_range_result_to_metrics(
    result: EntryPriceRangeResult,
    fair_value_range: FairValueRange | None = None,
    historical_valuation: HistoricalValuationResult | None = None,
    timing: TimingScoreResult | None = None,
    momentum: MomentumSnapshot | None = None,
    config: EntryPriceRangeConfig | None = None,
) -> dict[str, Any]:
    """EntryPriceRangeResultを、Recommendation.entry_price_range_metrics
    (延いてはDecisionSnapshot.entry_price_range_metrics)へ保存する監査用
    dictへ変換する。

    fair_value_range/historical_valuation/timing/momentum/configを渡した
    場合、Result自体には保持していない算出過程の生値(base margin・
    adjustment・正規化前価格・timing nudge目標値等)もあわせて記録する
    (evaluate_entry_price_range()と同じ純関数群を再利用して再算出するため、
    ロジックの二重実装にはならない)。取得不能値はNone(0で穴埋めしない)。
    """
    metrics: dict[str, Any] = {
        "state": result.state.value,
        "valuation_ceiling": str(result.valuation_ceiling)
        if result.valuation_ceiling is not None
        else None,
        "starter_entry_price": str(result.starter_entry_price)
        if result.starter_entry_price is not None
        else None,
        "preferred_entry_price": (
            str(result.preferred_entry_price) if result.preferred_entry_price is not None else None
        ),
        "strong_entry_price": str(result.strong_entry_price)
        if result.strong_entry_price is not None
        else None,
        "max_entry_price": str(result.max_entry_price)
        if result.max_entry_price is not None
        else None,
        "stop_review_price": str(result.stop_review_price)
        if result.stop_review_price is not None
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
        metrics["fair_value_confidence"] = (
            fair_value_range.overall_confidence.value if fair_value_range else None
        )
        return metrics

    hv_category = historical_valuation.category if historical_valuation is not None else None
    timing_category = timing.category if timing is not None else None

    metrics["valuation_anchor_neutral"] = (
        str(fair_value_range.neutral) if fair_value_range.neutral is not None else None
    )
    metrics["fair_value_confidence"] = fair_value_range.overall_confidence.value

    hv_adjustment, _ = _historical_valuation_margin_adjustment(hv_category, config)
    timing_strength, _ = _timing_nudge_strength(timing_category, config)
    tier = _select_margin_tier(fair_value_range.overall_confidence, config)
    base_margins = _base_margins(tier)
    adjusted_margins = _adjusted_margins(base_margins, hv_adjustment)
    for level in _PRICE_LEVELS:
        metrics[f"base_margin_fraction_{level}"] = base_margins[level]
        metrics[f"adjusted_margin_fraction_{level}"] = adjusted_margins[level]
    metrics["historical_adjustment_fraction"] = hv_adjustment

    if fair_value_range.neutral is not None and momentum is not None:
        raw_prices = _raw_prices(fair_value_range.neutral, adjusted_margins)
        for level in _PRICE_LEVELS:
            metrics[f"{level}_entry_price_pre_normalization"] = str(raw_prices[level])
        preferred_before_nudge = raw_prices["preferred"]
        nudge_target = _timing_nudge_target(
            timing_category,
            preferred_before_nudge,
            raw_prices["max"],
            result.current_price,
            momentum,
        )
        metrics["preferred_price_before_timing_nudge"] = str(preferred_before_nudge)
        metrics["timing_nudge_category"] = (
            timing_category.value if timing_category is not None else None
        )
        metrics["timing_nudge_strength_fraction"] = timing_strength
        metrics["timing_nudge_target_price"] = (
            str(nudge_target) if nudge_target is not None else None
        )
        nudged_preferred = _nudge_preferred(
            timing_category, preferred_before_nudge, nudge_target, timing_strength
        )
        metrics["preferred_price_after_timing_nudge"] = str(nudged_preferred)
        metrics["technical_ma20"] = str(momentum.ma20) if momentum.ma20 is not None else None
        metrics["technical_ma60"] = str(momentum.ma60) if momentum.ma60 is not None else None
        coverage, hv_avail, timing_avail, ma_avail = _overlay_coverage(
            hv_category, timing_category, momentum, config
        )
        metrics["coverage_historical_valuation"] = hv_avail
        metrics["coverage_timing"] = timing_avail
        metrics["coverage_technical_ma"] = ma_avail
        metrics["overlay_confidence"] = _overlay_confidence(coverage, config).value

    return metrics


def entry_price_range_config_values(config: EntryPriceRangeConfig) -> dict[str, Any]:
    """判定当時に実際に使用したEntry Price Range設定値
    (Recommendation.config_values_used["entry_price_range"]として保存する)。"""
    return {
        "model_version": config.model_version,
        "margin_by_confidence_fraction": {
            "high": config.margin_by_confidence_fraction.high.model_dump(),
            "medium": config.margin_by_confidence_fraction.medium.model_dump(),
        },
        "historical_valuation_margin_adjustment_fraction": (
            config.historical_valuation_margin_adjustment_fraction.model_dump()
        ),
        "timing_nudge_strength_fraction": config.timing_nudge_strength_fraction.model_dump(),
        "min_price_gap_fraction": config.min_price_gap_fraction,
        "historical_valuation_overlay_weight": config.historical_valuation_overlay_weight,
        "timing_overlay_weight": config.timing_overlay_weight,
        "technical_ma_overlay_weight": config.technical_ma_overlay_weight,
        "min_coverage_required": config.min_coverage_required,
        "coverage_high_threshold": config.coverage_high_threshold,
        "coverage_medium_threshold": config.coverage_medium_threshold,
    }
