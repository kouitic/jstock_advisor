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
    ProfitTakingIndustrySector,
    RecommendationType,
    RecordDateUnknownReason,
)


class Recommendation(ImmutableSnapshot):
    recommendation_id: str
    stock_code: str
    stock_name: str
    recommended_at: dt.datetime
    recommendation_type: RecommendationType
    # 格下げ前の生の判定(2026-07仕様レビュー対応)。格下げが無かった場合は
    # recommendation_typeと同じ値になる。
    raw_recommendation_type: RecommendationType | None = None

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

    # --- 利確判定レビュー対応で追加: 適正価格の内訳を通知へ表示するため ---
    fair_value_bear: Decimal | None = None
    fair_value_neutral: Decimal | None = None
    fair_value_bull: Decimal | None = None
    fair_value_overall_confidence: ConfidenceLevel | None = None
    fair_value_methods: list[dict[str, Any]] = []  # method/fair_value/confidence/exclusion_reason
    fair_value_spread_ratio: float | None = None

    # --- 増配実績と増配予想の分離(2026-07仕様レビュー対応) ---
    consecutive_actual_dividend_increase_years: int | None = None
    forecast_dividend_increase: bool | None = None
    forecast_dividend_increase_rate: float | None = None

    # --- 配当・優待基準日の推定ラベル(2026-07仕様レビュー対応)。正確な次回日付が
    # 不明でも、決算期末等の一次情報から基準月・基準日の周期パターンが分かる場合、
    # 単なる「不明」ではなくこのラベルを表示する ---
    dividend_record_date_recurring_label: str | None = None
    benefit_record_date_recurring_label: str | None = None

    # --- WATCH通知フォーマット刷新(2026-07仕様レビュー対応)。「保有継続を支持する
    # 要因」(counter_factors)とは別に、「直ちに利確しない理由」(まだ強い判定へ
    # 進めない理由)を明示する ---
    not_yet_action_reasons: list[str] = []

    # --- 利確判定エンジン再レビュー対応(2026-07)で追加 ---
    # 現在株価が中立/強気適正価格をどれだけ超過(または下回る)しているか。
    # 監視開始価格(閾値ベースの価格)ではなく、必ず実際の現在株価を使って算出する。
    current_price_vs_neutral_fair_value_pct: float | None = None
    current_price_vs_bull_fair_value_pct: float | None = None

    # 保有株数・売買単位を考慮した一部売却の実行可能性
    trading_unit: int | None = None
    minimum_sellable_shares: int | None = None
    partial_sale_executable: bool | None = None
    suggested_sell_shares: int | None = None
    odd_lot_trading_available: bool | None = None

    # 業種別適正価格モデルの適用状況(未対応の場合は信頼度HIGH・適正価格単独での
    # PARTIAL以上を禁止するゲートに使う)
    industry_sector: ProfitTakingIndustrySector | None = None
    industry_model_applied: bool | None = None
    industry_model_missing_reason: str | None = None

    # 配当減少の表示文言(実績確定/予想のみ/内訳不明の総額減少、を区別する)
    dividend_decrease_explanation: str | None = None

    # ポートフォリオ内保有比率(企業価値判断とは別の集中リスク通知に使う)
    portfolio_weight_pct: float | None = None
    portfolio_acquisition_cost_weight_pct: float | None = None
