"""推奨記録(要求仕様26節)。推奨時点の情報を変更不能なスナップショットとして保存する。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.common import (
    BuyPriceLevels,
    DataSourceReference,
    ScoreBreakdown,
    SellPriceLevels,
)
from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType


class Recommendation(ImmutableSnapshot):
    recommendation_id: str
    stock_code: str
    stock_name: str
    recommended_at: dt.datetime
    recommendation_type: RecommendationType

    buy_prices: BuyPriceLevels | None = None
    sell_prices: SellPriceLevels | None = None

    price_at_recommendation: Decimal
    average_purchase_price_at_recommendation: Decimal | None = None
    shares_at_recommendation: int | None = None

    dividend_yield_pct_at_recommendation: float | None = None
    shareholder_benefit_yield_pct_at_recommendation: float | None = None
    total_yield_pct_at_recommendation: float | None = None
    fair_value_at_recommendation: Decimal | None = None

    total_score: float | None = None
    score_breakdown: ScoreBreakdown | None = None

    reasons: list[str] = []
    counter_factors: list[str] = []  # 反対材料
    key_risks: list[str] = []
    confidence: ConfidenceLevel

    next_earnings_date: dt.date | None = None
    dividend_record_date: dt.date | None = None
    benefit_record_date: dt.date | None = None

    rule_version: str
    config_values_used: dict[str, Any] = {}
    data_sources: list[DataSourceReference] = []
