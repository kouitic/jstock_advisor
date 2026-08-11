"""判定精度向上機能Phase D: Sector Environment Score(所属セクターの地合い)。

config.momentum.sector_etf_mapに対応ETFが登録されている業種のみ評価する
(未登録なら即NOT_APPLICABLE、0点/NEUTRAL扱いにしない)。sector_bars/
topix_bars(build_stock_snapshot()で既に取得済み、新規Provider I/Oなし)を
使い、trend_structure/medium_term_return(Market Environmentと同じロジック
をsector_barsへ適用)に加え、relative_strength(セクターの対TOPIX相対強度、
compute_relative_strength_pct(sector_bars, topix_bars, window)を新規引数で
呼び出す — 個別株の対TOPIX/対セクター相対強度とは別物)を主要成分とする。

コードレビュー対応(2026-08、Phase D初版からの修正): sector_etf_symbolが
Noneの場合(その業種自体が評価対象外)のみNOT_APPLICABLEとし、mapping済み
だがデータ取得できない場合はNOT_EVALUATEDとして明確に区別する。bar
staleness判定・min_bars_return_60dの扱いはmarket_environment.pyと同じ
(BusinessCalendarによる営業日判定、60d return/relative_strength_60dは
config.min_bars_return_60d未満でNone扱い)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.config.models import SectorEnvironmentConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EnvironmentCategory,
    SectorEnvironmentEvaluationState,
    TrendClassification,
)
from jstock_advisor.domain.entities.sector_environment import SectorEnvironmentResult
from jstock_advisor.domain.signals._environment_shared import (
    cap_confidence,
    clamp_score,
    filter_future_bars,
)
from jstock_advisor.domain.signals.market_environment import (
    category_from_score,
    confidence_from_coverage,
    is_bars_stale,
)
from jstock_advisor.domain.signals.momentum import (
    classify_trend,
    compute_ma_slope_pct,
    compute_moving_average,
    compute_n_day_return_pct,
    compute_relative_strength_pct,
)
from jstock_advisor.interfaces.types import PriceBar

REASON_SECTOR_ETF_NOT_MAPPED = "SECTOR_ETF_NOT_MAPPED"
REASON_SECTOR_DATA_UNAVAILABLE = "SECTOR_DATA_UNAVAILABLE"
REASON_COVERAGE_BELOW_MINIMUM = "COVERAGE_BELOW_MINIMUM"
REASON_SECTOR_BARS_STALE = "SECTOR_BARS_STALE"
REASON_TREND_STRUCTURE_UNAVAILABLE = "TREND_STRUCTURE_UNAVAILABLE"
REASON_RETURN_UNAVAILABLE = "RETURN_UNAVAILABLE"
REASON_RELATIVE_STRENGTH_UNAVAILABLE = "RELATIVE_STRENGTH_UNAVAILABLE"

_TREND_SCORE_ATTR: dict[TrendClassification, str] = {
    TrendClassification.STRONG_UPTREND: "strong_uptrend",
    TrendClassification.UPTREND: "uptrend",
    TrendClassification.NEUTRAL: "neutral",
    TrendClassification.DOWNTREND: "downtrend",
    TrendClassification.STRONG_DOWNTREND: "strong_downtrend",
}


def evaluate_sector_environment(
    sector_bars: list[PriceBar] | None,
    topix_bars: list[PriceBar],
    sector_etf_symbol: str | None,
    as_of_date: dt.date,
    now: dt.datetime,
    config: SectorEnvironmentConfig,
    calendar: BusinessCalendar,
) -> SectorEnvironmentResult:
    # コードレビュー対応: 「その業種自体が評価対象外」(NOT_APPLICABLE)と
    # 「評価対象ではあるがデータ取得できない」(NOT_EVALUATED)を区別する。
    if sector_etf_symbol is None:
        return SectorEnvironmentResult(
            state=SectorEnvironmentEvaluationState.NOT_APPLICABLE,
            sector_etf_symbol=None,
            reason_codes=(REASON_SECTOR_ETF_NOT_MAPPED,),
            evaluated_at=now,
            model_version=config.model_version,
        )
    if not sector_bars:
        return SectorEnvironmentResult(
            state=SectorEnvironmentEvaluationState.NOT_EVALUATED,
            sector_etf_symbol=sector_etf_symbol,
            reason_codes=(REASON_SECTOR_DATA_UNAVAILABLE,),
            evaluated_at=now,
            model_version=config.model_version,
        )

    effective_sector_bars, sector_bars_filtered = filter_future_bars(sector_bars, as_of_date)
    effective_topix_bars, _ = filter_future_bars(topix_bars, as_of_date)
    latest_bar_date = effective_sector_bars[-1].date if effective_sector_bars else None
    bars_stale = is_bars_stale(
        latest_bar_date, as_of_date, config.max_bar_staleness_business_days, calendar
    )

    reason_codes: list[str] = []
    if bars_stale:
        reason_codes.append(REASON_SECTOR_BARS_STALE)
    if not effective_sector_bars:
        reason_codes.append(REASON_SECTOR_DATA_UNAVAILABLE)

    sector_current_price = effective_sector_bars[-1].close if effective_sector_bars else None

    # --- A. trend_structure_component ---
    ma20 = compute_moving_average(effective_sector_bars, 20)
    ma60 = compute_moving_average(effective_sector_bars, 60)
    ma20_slope_pct = compute_ma_slope_pct(effective_sector_bars, 20, config.ma_slope_lookback_days)
    trend_evaluable = ma20 is not None and ma60 is not None and ma20_slope_pct is not None
    trend_classification: TrendClassification | None = None
    trend_structure_component: float | None = None
    if trend_evaluable:
        assert sector_current_price is not None  # noqa: S101 (trend_evaluable含意)
        trend_classification = classify_trend(
            sector_current_price, ma20, ma60, ma20_slope_pct, None, 0.0
        )
        score_attr = _TREND_SCORE_ATTR[trend_classification]
        trend_structure_component = getattr(config.trend_classification_score, score_attr)
    else:
        reason_codes.append(REASON_TREND_STRUCTURE_UNAVAILABLE)

    # --- B. medium_term_return_component ---
    # コードレビュー対応: 60d returnはconfig.min_bars_return_60dを満たさない
    # 場合Noneとして扱う(20dは必要本数があれば単独で利用可能)。
    return_20d = compute_n_day_return_pct(effective_sector_bars, 20)
    return_60d = (
        compute_n_day_return_pct(effective_sector_bars, 60)
        if len(effective_sector_bars) >= config.min_bars_return_60d
        else None
    )
    available_returns = [r for r in (return_20d, return_60d) if r is not None]
    medium_term_return_component: float | None = None
    if available_returns:
        blended_return_pct = sum(available_returns) / len(available_returns)
        medium_term_return_component = clamp_score(
            blended_return_pct / config.return_score_scale_pct * 100
        )
    else:
        reason_codes.append(REASON_RETURN_UNAVAILABLE)

    # --- C. relative_strength_component(セクター vs TOPIX) ---
    # コードレビュー対応: relative_strength_60dもsector/TOPIX双方が
    # config.min_bars_return_60dを満たす場合のみ算出する。
    relative_strength_20d = compute_relative_strength_pct(
        effective_sector_bars, effective_topix_bars, 20
    )
    relative_strength_60d = (
        compute_relative_strength_pct(effective_sector_bars, effective_topix_bars, 60)
        if len(effective_sector_bars) >= config.min_bars_return_60d
        and len(effective_topix_bars) >= config.min_bars_return_60d
        else None
    )
    available_relative_strength = [
        r for r in (relative_strength_20d, relative_strength_60d) if r is not None
    ]
    relative_strength_component: float | None = None
    if available_relative_strength:
        blended_relative_strength_pp = sum(available_relative_strength) / len(
            available_relative_strength
        )
        relative_strength_component = clamp_score(
            blended_relative_strength_pp / config.relative_strength_scale_pct * 100
        )
    else:
        reason_codes.append(REASON_RELATIVE_STRENGTH_UNAVAILABLE)

    weighted_components = (
        (trend_structure_component, config.component_weights.trend_structure),
        (medium_term_return_component, config.component_weights.medium_term_return),
        (relative_strength_component, config.component_weights.relative_strength),
    )
    coverage = sum(w for v, w in weighted_components if v is not None)

    score: float | None = None
    category: EnvironmentCategory | None = None
    confidence: ConfidenceLevel | None = None
    state = SectorEnvironmentEvaluationState.NOT_EVALUATED

    if coverage > 0 and coverage >= config.min_coverage_required:
        weighted_sum = sum(v * w for v, w in weighted_components if v is not None)
        score = clamp_score(weighted_sum / coverage)
        category = category_from_score(score, config.category_thresholds)
        confidence = confidence_from_coverage(
            coverage, config.coverage_high_threshold, config.coverage_medium_threshold
        )
        if bars_stale:
            confidence = cap_confidence(confidence, ConfidenceLevel.MEDIUM)
        state = SectorEnvironmentEvaluationState.EVALUATED
    else:
        reason_codes.append(REASON_COVERAGE_BELOW_MINIMUM)

    return SectorEnvironmentResult(
        state=state,
        sector_etf_symbol=sector_etf_symbol,
        score=score,
        category=category,
        confidence=confidence,
        coverage=coverage,
        trend_structure_component=trend_structure_component,
        medium_term_return_component=medium_term_return_component,
        relative_strength_component=relative_strength_component,
        ma20=ma20,
        ma60=ma60,
        ma20_slope_pct=ma20_slope_pct,
        trend_classification=trend_classification,
        return_20d_pct=return_20d,
        return_60d_pct=return_60d,
        relative_strength_20d_pct=relative_strength_20d,
        relative_strength_60d_pct=relative_strength_60d,
        bars_used=len(effective_sector_bars),
        latest_bar_date=latest_bar_date,
        future_bars_filtered=sector_bars_filtered,
        bars_stale=bars_stale,
        reason_codes=tuple(reason_codes),
        evaluated_at=now,
        model_version=config.model_version,
    )


def sector_environment_result_to_metrics(result: SectorEnvironmentResult) -> dict[str, object]:
    """SectorEnvironmentResultを、Recommendation.sector_metrics(延いては
    DecisionSnapshot.sector_metrics)へ保存する監査用dictへ変換する。"""
    return {
        "state": result.state.value,
        "sector_etf_symbol": result.sector_etf_symbol,
        "category": result.category.value if result.category is not None else None,
        "score": result.score,
        "trend_structure_component": result.trend_structure_component,
        "medium_term_return_component": result.medium_term_return_component,
        "relative_strength_component": result.relative_strength_component,
        "ma20": str(result.ma20) if result.ma20 is not None else None,
        "ma60": str(result.ma60) if result.ma60 is not None else None,
        "ma20_slope_pct": result.ma20_slope_pct,
        "trend_classification": (
            result.trend_classification.value if result.trend_classification is not None else None
        ),
        "return_20d_pct": result.return_20d_pct,
        "return_60d_pct": result.return_60d_pct,
        "relative_strength_20d_pct": result.relative_strength_20d_pct,
        "relative_strength_60d_pct": result.relative_strength_60d_pct,
        "bars_used": result.bars_used,
        "latest_bar_date": (
            result.latest_bar_date.isoformat() if result.latest_bar_date is not None else None
        ),
        "future_bars_filtered": result.future_bars_filtered,
        "bars_stale": result.bars_stale,
        "model_version": result.model_version,
    }


def sector_environment_config_values(config: SectorEnvironmentConfig) -> dict[str, object]:
    return {
        "model_version": config.model_version,
        "component_weights": config.component_weights.model_dump(),
        "trend_classification_score": config.trend_classification_score.model_dump(),
        "ma_slope_lookback_days": config.ma_slope_lookback_days,
        "return_score_scale_pct": config.return_score_scale_pct,
        "relative_strength_scale_pct": config.relative_strength_scale_pct,
        "min_bars_return_60d": config.min_bars_return_60d,
        "max_bar_staleness_business_days": config.max_bar_staleness_business_days,
        "min_coverage_required": config.min_coverage_required,
        "coverage_high_threshold": config.coverage_high_threshold,
        "coverage_medium_threshold": config.coverage_medium_threshold,
        "category_thresholds": config.category_thresholds.model_dump(),
    }
