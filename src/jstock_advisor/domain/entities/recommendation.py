"""推奨記録(要求仕様26節)。推奨時点の情報を変更不能なスナップショットとして保存する。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.buy_decision import BuyDecisionReason
from jstock_advisor.domain.entities.common import (
    BuyPriceLevels,
    DataSourceReference,
    MarginAdjustment,
    ScoreBreakdown,
    SellPriceLevels,
)
from jstock_advisor.domain.entities.enums import (
    BUY_FAMILY_ACTIONS,
    AddOnEligibility,
    BuyAction,
    BuyIndustrySector,
    BuyPriceReliability,
    CandidateSource,
    ConfidenceLevel,
    DividendComparisonOutcome,
    EarningsDateStatus,
    ProfitTakingIndustrySector,
    RecommendationType,
    RecordDateUnknownReason,
)
from jstock_advisor.domain.entities.valuation import FairValueMethodResult


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

    # --- BUYパイプライン再設計(2026-07)で追加。要求仕様18節。
    # 「企業として投資候補になり得るか(company_quality_score)」と「現在の株価で
    # 実際に購入すべきか(purchase_attractiveness_score + buy_action)」を分離する ---
    buy_action: BuyAction | None = None
    # 格下げ・決算直前調整前の生の判定。格下げが無かった場合はbuy_actionと同じ値になる。
    raw_buy_action: BuyAction | None = None

    company_quality_score: float | None = None
    purchase_attractiveness_score: float | None = None

    # 適正価格の集約値。単一の「最終適正価格」を断定的に扱わず、レンジと
    # 購入判断基準価格(valuation_anchor)を分けて保持する。
    valuation_anchor: Decimal | None = None
    valuation_min: Decimal | None = None
    valuation_max: Decimal | None = None
    valuation_dispersion_ratio: Decimal | None = None

    entry_buy_price: Decimal | None = None
    standard_buy_price: Decimal | None = None
    strong_buy_price: Decimal | None = None

    current_vs_valuation_pct: Decimal | None = None
    current_vs_entry_price_pct: Decimal | None = None

    required_margin_of_safety_entry: Decimal | None = None
    required_margin_of_safety_standard: Decimal | None = None
    required_margin_of_safety_strong: Decimal | None = None
    margin_adjustments: tuple[MarginAdjustment, ...] = ()

    business_days_to_earnings: int | None = None

    # 購入判断における業種別モデルの区分(利確判定用industry_sectorとは別軸)。
    # industry_model_appliedは既存フィールドをBUY側でも共通利用する。
    buy_industry_sector: BuyIndustrySector | None = None

    # 平準化EPS(要求仕様13節)。景気循環銘柄等で単年度予想EPSのみに依らないため。
    forecast_eps: Decimal | None = None
    normalized_eps: Decimal | None = None
    eps_normalization_method: str | None = None

    # 適正価格の算出方式別結果(目標配当利回り/PER/PBR/過去株価レンジ/DCF/業種別)。
    valuation_methods: tuple[FairValueMethodResult, ...] = ()

    # 構造化された購入判断理由。通知層はこれを再計算せずそのまま表示する。
    buy_decision_reasons: tuple[BuyDecisionReason, ...] = ()

    # --- BUYパイプライン第2次修正(2026-07)で追加 ---
    # 買付価格3段階の信頼性(安全余裕率が上限に張り付いた・手法間バラつきが
    # 大きい・有効な算出方式が少ない等)。LOWの場合はBUY系判定を禁止する。
    buy_price_reliability: BuyPriceReliability | None = None

    # 下方外れ値除外後、購入判断に実際に使用した適正価格レンジ
    # (valuation_min/valuation_maxは全採用方式ベースの参考値のまま維持)。
    decision_valuation_min: Decimal | None = None
    decision_valuation_max: Decimal | None = None

    # 「打診買い価格まで、あと何%下落が必要か」(current_vs_entry_price_pctとは
    # 意味が異なる別指標。current_vs_entry_price_pctは「現在値がentryを何%
    # 上回っているか」であり、通知の「まで」という接近方向の文言には使えない)。
    required_decline_to_entry_pct: Decimal | None = None

    # 次回決算予定日の妥当性検証結果(要求仕様12節: 評価日より過去の決算日を
    # 「次回決算予定日」として表示しない)。earnings_date_rawは検証前の生値
    # (監査用、STALE_PAST_DATEの場合でもnext_earnings_dateはNoneのまま)。
    earnings_date_status: EarningsDateStatus | None = None
    earnings_date_raw: dt.date | None = None

    # --- 気になる銘柄と保有銘柄の統合BUY候補パイプライン(2026-07)で追加。
    # 既存レコード(candidate_source欠落)は表示層でWATCHLIST扱いにフォールバック
    # する(データ自体は書き換えない、後方互換)---
    candidate_source: CandidateSource | None = None

    # 保有由来フィールド(sourceがHOLDING/BOTHの場合のみ設定。表示・買い増し
    # リスク判定の参考情報としてのみ使い、適正価格算出へは一切使用しない)。
    holding_quantity: int | None = None
    average_acquisition_price: Decimal | None = None
    current_market_value: Decimal | None = None
    unrealized_profit_loss: Decimal | None = None
    unrealized_profit_loss_pct: Decimal | None = None

    # 買い増し後構成比の前提(最低売買単位1単元を仮定。資金余力が不明なため
    # 2単元以上は仮定しない)。
    projection_basis: str | None = None
    projected_add_on_quantity: int | None = None
    projected_add_on_price: Decimal | None = None
    projected_add_on_amount: Decimal | None = None
    projected_investment_amount: Decimal | None = None
    current_position_ratio: Decimal | None = None
    projected_position_ratio: Decimal | None = None
    current_sector_ratio: Decimal | None = None
    projected_sector_ratio: Decimal | None = None

    # 共通購入判断の生の結果(買い増し固有リスクゲート適用前)。buy_actionは
    # 引き続き最終判定(final_buy_action)として使う。
    base_buy_action: BuyAction | None = None
    add_on_eligibility: AddOnEligibility | None = None
    add_on_block_reasons: tuple[str, ...] = ()
    conflicting_holding_action: RecommendationType | None = None

    # 安全な資金配分ロジックが存在しないため、常にNoneのまま保持する
    # (推奨購入数量の自動提案は今回のスコープ外。将来拡張用に予約)。
    recommended_add_on_quantity: int | None = None
    recommended_add_on_amount: Decimal | None = None

    @property
    def recommended(self) -> bool:
        """買い候補として現在購入可能かどうかの派生値(直接設定不可)。

        判定の正本はbuy_actionであり、このプロパティはbuy_actionから導出する
        だけの読み取り専用値とする(要求仕様2節: recommendedを直接更新する
        設計は廃止)。BUY系以外の推奨タイプ(利確・売却等)ではbuy_actionが
        Noneのため常にFalseを返す。
        """
        if self.buy_action is None:
            return False
        return self.buy_action in BUY_FAMILY_ACTIONS
