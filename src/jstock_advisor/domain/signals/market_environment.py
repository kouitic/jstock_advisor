"""判定精度向上機能Phase D: Market Environment Score(市場全体の地合い)。

TOPIXのPriceBar(build_stock_snapshot()で既に取得済みのtopix_bars、新規
Provider I/Oなし)のみを使い、trend_structure(MA20/60・slopeをclassify_
trend()で分類)・medium_term_return(20d/60d return)・drawdown(直近高値
からの下落率)の3成分の加重平均でscoreを算出する。

境界: 再利用するのはmomentum.pyの純粋な価格計算関数(compute_moving_average
等)のみ。TimingScoreResult/evaluate_timing_score()が持つRSI過熱ペナルティ・
MACD成分・trend_qualityの重み付け式など、個別銘柄のエントリー適性に特化
したロジックは一切流用しない。classify_trend()へは意図的にrsi=Noneを渡す
(RSIベースのSTRONG_UPTREND/STRONG_DOWNTREND判定はTiming Score同様の
過熱評価と混同しないため、v1では通常のUPTREND/NEUTRAL/DOWNTREND判定に
留める。trend_classification_score設定にSTRONG系キーは将来拡張用に残す)。

Shadow計測専用。既存のBUY/SELL/HoldingDecision/ProfitTaking判定・LINE通知
には一切影響しない。

コードレビュー対応(2026-08、Phase D初版からの修正): bar staleness判定は
暦日差ではなく既存BusinessCalendar(domain/business_calendar.py)による
営業日差で行う(GW・年末年始等の連休をまたぐ際の意味のずれを解消)。また、
config.min_bars_ma60/min_bars_return_60dを、内部計算関数の暗黙の最低本数
要件任せにせず、明示的な評価可否条件として使用する。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from jstock_advisor.config.models import EnvironmentCategoryThresholds, MarketEnvironmentConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EnvironmentCategory,
    MarketEnvironmentEvaluationState,
    TrendClassification,
)
from jstock_advisor.domain.entities.market_environment import MarketEnvironmentResult
from jstock_advisor.domain.signals._environment_shared import (
    cap_confidence,
    clamp_score,
    filter_future_bars,
)
from jstock_advisor.domain.signals.momentum import (
    classify_trend,
    compute_drawdown_from_recent_high_pct,
    compute_ma_slope_pct,
    compute_moving_average,
    compute_n_day_return_pct,
)
from jstock_advisor.interfaces.types import PriceBar

REASON_MARKET_DATA_UNAVAILABLE = "MARKET_DATA_UNAVAILABLE"
REASON_COVERAGE_BELOW_MINIMUM = "COVERAGE_BELOW_MINIMUM"
REASON_MARKET_BARS_STALE = "MARKET_BARS_STALE"
REASON_TREND_STRUCTURE_UNAVAILABLE = "TREND_STRUCTURE_UNAVAILABLE"
REASON_RETURN_UNAVAILABLE = "RETURN_UNAVAILABLE"
REASON_DRAWDOWN_UNAVAILABLE = "DRAWDOWN_UNAVAILABLE"

_TREND_SCORE_ATTR: dict[TrendClassification, str] = {
    TrendClassification.STRONG_UPTREND: "strong_uptrend",
    TrendClassification.UPTREND: "uptrend",
    TrendClassification.NEUTRAL: "neutral",
    TrendClassification.DOWNTREND: "downtrend",
    TrendClassification.STRONG_DOWNTREND: "strong_downtrend",
}


def category_from_score(
    score: float, thresholds: EnvironmentCategoryThresholds
) -> EnvironmentCategory:
    if score >= thresholds.strong_tailwind:
        return EnvironmentCategory.STRONG_TAILWIND
    if score >= thresholds.tailwind:
        return EnvironmentCategory.TAILWIND
    if score <= thresholds.strong_headwind:
        return EnvironmentCategory.STRONG_HEADWIND
    if score <= thresholds.headwind:
        return EnvironmentCategory.HEADWIND
    return EnvironmentCategory.NEUTRAL


def confidence_from_coverage(
    coverage: float, high_threshold: float, medium_threshold: float
) -> ConfidenceLevel:
    if coverage >= high_threshold:
        return ConfidenceLevel.HIGH
    if coverage >= medium_threshold:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


def is_bars_stale(
    latest_bar_date: dt.date | None,
    as_of_date: dt.date,
    max_staleness_business_days: int,
    calendar: BusinessCalendar,
) -> bool:
    """最新barの日付が評価基準日よりmax_staleness_business_days(営業日、
    BusinessCalendarによる実際の営業日カウント)より古いか。バーが無ければ
    判定不能なのでFalse(NOT_EVALUATED側の判定は別途coverageで行う)。
    金曜最新bar・月曜評価は通常1営業日として扱われ、連休・年末年始をまたいでも
    実際に開いていた営業日の本数のみでカウントする。"""
    if latest_bar_date is None:
        return False
    business_days_stale = calendar.business_days_between(latest_bar_date, as_of_date)
    return business_days_stale > max_staleness_business_days


def evaluate_market_environment(
    bars: list[PriceBar],
    as_of_date: dt.date,
    now: dt.datetime,
    config: MarketEnvironmentConfig,
    calendar: BusinessCalendar,
) -> MarketEnvironmentResult:
    effective_bars, future_bars_filtered = filter_future_bars(bars, as_of_date)
    latest_bar_date = effective_bars[-1].date if effective_bars else None
    bars_stale = is_bars_stale(
        latest_bar_date, as_of_date, config.max_bar_staleness_business_days, calendar
    )

    reason_codes: list[str] = []
    if bars_stale:
        reason_codes.append(REASON_MARKET_BARS_STALE)
    if not effective_bars:
        reason_codes.append(REASON_MARKET_DATA_UNAVAILABLE)

    market_current_price = effective_bars[-1].close if effective_bars else None

    # --- A. trend_structure_component ---
    # コードレビュー対応: ma20/ma60/slopeの個別計算結果がたまたま得られても、
    # config.min_bars_ma60(最低本数条件)を満たさない場合は評価しない
    # (config_values_usedに保存した値と実際の評価条件を一致させるため)。
    ma20 = compute_moving_average(effective_bars, 20)
    ma60 = compute_moving_average(effective_bars, 60)
    ma20_slope_pct = compute_ma_slope_pct(effective_bars, 20, config.ma_slope_lookback_days)
    trend_evaluable = (
        ma20 is not None
        and ma60 is not None
        and ma20_slope_pct is not None
        and len(effective_bars) >= config.min_bars_ma60
    )
    trend_classification: TrendClassification | None = None
    trend_structure_component: float | None = None
    if trend_evaluable:
        assert market_current_price is not None  # noqa: S101 (trend_evaluable含意)
        trend_classification = classify_trend(
            market_current_price, ma20, ma60, ma20_slope_pct, None, 0.0
        )
        score_attr = _TREND_SCORE_ATTR[trend_classification]
        trend_structure_component = getattr(config.trend_classification_score, score_attr)
    else:
        reason_codes.append(REASON_TREND_STRUCTURE_UNAVAILABLE)

    # --- B. medium_term_return_component ---
    # コードレビュー対応: 60d returnはconfig.min_bars_return_60dを満たさない
    # 場合Noneとして扱う(20dは必要本数があれば単独で利用可能。60dだけ不足
    # している場合は20dのみでmedium_term_return_componentを算出する)。
    return_20d = compute_n_day_return_pct(effective_bars, 20)
    return_60d = (
        compute_n_day_return_pct(effective_bars, 60)
        if len(effective_bars) >= config.min_bars_return_60d
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

    # --- C. drawdown_component ---
    drawdown_from_high_pct: float | None = None
    drawdown_component: float | None = None
    if market_current_price is not None:
        drawdown_from_high_pct = compute_drawdown_from_recent_high_pct(
            effective_bars, market_current_price, config.drawdown_window_days
        )
    if drawdown_from_high_pct is not None:
        drawdown_component = clamp_score(
            (drawdown_from_high_pct - config.drawdown_neutral_threshold_pct)
            / config.drawdown_scale_pct
            * 100
        )
    else:
        reason_codes.append(REASON_DRAWDOWN_UNAVAILABLE)

    weighted_components = (
        (trend_structure_component, config.component_weights.trend_structure),
        (medium_term_return_component, config.component_weights.medium_term_return),
        (drawdown_component, config.component_weights.drawdown),
    )
    coverage = sum(w for v, w in weighted_components if v is not None)

    score: float | None = None
    category: EnvironmentCategory | None = None
    confidence: ConfidenceLevel | None = None
    state = MarketEnvironmentEvaluationState.NOT_EVALUATED

    if coverage > 0 and coverage >= config.min_coverage_required:
        weighted_sum = sum(v * w for v, w in weighted_components if v is not None)
        score = clamp_score(weighted_sum / coverage)
        category = category_from_score(score, config.category_thresholds)
        confidence = confidence_from_coverage(
            coverage, config.coverage_high_threshold, config.coverage_medium_threshold
        )
        if bars_stale:
            confidence = cap_confidence(confidence, ConfidenceLevel.MEDIUM)
        state = MarketEnvironmentEvaluationState.EVALUATED
    else:
        reason_codes.append(REASON_COVERAGE_BELOW_MINIMUM)

    return MarketEnvironmentResult(
        state=state,
        score=score,
        category=category,
        confidence=confidence,
        coverage=coverage,
        trend_structure_component=trend_structure_component,
        medium_term_return_component=medium_term_return_component,
        drawdown_component=drawdown_component,
        ma20=ma20,
        ma60=ma60,
        ma20_slope_pct=ma20_slope_pct,
        trend_classification=trend_classification,
        return_20d_pct=return_20d,
        return_60d_pct=return_60d,
        drawdown_from_high_pct=drawdown_from_high_pct,
        bars_used=len(effective_bars),
        latest_bar_date=latest_bar_date,
        future_bars_filtered=future_bars_filtered,
        bars_stale=bars_stale,
        reason_codes=tuple(reason_codes),
        evaluated_at=now,
        model_version=config.model_version,
    )


def market_environment_result_to_metrics(result: MarketEnvironmentResult) -> dict[str, Any]:
    """MarketEnvironmentResultを、Recommendation.market_metrics(延いては
    DecisionSnapshot.market_metrics)へ保存する監査用dictへ変換する(既存4
    スコアのresult_to_metrics()と同型のI/F)。"""
    return {
        "state": result.state.value,
        "category": result.category.value if result.category is not None else None,
        "score": result.score,
        "trend_structure_component": result.trend_structure_component,
        "medium_term_return_component": result.medium_term_return_component,
        "drawdown_component": result.drawdown_component,
        "ma20": str(result.ma20) if result.ma20 is not None else None,
        "ma60": str(result.ma60) if result.ma60 is not None else None,
        "ma20_slope_pct": result.ma20_slope_pct,
        "trend_classification": (
            result.trend_classification.value if result.trend_classification is not None else None
        ),
        "return_20d_pct": result.return_20d_pct,
        "return_60d_pct": result.return_60d_pct,
        "drawdown_from_high_pct": result.drawdown_from_high_pct,
        "bars_used": result.bars_used,
        "latest_bar_date": (
            result.latest_bar_date.isoformat() if result.latest_bar_date is not None else None
        ),
        "future_bars_filtered": result.future_bars_filtered,
        "bars_stale": result.bars_stale,
        "model_version": result.model_version,
    }


def market_environment_config_values(config: MarketEnvironmentConfig) -> dict[str, object]:
    return {
        "model_version": config.model_version,
        "component_weights": config.component_weights.model_dump(),
        "trend_classification_score": config.trend_classification_score.model_dump(),
        "ma_slope_lookback_days": config.ma_slope_lookback_days,
        "return_score_scale_pct": config.return_score_scale_pct,
        "drawdown_window_days": config.drawdown_window_days,
        "drawdown_neutral_threshold_pct": config.drawdown_neutral_threshold_pct,
        "drawdown_scale_pct": config.drawdown_scale_pct,
        "min_bars_ma60": config.min_bars_ma60,
        "min_bars_return_60d": config.min_bars_return_60d,
        "max_bar_staleness_business_days": config.max_bar_staleness_business_days,
        "min_coverage_required": config.min_coverage_required,
        "coverage_high_threshold": config.coverage_high_threshold,
        "coverage_medium_threshold": config.coverage_medium_threshold,
        "category_thresholds": config.category_thresholds.model_dump(),
    }
