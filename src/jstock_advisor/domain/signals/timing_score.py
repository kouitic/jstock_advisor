"""判定精度向上機能Phase B第二弾: Timing Score(モメンタムベースの技術的
タイミングスコア)。

既存のMomentumSnapshot(domain/signals/momentum.py、既にStockSnapshot.momentum
として毎回計算済み)を基に、現在が技術的モメンタムの観点から良いタイミングか
どうかを-100(逆風)〜+100(追い風)のスコアで表す。MomentumSnapshotは既に
point-in-time安全な株価バーから計算済みのため、この関数自体は独自の
look-ahead bias対策(Historical Valuation Scoreのavailable_at等)を必要と
しない、純粋な派生値の変換のみを行う(外部I/Oを一切行わない純関数、
domain/signals/momentum.pyと同じパターン)。

コードレビュー前例(Historical Valuation Score)を踏襲し、算出に使う成分が
不足する場合は推測で補完せず、coverageがconfig化した最低ラインを下回れば
NOT_EVALUATEDを返す。

コードレビュー前例(Shadow計測): この評価結果はDecisionSnapshot(判定精度
向上機能Phase Aの自己評価基盤)へ記録する専用のものであり、BUY候補判定・
保有判断スコア・旧売却判定・ProfitTaking判定・LINE通知など既存の判定
ロジックからは一切参照されない。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from jstock_advisor.config.models import TimingScoreRulesConfig
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    TimingScoreCategory,
    TimingScoreEvaluationState,
    TrendClassification,
)
from jstock_advisor.domain.entities.momentum import MomentumSnapshot
from jstock_advisor.domain.entities.timing_score import TimingScoreResult
from jstock_advisor.domain.jst import require_timezone_aware

REASON_RSI_UNAVAILABLE = "RSI_UNAVAILABLE"
REASON_MACD_UNAVAILABLE = "MACD_UNAVAILABLE"
REASON_TOPIX_RELATIVE_STRENGTH_UNAVAILABLE = "TOPIX_RELATIVE_STRENGTH_UNAVAILABLE"
REASON_SECTOR_RELATIVE_STRENGTH_UNAVAILABLE = "SECTOR_RELATIVE_STRENGTH_UNAVAILABLE"
REASON_DRAWDOWN_UNAVAILABLE = "DRAWDOWN_UNAVAILABLE"

_TREND_SCORE = {
    TrendClassification.STRONG_UPTREND: 100.0,
    TrendClassification.UPTREND: 50.0,
    TrendClassification.NEUTRAL: 0.0,
    TrendClassification.DOWNTREND: -50.0,
    TrendClassification.STRONG_DOWNTREND: -100.0,
}


def _clamp(value: float, low: float = -100.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _classify_category(
    score: float, config: TimingScoreRulesConfig
) -> TimingScoreCategory:
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
    evaluated_at: dt.datetime,
    config: TimingScoreRulesConfig,
) -> TimingScoreResult:
    """MomentumSnapshotを基にTiming Scoreを算出する。

    trend成分は常に利用可能(MomentumSnapshot.trend_classificationは必須
    フィールド、データ不足時もNEUTRAL)。他の5成分(RSI・MACD・TOPIX/セクター
    相対強度・直近高値からの下落率)は元データが無ければ利用不可として除外し、
    利用可能な成分の重みだけで加重平均を正規化する。coverageが
    `config.min_coverage_required`未満の場合はNOT_EVALUATEDを返す
    (trendだけで低カバレッジのスコアが出てしまうことを防ぐため)。
    """
    require_timezone_aware(evaluated_at)

    reason_codes: set[str] = set()
    components: list[tuple[float, float]] = []  # (score, weight)

    trend_component = _TREND_SCORE[momentum.trend_classification]
    components.append((trend_component, config.trend_weight))

    rsi_component: float | None = None
    if momentum.rsi is not None:
        rsi_component = _clamp((momentum.rsi - 50.0) * 2.0)
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

    topix_component: float | None = None
    if momentum.relative_strength_vs_topix_pct is not None:
        topix_component = _clamp(
            momentum.relative_strength_vs_topix_pct
            / config.relative_strength_full_scale_pct
            * 100.0
        )
        components.append((topix_component, config.topix_relative_strength_weight))
    else:
        reason_codes.add(REASON_TOPIX_RELATIVE_STRENGTH_UNAVAILABLE)

    sector_component: float | None = None
    if momentum.relative_strength_vs_sector_pct is not None:
        sector_component = _clamp(
            momentum.relative_strength_vs_sector_pct
            / config.relative_strength_full_scale_pct
            * 100.0
        )
        components.append((sector_component, config.sector_relative_strength_weight))
    else:
        reason_codes.add(REASON_SECTOR_RELATIVE_STRENGTH_UNAVAILABLE)

    drawdown_component: float | None = None
    if momentum.drawdown_from_recent_high_pct is not None:
        drawdown_component = _clamp(
            100.0
            + momentum.drawdown_from_recent_high_pct / config.drawdown_full_scale_pct * 100.0
        )
        components.append((drawdown_component, config.drawdown_weight))
    else:
        reason_codes.add(REASON_DRAWDOWN_UNAVAILABLE)

    total_config_weight = (
        config.trend_weight
        + config.rsi_weight
        + config.macd_weight
        + config.topix_relative_strength_weight
        + config.sector_relative_strength_weight
        + config.drawdown_weight
    )
    available_weight = sum(weight for _, weight in components)
    coverage = available_weight / total_config_weight if total_config_weight > 0 else 0.0

    if coverage < config.min_coverage_required:
        return TimingScoreResult(
            state=TimingScoreEvaluationState.NOT_EVALUATED,
            coverage=coverage,
            trend_component=trend_component,
            rsi_component=rsi_component,
            macd_component=macd_component,
            topix_relative_strength_component=topix_component,
            sector_relative_strength_component=sector_component,
            drawdown_component=drawdown_component,
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
        trend_component=trend_component,
        rsi_component=rsi_component,
        macd_component=macd_component,
        topix_relative_strength_component=topix_component,
        sector_relative_strength_component=sector_component,
        drawdown_component=drawdown_component,
        reason_codes=tuple(sorted(reason_codes)),
        evaluated_at=evaluated_at,
        model_version=config.model_version,
    )


def timing_score_result_to_metrics(result: TimingScoreResult) -> dict[str, Any]:
    """TimingScoreResultを、Recommendation.timing_metrics(延いては
    DecisionSnapshot.timing_metrics)へ保存する監査用dictへ変換する
    (後から「なぜこの点数だったか」を再現できるようにするため)。"""
    return {
        "state": result.state.value,
        "category": result.category.value if result.category is not None else None,
        "trend_component": result.trend_component,
        "rsi_component": result.rsi_component,
        "macd_component": result.macd_component,
        "topix_relative_strength_component": result.topix_relative_strength_component,
        "sector_relative_strength_component": result.sector_relative_strength_component,
        "drawdown_component": result.drawdown_component,
        "model_version": result.model_version,
    }


def timing_score_config_values(config: TimingScoreRulesConfig) -> dict[str, Any]:
    """判定当時に実際に使用したTiming Score設定値
    (Recommendation.config_values_used["timing_score"]として保存する)。"""
    return {
        "model_version": config.model_version,
        "trend_weight": config.trend_weight,
        "rsi_weight": config.rsi_weight,
        "macd_weight": config.macd_weight,
        "topix_relative_strength_weight": config.topix_relative_strength_weight,
        "sector_relative_strength_weight": config.sector_relative_strength_weight,
        "drawdown_weight": config.drawdown_weight,
        "relative_strength_full_scale_pct": config.relative_strength_full_scale_pct,
        "drawdown_full_scale_pct": config.drawdown_full_scale_pct,
        "min_coverage_required": config.min_coverage_required,
        "coverage_high_threshold": config.coverage_high_threshold,
        "coverage_medium_threshold": config.coverage_medium_threshold,
    }
