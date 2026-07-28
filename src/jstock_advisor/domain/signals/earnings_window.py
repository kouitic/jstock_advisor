"""決算直前・直後ルール(要求仕様14節)。

決算発表直前は情報の陳腐化リスクが高く、発表直後は最新の実績が判定に
反映されていない可能性があるため、通常の判定に対して特別な扱いをする。

RECENTLY_REPORTEDの判定は、実際の決算発表日ではなく取得できた直近四半期の
期末日(fiscal_period_end)を代理指標として用いた近似である。yfinance/EDINET
いずれも決算発表日そのものは提供しないため、これが取得可能な最良の代替指標
である(推測で補完しない、という既存方針の範囲内での近似)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from jstock_advisor.config.models import EarningsWindowRulesConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import EarningsWindowStatus, RecommendationType

_BUY_LIKE = (RecommendationType.BUY, RecommendationType.WATCH_BUY)
_PROFIT_TAKE_LIKE = (RecommendationType.PARTIAL_PROFIT_TAKE, RecommendationType.FULL_PROFIT_TAKE)
_REVIEW_ELIGIBLE = (RecommendationType.HOLD, RecommendationType.WATCH)


@dataclass(frozen=True)
class EarningsWindowEvaluation:
    status: EarningsWindowStatus
    business_days_to_next_earnings: int | None
    days_since_latest_quarter_end: int | None


def evaluate_earnings_window(
    as_of: dt.date,
    calendar: BusinessCalendar,
    config: EarningsWindowRulesConfig,
    next_earnings_date: dt.date | None = None,
    latest_quarter_end: dt.date | None = None,
) -> EarningsWindowEvaluation:
    business_days_to_next_earnings = None
    if next_earnings_date is not None and next_earnings_date >= as_of:
        business_days_to_next_earnings = calendar.business_days_between(as_of, next_earnings_date)

    days_since_latest_quarter_end = None
    if latest_quarter_end is not None and latest_quarter_end <= as_of:
        days_since_latest_quarter_end = (as_of - latest_quarter_end).days

    if (
        business_days_to_next_earnings is not None
        and business_days_to_next_earnings <= config.approaching_window_business_days
    ):
        status = EarningsWindowStatus.APPROACHING_EARNINGS
    elif (
        days_since_latest_quarter_end is not None
        and days_since_latest_quarter_end <= config.recently_reported_calendar_days
    ):
        status = EarningsWindowStatus.RECENTLY_REPORTED
    else:
        status = EarningsWindowStatus.NONE

    return EarningsWindowEvaluation(
        status=status,
        business_days_to_next_earnings=business_days_to_next_earnings,
        days_since_latest_quarter_end=days_since_latest_quarter_end,
    )


def recommend_earnings_aware_action(
    base_recommendation: RecommendationType,
    window: EarningsWindowEvaluation,
) -> RecommendationType:
    """決算直前・直後であることを理由に、通常の判定を必要な範囲でのみ上書きする。

    投資前提の悪化を根拠とするSELL/URGENT_REVIEWは、決算直前であっても
    タイミングを理由に抑制しない(悪化は既に確認済みの事実であり、決算を
    待つこと自体がリスクになりうるため)。
    """
    if window.status == EarningsWindowStatus.APPROACHING_EARNINGS:
        if base_recommendation in _BUY_LIKE:
            return RecommendationType.WATCH_BEFORE_EARNINGS
        if base_recommendation in _PROFIT_TAKE_LIKE:
            return RecommendationType.PARTIAL_RISK_REDUCTION
        return base_recommendation
    if window.status == EarningsWindowStatus.RECENTLY_REPORTED:
        if base_recommendation in _REVIEW_ELIGIBLE:
            return RecommendationType.REVIEW_AFTER_EARNINGS
        return base_recommendation
    return base_recommendation
