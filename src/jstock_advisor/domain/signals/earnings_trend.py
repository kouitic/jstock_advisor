"""判定精度向上機能Phase C: Earnings Trend Score v1(業績トレンドスコア)。

Earnings Surprise Score(earnings_surprise.py)とは独立した評価軸。実装前
調査の結果、営業利益トレンド・営業CFトレンド・配当方向の3成分(+補助的な
acceleration成分)で構成する(売上トレンド・EPSトレンド・利益率改善・会社
予想方向は現行Providerでは算出できないため対象外。domain/entities/
earnings_trend.py参照)。

look-ahead bias防止: Earnings Surprise Scoreと同様、最新決算が確定反映
されたかどうかはこの関数では判定せず、呼び出し側が
`EarningsReleaseConfirmationState`を解決したうえで渡す。決算予定日を経過
していながら財務データへの反映が未確認の場合、NOT_APPLICABLEを返す。

外部I/Oを一切行わない純関数(domain/signals/timing_score.pyと同じパターン)。

コードレビュー対応(Shadow計測): この評価結果はDecisionSnapshotへ記録する
専用のものであり、BUY候補判定・保有判断スコア・旧売却判定・ProfitTaking
判定・LINE通知など既存の判定ロジックからは一切参照されない。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.config.models import EarningsTrendRulesConfig
from jstock_advisor.domain.entities.earnings_trend import EarningsTrendResult
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    DividendComparisonOutcome,
    EarningsDateStatus,
    EarningsReleaseConfirmationState,
    EarningsTrendCategory,
    EarningsTrendEvaluationState,
)
from jstock_advisor.domain.jst import require_timezone_aware

REASON_AWAITING_EARNINGS_CONFIRMATION = "AWAITING_EARNINGS_CONFIRMATION"
REASON_OPERATING_INCOME_TREND_UNAVAILABLE = "OPERATING_INCOME_TREND_UNAVAILABLE"
REASON_OPERATING_CASHFLOW_TREND_UNAVAILABLE = "OPERATING_CASHFLOW_TREND_UNAVAILABLE"
REASON_DIVIDEND_DIRECTION_UNAVAILABLE = "DIVIDEND_DIRECTION_UNAVAILABLE"
REASON_ACCELERATION_UNAVAILABLE = "ACCELERATION_UNAVAILABLE"

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


def _clamp(value: float, low: float = -100.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _change_pct(previous: Decimal, latest: Decimal) -> float | None:
    if previous == 0:
        return None
    return float(latest / previous - 1) * 100.0


def _banded_score(change_pct: float, config: EarningsTrendRulesConfig) -> float:
    if change_pct >= config.trend_strong_improve_pct:
        return 100.0
    if change_pct >= config.trend_improve_pct:
        return 50.0
    if change_pct > config.trend_decline_pct:
        return 0.0
    if change_pct > config.trend_strong_decline_pct:
        return -50.0
    return -100.0


def _trend_component(
    series: list[Decimal], config: EarningsTrendRulesConfig
) -> float | None:
    """seriesは時系列昇順(最後が最新)を前提とする(domain/financial_series.py
    のto_seasonally_adjusted_series()と同じ規約)。直近期の前期比変化率を
    段階評価する。"""
    if len(series) < 2:
        return None
    change_pct = _change_pct(series[-2], series[-1])
    if change_pct is None:
        return None
    return _banded_score(change_pct, config)


def _acceleration_component(
    series: list[Decimal], config: EarningsTrendRulesConfig
) -> float | None:
    """直近の前期比変化率と、その1つ前の前期比変化率の差(2階差分)を評価する。
    最低3四半期分のデータが必要(データが薄いため補助成分として扱う)。"""
    if len(series) < 3:
        return None
    change_latest = _change_pct(series[-2], series[-1])
    change_previous = _change_pct(series[-3], series[-2])
    if change_latest is None or change_previous is None:
        return None
    delta2 = change_latest - change_previous
    return _clamp(delta2 / config.acceleration_full_scale_pct * 100.0)


def _dividend_component(
    outcome: DividendComparisonOutcome | None, config: EarningsTrendRulesConfig
) -> float | None:
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


def _classify_category(score: float, config: EarningsTrendRulesConfig) -> EarningsTrendCategory:
    t = config.category_thresholds
    if score >= t.strong_improving:
        return EarningsTrendCategory.STRONG_IMPROVING
    if score >= t.improving:
        return EarningsTrendCategory.IMPROVING
    if score <= t.strong_deteriorating:
        return EarningsTrendCategory.STRONG_DETERIORATING
    if score <= t.deteriorating:
        return EarningsTrendCategory.DETERIORATING
    return EarningsTrendCategory.STABLE


def evaluate_earnings_trend(
    quarterly_operating_incomes: list[Decimal],
    quarterly_operating_cashflows: list[Decimal],
    dividend_comparison_outcome: DividendComparisonOutcome | None,
    earnings_date_status: EarningsDateStatus,
    release_confirmation_state: EarningsReleaseConfirmationState,
    evaluated_at: dt.datetime,
    config: EarningsTrendRulesConfig,
) -> EarningsTrendResult:
    """営業利益トレンド・営業CFトレンド・配当方向・(補助的な)accelerationの
    加重平均でEarnings Trend Scoreを算出する。

    quarterly_operating_incomes/quarterly_operating_cashflowsは季節調整済み
    (TTM)系列(StockSnapshot.quarterly_operating_incomes/
    quarterly_operating_cashflows、時系列昇順)を渡すこと。

    earnings_date_status/release_confirmation_stateがともに「決算予定日を
    経過したが財務データへの反映が未確認」を示す場合、NOT_APPLICABLEを返し
    評価を意図的に見送る(Earnings Surprise Scoreと同じ前提条件)。
    """
    require_timezone_aware(evaluated_at)

    if (
        earnings_date_status == EarningsDateStatus.STALE_PAST_DATE
        and release_confirmation_state in _AWAITING_STATES
    ):
        return EarningsTrendResult(
            state=EarningsTrendEvaluationState.NOT_APPLICABLE,
            reason_codes=(REASON_AWAITING_EARNINGS_CONFIRMATION,),
            evaluated_at=evaluated_at,
            model_version=config.model_version,
        )

    reason_codes: set[str] = set()
    components: list[tuple[float, float]] = []

    income_component = _trend_component(quarterly_operating_incomes, config)
    if income_component is not None:
        components.append((income_component, config.operating_income_trend_weight))
    else:
        reason_codes.add(REASON_OPERATING_INCOME_TREND_UNAVAILABLE)

    cashflow_component = _trend_component(quarterly_operating_cashflows, config)
    if cashflow_component is not None:
        components.append((cashflow_component, config.operating_cashflow_trend_weight))
    else:
        reason_codes.add(REASON_OPERATING_CASHFLOW_TREND_UNAVAILABLE)

    dividend_component = _dividend_component(dividend_comparison_outcome, config)
    if dividend_component is not None:
        components.append((dividend_component, config.dividend_direction_weight))
    else:
        reason_codes.add(REASON_DIVIDEND_DIRECTION_UNAVAILABLE)

    acceleration_component = _acceleration_component(quarterly_operating_incomes, config)
    if acceleration_component is not None:
        components.append((acceleration_component, config.acceleration_weight))
    else:
        reason_codes.add(REASON_ACCELERATION_UNAVAILABLE)

    total_config_weight = (
        config.operating_income_trend_weight
        + config.operating_cashflow_trend_weight
        + config.dividend_direction_weight
        + config.acceleration_weight
    )
    available_weight = sum(weight for _, weight in components)
    coverage = available_weight / total_config_weight if total_config_weight > 0 else 0.0

    if coverage < config.min_coverage_required:
        return EarningsTrendResult(
            state=EarningsTrendEvaluationState.NOT_EVALUATED,
            coverage=coverage,
            operating_income_trend_component=income_component,
            operating_cashflow_trend_component=cashflow_component,
            dividend_direction_component=dividend_component,
            acceleration_component=acceleration_component,
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

    return EarningsTrendResult(
        state=EarningsTrendEvaluationState.EVALUATED,
        score=score,
        category=category,
        confidence=confidence,
        coverage=coverage,
        operating_income_trend_component=income_component,
        operating_cashflow_trend_component=cashflow_component,
        dividend_direction_component=dividend_component,
        acceleration_component=acceleration_component,
        reason_codes=tuple(sorted(reason_codes)),
        evaluated_at=evaluated_at,
        model_version=config.model_version,
    )


def earnings_trend_result_to_metrics(result: EarningsTrendResult) -> dict[str, object]:
    """EarningsTrendResultを、Recommendation.earnings_trend_metrics
    (延いてはDecisionSnapshot.earnings_trend_metrics)へ保存する監査用dict
    へ変換する。"""
    return {
        "state": result.state.value,
        "category": result.category.value if result.category is not None else None,
        "operating_income_trend_component": result.operating_income_trend_component,
        "operating_cashflow_trend_component": result.operating_cashflow_trend_component,
        "dividend_direction_component": result.dividend_direction_component,
        "acceleration_component": result.acceleration_component,
        "model_version": result.model_version,
    }


def earnings_trend_config_values(config: EarningsTrendRulesConfig) -> dict[str, object]:
    """判定当時に実際に使用したEarnings Trend Score設定値
    (Recommendation.config_values_used["earnings_trend"]として保存する)。"""
    return {
        "model_version": config.model_version,
        "operating_income_trend_weight": config.operating_income_trend_weight,
        "operating_cashflow_trend_weight": config.operating_cashflow_trend_weight,
        "dividend_direction_weight": config.dividend_direction_weight,
        "acceleration_weight": config.acceleration_weight,
        "trend_strong_decline_pct": config.trend_strong_decline_pct,
        "trend_decline_pct": config.trend_decline_pct,
        "trend_improve_pct": config.trend_improve_pct,
        "trend_strong_improve_pct": config.trend_strong_improve_pct,
        "acceleration_full_scale_pct": config.acceleration_full_scale_pct,
        "dividend_actual_cut_score": config.dividend_actual_cut_score,
        "dividend_forecast_cut_score": config.dividend_forecast_cut_score,
        "dividend_maintained_score": config.dividend_maintained_score,
        "dividend_increase_score": config.dividend_increase_score,
        "min_coverage_required": config.min_coverage_required,
        "coverage_high_threshold": config.coverage_high_threshold,
        "coverage_medium_threshold": config.coverage_medium_threshold,
        "category_thresholds": config.category_thresholds.model_dump(),
    }
