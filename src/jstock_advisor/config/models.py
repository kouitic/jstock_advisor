"""設定ファイル(config/*.yaml, config/*.json)に対応するpydanticモデル群。

すべてのモデルは `extra="forbid"` とし、YAML側のタイプミスや未知キーを
起動時に検出できるようにする。数値の閾値は要求仕様のデフォルト値をそのまま
Pythonのデフォルトとしては持たせず、YAMLの値のみを正とする(設定ファイル必須)。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- screening_rules.yaml ------------------------------------------------


class UniverseRules(StrictModel):
    exclude_reit: bool
    exclude_etf: bool
    min_avg_trading_value_20d_yen: int


class TotalYieldScreening(StrictModel):
    min_total_yield_pct: float


class FinancialHealthScreening(StrictModel):
    max_payout_ratio_pct: float
    min_equity_ratio_pct: float
    require_positive_operating_cashflow: bool
    exclude_negative_equity: bool
    exclude_deficit_companies: bool


class CorporateEventsScreening(StrictModel):
    exclude_recent_dividend_cut_announced: bool
    exclude_going_concern_doubt: bool
    scandal_or_delisting_risk_action: str


class IndustrySpecificRules(StrictModel):
    financial_sector_action: str
    target_industry_classification: list[str]


class DataQualityRules(StrictModel):
    max_data_age_business_days: int


class ScreeningRulesConfig(StrictModel):
    version: int
    universe: UniverseRules
    total_yield: TotalYieldScreening
    financial_health: FinancialHealthScreening
    corporate_events: CorporateEventsScreening
    industry_specific_rules: IndustrySpecificRules
    data_quality: DataQualityRules


# --- valuation_rules.yaml -------------------------------------------------


class FairValueMethods(StrictModel):
    enabled_methods: list[str]
    aggregation_method: str
    method_weights: dict[str, float]


class TargetYieldMethod(StrictModel):
    target_dividend_yield_pct: float
    target_total_yield_pct: float


class PerMethod(StrictModel):
    lookback_years_primary: int
    lookback_years_fallback: int


class PbrMethod(StrictModel):
    lookback_years_primary: int
    lookback_years_fallback: int


class HistoricalRangeMethod(StrictModel):
    lookback_years: int
    use_52_week_low: bool
    use_support_levels: bool


class RecommendedBuyPrice(StrictModel):
    tentative_buy_ratio: float
    standard_buy_ratio: float
    aggressive_buy_ratio: float
    enable_price_context_adjustment: bool


class DcfMethod(StrictModel):
    # 完全なCAPMは金利・ベータ等のデータソースが無いため実装不可(要求仕様8節の
    # フィージビリティ制約)。固定割引率による簡易DCFとし、信頼度はMEDIUM上限とする。
    discount_rate_pct: float
    terminal_growth_rate_pct: float
    projection_years: int


class FairValueUsability(StrictModel):
    max_method_spread_ratio: float  # 手法間の最大値/最小値がこの倍率以上なら使用不可
    min_methods_required: int  # 有効な手法数がこれ未満なら使用不可


class ValuationRulesConfig(StrictModel):
    version: int
    fair_value_methods: FairValueMethods
    target_yield_method: TargetYieldMethod
    per_method: PerMethod
    pbr_method: PbrMethod
    historical_range_method: HistoricalRangeMethod
    recommended_buy_price: RecommendedBuyPrice
    dcf_method: DcfMethod
    fair_value_usability: FairValueUsability


# --- profit_taking_rules.yaml ----------------------------------------------


class ProfitTakingThresholds(StrictModel):
    unrealized_gain_watch_pct: float
    unrealized_gain_partial_pct: float
    unrealized_gain_full_pct: float
    fair_value_excess_partial_pct: float
    fair_value_excess_full_pct: float
    total_yield_caution_pct: float
    total_yield_strong_caution_pct: float


class MitigatingFactor(StrictModel):
    enabled: bool
    downgrade_levels: int
    min_consecutive_years: int | None = None
    within_business_days: int | None = None


class MitigatingFactors(StrictModel):
    fair_value_rising_with_earnings_growth: MitigatingFactor
    continuous_dividend_increase: MitigatingFactor
    progressive_dividend_or_doe_policy: MitigatingFactor
    long_term_holding_benefit_imminent: MitigatingFactor
    few_reinvestment_alternatives: MitigatingFactor
    nisa_long_term_benefit: MitigatingFactor


class EventProximityNotice(StrictModel):
    dividend_record_date_within_business_days: int
    benefit_record_date_within_business_days: int
    earnings_announcement_within_business_days: int


class ConditionBasedJudgment(StrictModel):
    # PARTIALはこの件数以上の独立条件が該当した場合のみ成立する(要求仕様9節)
    min_conditions_for_partial: int
    # FULLは強い条件1つ、またはこの件数以上の中程度条件で成立する
    min_moderate_conditions_for_full: int
    # 適正価格ベースの期待リターンがこの%以下(例: -20%)ならFULLの強い条件とする
    forward_return_inferior_threshold_pct: float
    # 現在値が適正価格レンジ上限(bull)をこの%以上超えている場合、上昇トレンドによる
    # 判定緩和(timing_action)を禁止する(トレンドだけで割高評価を無効化しない)
    timing_downgrade_block_margin_pct: float


class ProfitTakingRulesConfig(StrictModel):
    version: int
    thresholds: ProfitTakingThresholds
    mitigating_factors: MitigatingFactors
    event_proximity_notice: EventProximityNotice
    condition_based_judgment: ConditionBasedJudgment


# --- sell_rules.yaml --------------------------------------------------------


class SellRule(StrictModel):
    enabled: bool
    severity: str
    threshold_pct: float | None = None
    consecutive_quarters: int | None = None
    yoy_increase_threshold_pct: float | None = None
    equity_ratio_critical_pct: float | None = None


class SellJudgmentPolicy(StrictModel):
    major_to_sell_min_count: int
    critical_to_urgent_review_min_count: int
    major_to_urgent_review_min_count: int


class SellRulesConfig(StrictModel):
    version: int
    rules: dict[str, SellRule]
    judgment: SellJudgmentPolicy
    disclosure_risk_keywords: list[str]


# --- scoring_weights.yaml ---------------------------------------------------


class ScoreWeights(StrictModel):
    total_yield_attractiveness: float
    dividend_sustainability: float
    financial_health: float
    undervaluation: float
    shareholder_benefit_value: float
    earnings_stability: float
    price_stability: float


class TotalYieldAttractivenessParams(StrictModel):
    full_score_total_yield_pct: float
    zero_score_total_yield_pct: float


class UndervaluationParams(StrictModel):
    criteria_considered: list[str]


class BenefitUtilityCoefficients(StrictModel):
    cash_equivalent: float
    versatile_point: float
    in_house_service: float
    in_house_product: float
    discount_voucher: float
    lottery_or_commemorative: float


class ShareholderBenefitValueParams(StrictModel):
    utility_coefficients_default: BenefitUtilityCoefficients


class ScoringWeightsConfig(StrictModel):
    version: int
    weights: ScoreWeights
    total_yield_attractiveness: TotalYieldAttractivenessParams
    undervaluation: UndervaluationParams
    shareholder_benefit_value: ShareholderBenefitValueParams


# --- schedule.yaml -----------------------------------------------------------


class ScheduleJob(StrictModel):
    description: str
    enabled: bool
    days: list[str]
    time: str | None = None
    times: list[str] | None = None
    run_only_on_first_occurrence_of_month: bool | None = None
    months: list[int] | None = None


class ScheduleConfig(StrictModel):
    version: int
    timezone: str
    jobs: dict[str, ScheduleJob]
    evaluation_horizons_business_days: dict[str, list[int]]


# --- notification_rules.yaml --------------------------------------------------


class NotificationRulesConfig(StrictModel):
    version: int
    resend_after_days: int
    price_change_resend_threshold_pct: float


# --- data_validation_rules.yaml -------------------------------------------


class SplitConsistencyConfig(StrictModel):
    lookback_years: int
    typical_split_ratios: list[float]
    ratio_tolerance_pct: float
    price_discontinuity_threshold_pct: float


class AnomalyDetectionConfig(StrictModel):
    fair_value_min_ratio: float
    fair_value_max_ratio: float
    fair_value_change_threshold_pct: float
    profit_take_price_change_threshold_pct: float
    reassessment_price_deviation_threshold_pct: float
    dividend_yield_change_threshold_pts: float
    eps_bps_dps_change_threshold_pct: float


class ConsistencyValidationConfig(StrictModel):
    full_take_extreme_margin_pct: float
    reevaluation_vs_full_take_max_margin_pct: float
    min_reasons_for_full_take_on_gain_alone: int


class DataValidationRulesConfig(StrictModel):
    version: int
    discrepancy_threshold_pct: float
    split_adjustment_lookback_days: int
    split_consistency: SplitConsistencyConfig
    anomaly_detection: AnomalyDetectionConfig
    consistency_validation: ConsistencyValidationConfig


# --- evaluation_rules.yaml --------------------------------------------------


class ExitEvaluationThresholds(StrictModel):
    decline_confirms_good_call_pct: float
    rally_flags_too_early_or_too_sensitive_pct: float


class EvaluationRulesConfig(StrictModel):
    version: int
    severe_decline_after_buy_pct: float
    exit_evaluation: ExitEvaluationThresholds


# --- holiday_calendar.json ----------------------------------------------------


class RecurringMarketClosures(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    dates_mm_dd: list[str]


class AdditionalClosures(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    dates: list[str]


class HolidayCalendarConfig(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    recurring_market_closures: RecurringMarketClosures
    additional_closures: AdditionalClosures


# --- stock_classification_rules.yaml -----------------------------------------


class IncomeClassificationRules(StrictModel):
    min_dividend_yield_pct: float
    max_payout_ratio_pct: float


class GrowthClassificationRules(StrictModel):
    min_consecutive_growth_quarters: int
    max_dividend_yield_pct: float


class ValueClassificationRules(StrictModel):
    max_pbr: float
    min_dividend_yield_pct: float


class CyclicalClassificationRules(StrictModel):
    industry_keywords: list[str]


class DefensiveClassificationRules(StrictModel):
    industry_keywords: list[str]


class TurnaroundClassificationRules(StrictModel):
    min_consecutive_improvement_quarters: int


class AssetPlayClassificationRules(StrictModel):
    max_pbr: float
    min_equity_ratio_pct: float


class EventDrivenClassificationRules(StrictModel):
    disclosure_keywords: list[str]


class StockClassificationRulesConfig(StrictModel):
    version: int
    income: IncomeClassificationRules
    growth: GrowthClassificationRules
    value: ValueClassificationRules
    cyclical: CyclicalClassificationRules
    defensive: DefensiveClassificationRules
    turnaround: TurnaroundClassificationRules
    asset_play: AssetPlayClassificationRules
    event_driven: EventDrivenClassificationRules


# --- momentum_rules.yaml ------------------------------------------------------


class MovingAveragesConfig(StrictModel):
    windows: list[int]
    slope_lookback_days: int


class HighLowConfig(StrictModel):
    high_window_days_short: int
    high_window_days_long: int
    drawdown_window_days: int


class VolumeConfig(StrictModel):
    short_window_days: int
    long_window_days: int


class RsiConfig(StrictModel):
    period: int
    overbought_threshold: float
    oversold_threshold: float


class MacdConfig(StrictModel):
    fast_period: int
    slow_period: int
    signal_period: int


class TrailingStopConfig(StrictModel):
    trailing_pct: float


class TrendClassificationConfig(StrictModel):
    strong_trend_rsi_threshold: float


class MomentumRulesConfig(StrictModel):
    version: int
    moving_averages: MovingAveragesConfig
    high_low: HighLowConfig
    volume: VolumeConfig
    rsi: RsiConfig
    macd: MacdConfig
    trailing_stop: TrailingStopConfig
    trend_classification: TrendClassificationConfig
    sector_etf_map: dict[str, str]


# --- confidence_rules.yaml -----------------------------------------------------


class ConfidenceScoringWeights(StrictModel):
    base_score: float
    penalty_stale_data: float
    penalty_low_primary_source_rate: float
    penalty_corporate_action_inconsistent: float
    penalty_financial_period_incomparable: float
    penalty_low_method_agreement: float
    penalty_missing_data: float
    penalty_untraced_one_time_factors: float
    penalty_cross_rule_disagreement: float
    high_threshold: float
    medium_threshold: float


class HighConfidenceDisallowRules(StrictModel):
    min_business_days_to_earnings: int
    max_days_since_split_for_unconfirmed_adjustment: int
    max_fair_value_method_spread_ratio: float
    min_independent_evidence_groups_for_high: int


class JudgmentSafetyLadderConfig(StrictModel):
    min_data_quality_score_for_strong_action: float
    max_latest_earnings_age_days: int
    min_business_days_to_earnings_for_strong_action: int


class ConfidenceRulesConfig(StrictModel):
    version: int
    scoring: ConfidenceScoringWeights
    max_data_freshness_days: int
    min_primary_source_rate: float
    high_confidence_disallow: HighConfidenceDisallowRules
    judgment_safety_ladder: JudgmentSafetyLadderConfig


class EarningsWindowRulesConfig(StrictModel):
    """決算直前・直後ルール(要求仕様14節)。"""

    version: int
    approaching_window_business_days: int
    recently_reported_calendar_days: int


# --- 集約 --------------------------------------------------------------------


class AppConfig(StrictModel):
    screening: ScreeningRulesConfig
    valuation: ValuationRulesConfig
    profit_taking: ProfitTakingRulesConfig
    sell: SellRulesConfig
    scoring: ScoringWeightsConfig
    schedule: ScheduleConfig
    notification: NotificationRulesConfig
    data_validation: DataValidationRulesConfig
    evaluation: EvaluationRulesConfig
    holiday_calendar: HolidayCalendarConfig
    stock_classification: StockClassificationRulesConfig
    momentum: MomentumRulesConfig
    confidence: ConfidenceRulesConfig
    earnings_window: EarningsWindowRulesConfig
