"""判定精度向上機能Phase B第二弾: Timing Score v2(モメンタムベースの
エントリータイミング品質スコア)。

コードレビュー対応: v1は「モメンタムが強いほど高得点」になっており
(RSI高値・直近高値ぴったり・STRONG_UPTREND満点が最高評価)、意図していた
「良いトレンドを維持しながら、過熱しておらず、エントリーしやすい価格位置に
あるか」という評価になっていなかった。v2では以下の設計で再構成する。

- trend_quality_component: 既存classify_trend()(RSIをSTRONG判定に使う)は
  変更せず、Timing Score専用にcurrent_price/ma20/ma60/ma20_slope_pctのみから
  独立算出する(RSIを一切参照しない。二重評価を構造的に防ぐ)。
- rsi_component: RSIが高いほど加点、という単調評価を廃止し、過熱・
  エントリー適性のみを見る段階評価(45〜60をピークとし、70超は過熱として
  明確にペナルティ化)とする。
- price_vs_ma20/ma60_component: current_priceとMAのsigned乖離(abs()による
  対称評価は使わない、MAより上/下は意味が異なるため)を段階評価する。
  「適正位置」区分の正のスコアはtrend_quality_componentが0以下の場合0へ
  キャップし、下降トレンド中の下方乖離を積極評価しない。
- drawdown_component: 「直近高値ぴったり」を最高評価とせず、「適度な
  押し目」区分を最高評価とする。この正のスコアもtrend_quality_componentが
  0以下の場合は0へキャップし、「高値から下がった」ことを無条件に「押し目」
  と呼ばない。
- volume_component: 単純に出来高が多いほど加点する設計を廃止。
- overheat_penalty_component: 5日リターン・RSI・drawdownの3条件がすべて
  過熱を示す場合のみ発火する複合ペナルティ。
- TOPIX/セクター相対強度はTiming Scoreの算出対象から除外する(将来の
  Market/Sector Environment Scoreとの二重評価を避けるため。
  MomentumSnapshot側のフィールド自体は温存する)。

外部I/Oを一切行わない純関数(domain/signals/momentum.pyと同じパターン)。
MomentumSnapshotは既にpoint-in-time安全な株価バーから計算済みのため、この
関数自体は独自のlook-ahead bias対策を必要としない(前回レビュー対応と同じ
前提)。

コードレビュー対応(Shadow計測): この評価結果はDecisionSnapshotへ記録する
専用のものであり、BUY候補判定・保有判断スコア・旧売却判定・ProfitTaking
判定・LINE通知など既存の判定ロジックからは一切参照されない。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from jstock_advisor.config.models import TimingScoreRulesConfig
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    TimingScoreCategory,
    TimingScoreEvaluationState,
)
from jstock_advisor.domain.entities.momentum import MomentumSnapshot
from jstock_advisor.domain.entities.timing_score import TimingScoreResult
from jstock_advisor.domain.jst import require_timezone_aware

REASON_TREND_UNAVAILABLE = "TREND_UNAVAILABLE"
REASON_MA20_UNAVAILABLE = "MA20_UNAVAILABLE"
REASON_MA60_UNAVAILABLE = "MA60_UNAVAILABLE"
REASON_RSI_UNAVAILABLE = "RSI_UNAVAILABLE"
REASON_MACD_UNAVAILABLE = "MACD_UNAVAILABLE"
REASON_DRAWDOWN_UNAVAILABLE = "DRAWDOWN_UNAVAILABLE"
REASON_VOLUME_UNAVAILABLE = "VOLUME_UNAVAILABLE"
REASON_OVERHEAT_PENALTY_UNAVAILABLE = "OVERHEAT_PENALTY_UNAVAILABLE"


def _clamp(value: float, low: float = -100.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _trend_quality_component(
    momentum: MomentumSnapshot, current_price: Decimal, config: TimingScoreRulesConfig
) -> float | None:
    """RSIを一切使わず、current_price/ma20/ma60/ma20_slope_pctのみから
    トレンドの質を算出する(既存classify_trend()とは独立、二重評価防止)。"""
    if not momentum.trend_evaluable:
        return None
    ma20 = momentum.ma20
    ma60 = momentum.ma60
    assert ma20 is not None and ma60 is not None  # noqa: S101 trend_evaluableで保証済み
    assert momentum.ma20_slope_pct is not None  # noqa: S101 trend_evaluableで保証済み

    if current_price > ma20 > ma60:
        stack_score = 100.0
    elif current_price < ma20 < ma60:
        stack_score = -100.0
    else:
        stack_score = 0.0

    slope_score = _clamp(momentum.ma20_slope_pct / config.trend_slope_full_scale_pct * 100.0)
    return (stack_score + slope_score) / 2.0


def _rsi_component(rsi: float, config: TimingScoreRulesConfig) -> float:
    """RSIが高いほど加点、という単調評価は採用しない。過熱・エントリー
    適性のみを見る段階評価(45〜60をピークとし、70超は過熱として明確に
    ペナルティ化する)。"""
    if rsi < config.rsi_oversold_boundary:
        return -20.0
    if rsi < config.rsi_neutral_boundary:
        return -10.0
    if rsi < config.rsi_sweet_spot_boundary:
        return 80.0
    if rsi < config.rsi_caution_boundary:
        return 30.0
    if rsi < config.rsi_overheat_boundary:
        return -40.0
    return -100.0


def _signed_deviation_component(
    dev_pct: float,
    breakdown_pct: float,
    pullback_low_pct: float,
    near_high_pct: float,
    overheat_pct: float,
    trend_quality_component: float | None,
) -> float:
    """current_priceとMAのsigned乖離を段階評価する(abs()による対称評価は
    採用しない)。「適正位置」区分の正のスコアはtrend_quality_componentが
    Noneまたは0以下の場合0へキャップする。"""
    if dev_pct < breakdown_pct:
        return -100.0
    if dev_pct < pullback_low_pct:
        return -20.0
    if dev_pct < near_high_pct:
        score = 80.0
        if trend_quality_component is None or trend_quality_component <= 0:
            return min(score, 0.0)
        return score
    if dev_pct < overheat_pct:
        return 30.0
    return -100.0


def _drawdown_component(
    pct: float, config: TimingScoreRulesConfig, trend_quality_component: float | None
) -> float:
    """「直近高値ぴったり」を最高評価とせず、「適度な押し目」区分を最高評価
    とする。押し目区分の正のスコアはtrend_quality_componentがNoneまたは
    0以下の場合0へキャップし、「高値から下がった」ことを無条件に「押し目」
    と呼ばない。"""
    if pct > config.drawdown_near_high_pct:
        return 20.0
    if pct > config.drawdown_pullback_pct:
        score = 80.0
        if trend_quality_component is None or trend_quality_component <= 0:
            return min(score, 0.0)
        return score
    if pct > config.drawdown_neutral_pct:
        return 0.0
    return -80.0


def _volume_component(volume_ratio: float, config: TimingScoreRulesConfig) -> float:
    """単純に出来高が多いほど加点する設計は採用しない。"""
    if volume_ratio < config.volume_low_threshold:
        return -20.0
    if config.volume_moderate_low <= volume_ratio <= config.volume_moderate_high:
        return 50.0
    if volume_ratio >= config.volume_extreme_threshold:
        return -50.0
    return 0.0


def _classify_category(score: float, config: TimingScoreRulesConfig) -> TimingScoreCategory:
    t = config.category_thresholds
    if score >= t.strong_tailwind:
        return TimingScoreCategory.STRONG_TAILWIND
    if score >= t.tailwind:
        return TimingScoreCategory.TAILWIND
    if score <= t.strong_headwind:
        return TimingScoreCategory.STRONG_HEADWIND
    if score <= t.headwind:
        return TimingScoreCategory.HEADWIND
    return TimingScoreCategory.NEUTRAL


def evaluate_timing_score(
    momentum: MomentumSnapshot,
    current_price: Decimal,
    evaluated_at: dt.datetime,
    config: TimingScoreRulesConfig,
) -> TimingScoreResult:
    """MomentumSnapshotとcurrent_priceを基にTiming Score v2を算出する。

    trend_quality/price_vs_ma20/price_vs_ma60/rsi/macd/drawdown/volume/
    overheat_penaltyの8成分を、利用可能な成分の重みだけで正規化して
    加重平均する。coverageが`config.min_coverage_required`未満の場合は
    NOT_EVALUATEDを返す。
    """
    require_timezone_aware(evaluated_at)

    reason_codes: set[str] = set()
    components: list[tuple[float, float]] = []  # (score, weight)

    trend_quality = _trend_quality_component(momentum, current_price, config)
    if trend_quality is not None:
        components.append((trend_quality, config.trend_quality_weight))
    else:
        reason_codes.add(REASON_TREND_UNAVAILABLE)

    ma20_component: float | None = None
    if momentum.ma20 is not None:
        dev = float(current_price / momentum.ma20 - 1) * 100.0
        ma20_component = _signed_deviation_component(
            dev,
            config.ma20_breakdown_pct,
            config.ma20_pullback_low_pct,
            config.ma20_near_high_pct,
            config.ma20_overheat_pct,
            trend_quality,
        )
        components.append((ma20_component, config.price_vs_ma20_weight))
    else:
        reason_codes.add(REASON_MA20_UNAVAILABLE)

    ma60_component: float | None = None
    if momentum.ma60 is not None:
        dev = float(current_price / momentum.ma60 - 1) * 100.0
        ma60_component = _signed_deviation_component(
            dev,
            config.ma60_breakdown_pct,
            config.ma60_pullback_low_pct,
            config.ma60_near_high_pct,
            config.ma60_overheat_pct,
            trend_quality,
        )
        components.append((ma60_component, config.price_vs_ma60_weight))
    else:
        reason_codes.add(REASON_MA60_UNAVAILABLE)

    rsi_component: float | None = None
    if momentum.rsi is not None:
        rsi_component = _rsi_component(momentum.rsi, config)
        components.append((rsi_component, config.rsi_weight))
    else:
        reason_codes.add(REASON_RSI_UNAVAILABLE)

    macd_component: float | None = None
    if momentum.macd is not None:
        histogram = momentum.macd.histogram
        macd_component = 100.0 if histogram > 0 else -100.0 if histogram < 0 else 0.0
        components.append((macd_component, config.macd_weight))
    else:
        reason_codes.add(REASON_MACD_UNAVAILABLE)

    drawdown_component: float | None = None
    if momentum.drawdown_from_recent_high_pct is not None:
        drawdown_component = _drawdown_component(
            momentum.drawdown_from_recent_high_pct, config, trend_quality
        )
        components.append((drawdown_component, config.drawdown_weight))
    else:
        reason_codes.add(REASON_DRAWDOWN_UNAVAILABLE)

    volume_component: float | None = None
    if momentum.volume_ratio is not None:
        volume_component = _volume_component(momentum.volume_ratio, config)
        components.append((volume_component, config.volume_weight))
    else:
        reason_codes.add(REASON_VOLUME_UNAVAILABLE)

    overheat_penalty_component: float | None = None
    if (
        momentum.five_day_return_pct is not None
        and momentum.rsi is not None
        and momentum.drawdown_from_recent_high_pct is not None
    ):
        overheated = (
            momentum.five_day_return_pct >= config.overheat_five_day_return_pct_threshold
            and momentum.rsi >= config.overheat_rsi_threshold
            and momentum.drawdown_from_recent_high_pct >= config.overheat_drawdown_pct_threshold
        )
        overheat_penalty_component = -100.0 if overheated else 0.0
        components.append((overheat_penalty_component, config.overheat_penalty_weight))
    else:
        reason_codes.add(REASON_OVERHEAT_PENALTY_UNAVAILABLE)

    total_config_weight = (
        config.trend_quality_weight
        + config.price_vs_ma20_weight
        + config.price_vs_ma60_weight
        + config.rsi_weight
        + config.macd_weight
        + config.drawdown_weight
        + config.volume_weight
        + config.overheat_penalty_weight
    )
    available_weight = sum(weight for _, weight in components)
    coverage = available_weight / total_config_weight if total_config_weight > 0 else 0.0

    if coverage < config.min_coverage_required:
        return TimingScoreResult(
            state=TimingScoreEvaluationState.NOT_EVALUATED,
            coverage=coverage,
            trend_quality_component=trend_quality,
            price_vs_ma20_component=ma20_component,
            price_vs_ma60_component=ma60_component,
            rsi_component=rsi_component,
            macd_component=macd_component,
            drawdown_component=drawdown_component,
            volume_component=volume_component,
            overheat_penalty_component=overheat_penalty_component,
            reason_codes=tuple(sorted(reason_codes)),
            evaluated_at=evaluated_at,
            model_version=config.model_version,
        )

    score = sum(s * weight for s, weight in components) / available_weight
    category = _classify_category(score, config)

    if coverage >= config.coverage_high_threshold:
        confidence = ConfidenceLevel.HIGH
    elif coverage >= config.coverage_medium_threshold:
        confidence = ConfidenceLevel.MEDIUM
    else:
        confidence = ConfidenceLevel.LOW

    return TimingScoreResult(
        state=TimingScoreEvaluationState.EVALUATED,
        score=score,
        category=category,
        confidence=confidence,
        coverage=coverage,
        trend_quality_component=trend_quality,
        price_vs_ma20_component=ma20_component,
        price_vs_ma60_component=ma60_component,
        rsi_component=rsi_component,
        macd_component=macd_component,
        drawdown_component=drawdown_component,
        volume_component=volume_component,
        overheat_penalty_component=overheat_penalty_component,
        reason_codes=tuple(sorted(reason_codes)),
        evaluated_at=evaluated_at,
        model_version=config.model_version,
    )


def timing_score_result_to_metrics(
    result: TimingScoreResult,
    momentum: MomentumSnapshot | None = None,
    current_price: Decimal | None = None,
) -> dict[str, Any]:
    """TimingScoreResultを、Recommendation.timing_metrics(延いては
    DecisionSnapshot.timing_metrics)へ保存する監査用dictへ変換する
    (後から「どの成分が実効性を持ったか」を分析できるようにするため)。

    momentum・current_priceを渡した場合、成分のスコアだけでなく元になった
    生値(raw metrics)もあわせて記録する。
    """
    metrics: dict[str, Any] = {
        "state": result.state.value,
        "category": result.category.value if result.category is not None else None,
        "trend_quality_component": result.trend_quality_component,
        "price_vs_ma20_component": result.price_vs_ma20_component,
        "price_vs_ma60_component": result.price_vs_ma60_component,
        "rsi_component": result.rsi_component,
        "macd_component": result.macd_component,
        "drawdown_component": result.drawdown_component,
        "volume_component": result.volume_component,
        "overheat_penalty_component": result.overheat_penalty_component,
        "model_version": result.model_version,
    }
    if momentum is not None:
        metrics["rsi"] = momentum.rsi
        metrics["drawdown_from_recent_high_pct"] = momentum.drawdown_from_recent_high_pct
        metrics["volume_ratio"] = momentum.volume_ratio
        metrics["one_day_return_pct"] = momentum.one_day_return_pct
        metrics["five_day_return_pct"] = momentum.five_day_return_pct
        if current_price is not None:
            metrics["current_vs_ma20_pct"] = (
                float(current_price / momentum.ma20 - 1) * 100.0
                if momentum.ma20 is not None
                else None
            )
            metrics["current_vs_ma60_pct"] = (
                float(current_price / momentum.ma60 - 1) * 100.0
                if momentum.ma60 is not None
                else None
            )
    return metrics


def timing_score_config_values(config: TimingScoreRulesConfig) -> dict[str, Any]:
    """判定当時に実際に使用したTiming Score設定値
    (Recommendation.config_values_used["timing_score"]として保存する)。"""
    return {
        "model_version": config.model_version,
        "trend_quality_weight": config.trend_quality_weight,
        "price_vs_ma20_weight": config.price_vs_ma20_weight,
        "price_vs_ma60_weight": config.price_vs_ma60_weight,
        "rsi_weight": config.rsi_weight,
        "macd_weight": config.macd_weight,
        "drawdown_weight": config.drawdown_weight,
        "volume_weight": config.volume_weight,
        "overheat_penalty_weight": config.overheat_penalty_weight,
        "trend_slope_full_scale_pct": config.trend_slope_full_scale_pct,
        "rsi_oversold_boundary": config.rsi_oversold_boundary,
        "rsi_neutral_boundary": config.rsi_neutral_boundary,
        "rsi_sweet_spot_boundary": config.rsi_sweet_spot_boundary,
        "rsi_caution_boundary": config.rsi_caution_boundary,
        "rsi_overheat_boundary": config.rsi_overheat_boundary,
        "drawdown_near_high_pct": config.drawdown_near_high_pct,
        "drawdown_pullback_pct": config.drawdown_pullback_pct,
        "drawdown_neutral_pct": config.drawdown_neutral_pct,
        "ma20_breakdown_pct": config.ma20_breakdown_pct,
        "ma20_pullback_low_pct": config.ma20_pullback_low_pct,
        "ma20_near_high_pct": config.ma20_near_high_pct,
        "ma20_overheat_pct": config.ma20_overheat_pct,
        "ma60_breakdown_pct": config.ma60_breakdown_pct,
        "ma60_pullback_low_pct": config.ma60_pullback_low_pct,
        "ma60_near_high_pct": config.ma60_near_high_pct,
        "ma60_overheat_pct": config.ma60_overheat_pct,
        "volume_low_threshold": config.volume_low_threshold,
        "volume_moderate_low": config.volume_moderate_low,
        "volume_moderate_high": config.volume_moderate_high,
        "volume_extreme_threshold": config.volume_extreme_threshold,
        "overheat_five_day_return_pct_threshold": config.overheat_five_day_return_pct_threshold,
        "overheat_rsi_threshold": config.overheat_rsi_threshold,
        "overheat_drawdown_pct_threshold": config.overheat_drawdown_pct_threshold,
        "min_coverage_required": config.min_coverage_required,
        "coverage_high_threshold": config.coverage_high_threshold,
        "coverage_medium_threshold": config.coverage_medium_threshold,
    }
