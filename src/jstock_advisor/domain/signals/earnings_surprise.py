"""判定精度向上機能Phase C: Earnings Surprise Score v1(決算サプライズスコア)。

実装前調査(調査結果はセッション内の計画書参照)により、当初検討した4要素の
うちAnalyst Consensus SurpriseとDividend Surprise/Revisionの2要素のみで
構成する(Historical Progress Surprise・Guidance Revisionは現行データソース
では実装しない。domain/entities/earnings_surprise.py参照)。

look-ahead bias防止: 最新決算が確定反映されたかどうかは、この関数では判定
せず、呼び出し側が`EarningsReleaseConfirmationState`(domain/signals/
earnings_window.py)を解決したうえで渡す。決算予定日を経過していながら
財務データへの反映が未確認(AWAITING_CONFIRMATION/DELAYED)の場合、この関数は
NOT_APPLICABLEを返し評価を意図的に見送る(古い決算データを最新として使わない、
という既存方針を踏襲)。

外部I/Oを一切行わない純関数(domain/signals/timing_score.pyと同じパターン)。

コードレビュー対応(Shadow計測): この評価結果はDecisionSnapshotへ記録する
専用のものであり、BUY候補判定・保有判断スコア・旧売却判定・ProfitTaking
判定・LINE通知など既存の判定ロジックからは一切参照されない。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.config.models import EarningsSurpriseRulesConfig
from jstock_advisor.domain.entities.earnings_surprise import EarningsSurpriseResult
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    DividendComparisonOutcome,
    EarningsDateStatus,
    EarningsReleaseConfirmationState,
    EarningsSurpriseCategory,
    EarningsSurpriseEvaluationState,
)
from jstock_advisor.domain.jst import require_timezone_aware
from jstock_advisor.interfaces.types import EarningsSurpriseRecord

REASON_AWAITING_EARNINGS_CONFIRMATION = "AWAITING_EARNINGS_CONFIRMATION"
REASON_ANALYST_CONSENSUS_UNAVAILABLE = "ANALYST_CONSENSUS_UNAVAILABLE"
REASON_DIVIDEND_REVISION_UNAVAILABLE = "DIVIDEND_REVISION_UNAVAILABLE"

_AWAITING_STATES = (
    EarningsReleaseConfirmationState.AWAITING_CONFIRMATION,
    EarningsReleaseConfirmationState.DELAYED,
)

_DIVIDEND_SCORE_MAP_KEYS = (
    DividendComparisonOutcome.ACTUAL_DIVIDEND_CUT,
    DividendComparisonOutcome.FORECAST_DIVIDEND_CUT,
    DividendComparisonOutcome.DIVIDEND_MAINTAINED,
    DividendComparisonOutcome.DIVIDEND_INCREASE,
)


def _analyst_consensus_component(surprise_pct: float, config: EarningsSurpriseRulesConfig) -> float:
    if surprise_pct >= config.analyst_consensus_strong_positive_pct:
        return 100.0
    if surprise_pct >= config.analyst_consensus_positive_pct:
        return 50.0
    if surprise_pct > config.analyst_consensus_negative_pct:
        return 0.0
    if surprise_pct > config.analyst_consensus_strong_negative_pct:
        return -50.0
    return -100.0


def _dividend_component(
    outcome: DividendComparisonOutcome | None, config: EarningsSurpriseRulesConfig
) -> float | None:
    """DividendComparisonOutcomeを固定点数へ変換する。SPLIT_ADJUSTMENT_ONLY/
    COMPARISON_NOT_POSSIBLE(比較不能)/Noneはいずれも算出不可としてNoneを返す
    (0点として加算しない)。"""
    if outcome is None:
        return None
    mapping = dict(
        zip(
            _DIVIDEND_SCORE_MAP_KEYS,
            (
                config.dividend_actual_cut_score,
                config.dividend_forecast_cut_score,
                config.dividend_maintained_score,
                config.dividend_increase_score,
            ),
            strict=True,
        )
    )
    return mapping.get(outcome)


def _classify_category(
    score: float, config: EarningsSurpriseRulesConfig
) -> EarningsSurpriseCategory:
    t = config.category_thresholds
    if score >= t.strong_positive:
        return EarningsSurpriseCategory.STRONG_POSITIVE_SURPRISE
    if score >= t.positive:
        return EarningsSurpriseCategory.POSITIVE_SURPRISE
    if score <= t.strong_negative:
        return EarningsSurpriseCategory.STRONG_NEGATIVE_SURPRISE
    if score <= t.negative:
        return EarningsSurpriseCategory.NEGATIVE_SURPRISE
    return EarningsSurpriseCategory.NEUTRAL


def evaluate_earnings_surprise(
    earnings_surprise_history: list[EarningsSurpriseRecord],
    resolved_period_end: dt.date | None,
    dividend_comparison_outcome: DividendComparisonOutcome | None,
    earnings_date_status: EarningsDateStatus,
    release_confirmation_state: EarningsReleaseConfirmationState,
    evaluated_at: dt.datetime,
    config: EarningsSurpriseRulesConfig,
) -> EarningsSurpriseResult:
    """Analyst Consensus Surprise(直近確定四半期の実績EPS vs 決算発表前
    コンセンサス予想)とDividend Revision(配当予想の増額/減額)の加重平均で
    Earnings Surprise Scoreを算出する。

    resolved_period_endは`resolve_latest_financial_period_end()`
    (domain/signals/earnings_window.py)の戻り値(.period_end)を渡すこと。
    earnings_surprise_historyのうちresolved_period_endと同一のquarter_end
    を持つ記録のみを対象とする(古い四半期・未確定の四半期を誤って使わない
    ため)。

    earnings_date_status/release_confirmation_stateがともに「決算予定日を
    経過したが財務データへの反映が未確認」を示す場合、NOT_APPLICABLEを返し
    評価を意図的に見送る。
    """
    require_timezone_aware(evaluated_at)

    if (
        earnings_date_status == EarningsDateStatus.STALE_PAST_DATE
        and release_confirmation_state in _AWAITING_STATES
    ):
        return EarningsSurpriseResult(
            state=EarningsSurpriseEvaluationState.NOT_APPLICABLE,
            reason_codes=(REASON_AWAITING_EARNINGS_CONFIRMATION,),
            evaluated_at=evaluated_at,
            model_version=config.model_version,
        )

    reason_codes: set[str] = set()
    components: list[tuple[float, float]] = []

    matched = next(
        (r for r in earnings_surprise_history if r.quarter_end == resolved_period_end),
        None,
    )
    analyst_component: float | None = None
    if matched is not None and matched.surprise_pct is not None:
        analyst_component = _analyst_consensus_component(matched.surprise_pct, config)
        components.append((analyst_component, config.analyst_consensus_weight))
    else:
        reason_codes.add(REASON_ANALYST_CONSENSUS_UNAVAILABLE)

    dividend_component = _dividend_component(dividend_comparison_outcome, config)
    if dividend_component is not None:
        components.append((dividend_component, config.dividend_revision_weight))
    else:
        reason_codes.add(REASON_DIVIDEND_REVISION_UNAVAILABLE)

    total_config_weight = config.analyst_consensus_weight + config.dividend_revision_weight
    available_weight = sum(weight for _, weight in components)
    coverage = available_weight / total_config_weight if total_config_weight > 0 else 0.0

    if coverage < config.min_coverage_required:
        return EarningsSurpriseResult(
            state=EarningsSurpriseEvaluationState.NOT_EVALUATED,
            coverage=coverage,
            analyst_consensus_component=analyst_component,
            dividend_revision_component=dividend_component,
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

    return EarningsSurpriseResult(
        state=EarningsSurpriseEvaluationState.EVALUATED,
        score=score,
        category=category,
        confidence=confidence,
        coverage=coverage,
        analyst_consensus_component=analyst_component,
        dividend_revision_component=dividend_component,
        reason_codes=tuple(sorted(reason_codes)),
        evaluated_at=evaluated_at,
        model_version=config.model_version,
    )


def earnings_surprise_result_to_metrics(
    result: EarningsSurpriseResult,
) -> dict[str, object]:
    """EarningsSurpriseResultを、Recommendation.earnings_surprise_metrics
    (延いてはDecisionSnapshot.earnings_surprise_metrics)へ保存する監査用dict
    へ変換する。"""
    return {
        "state": result.state.value,
        "category": result.category.value if result.category is not None else None,
        "analyst_consensus_component": result.analyst_consensus_component,
        "dividend_revision_component": result.dividend_revision_component,
        "model_version": result.model_version,
    }


def earnings_surprise_config_values(config: EarningsSurpriseRulesConfig) -> dict[str, object]:
    """判定当時に実際に使用したEarnings Surprise Score設定値
    (Recommendation.config_values_used["earnings_surprise"]として保存する)。"""
    return {
        "model_version": config.model_version,
        "analyst_consensus_weight": config.analyst_consensus_weight,
        "dividend_revision_weight": config.dividend_revision_weight,
        "analyst_consensus_strong_negative_pct": config.analyst_consensus_strong_negative_pct,
        "analyst_consensus_negative_pct": config.analyst_consensus_negative_pct,
        "analyst_consensus_positive_pct": config.analyst_consensus_positive_pct,
        "analyst_consensus_strong_positive_pct": config.analyst_consensus_strong_positive_pct,
        "dividend_actual_cut_score": config.dividend_actual_cut_score,
        "dividend_forecast_cut_score": config.dividend_forecast_cut_score,
        "dividend_maintained_score": config.dividend_maintained_score,
        "dividend_increase_score": config.dividend_increase_score,
        "min_coverage_required": config.min_coverage_required,
        "coverage_high_threshold": config.coverage_high_threshold,
        "coverage_medium_threshold": config.coverage_medium_threshold,
        "category_thresholds": config.category_thresholds.model_dump(),
    }
