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
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    DividendComparisonOutcome,
    RecommendationType,
    RecordDateUnknownReason,
)


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

    # --- 通知本文の改善(要求仕様16節)で追加。確認事項を比較年度付きで表示するため ---
    dividend_comparison_source_fiscal_year: str | None = None
    dividend_comparison_target_fiscal_year: str | None = None
    dividend_comparison_outcome: DividendComparisonOutcome | None = None
    dividend_record_date_unknown_reason: RecordDateUnknownReason | None = None
    benefit_record_date_unknown_reason: RecordDateUnknownReason | None = None

    rule_version: str
    config_values_used: dict[str, Any] = {}
    data_sources: list[DataSourceReference] = []

    # --- 通知層の自動生成文言廃止(2026-07仕様§9)で追加。判定結果の文言は
    # 通知層(line_notification_service)で生成せず、ここに判定サービスが直接格納する ---
    recommended_action_summary: str | None = None
    next_review_conditions: list[str] = []
    holding_risks: list[str] = []
    evidence_details: list[dict[str, Any]] = []
    independent_evidence_group_count: int | None = None
