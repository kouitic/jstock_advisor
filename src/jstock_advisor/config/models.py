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


class ValuationRulesConfig(StrictModel):
    version: int
    fair_value_methods: FairValueMethods
    target_yield_method: TargetYieldMethod
    per_method: PerMethod
    pbr_method: PbrMethod
    historical_range_method: HistoricalRangeMethod
    recommended_buy_price: RecommendedBuyPrice


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


class ProfitTakingRulesConfig(StrictModel):
    version: int
    thresholds: ProfitTakingThresholds
    mitigating_factors: MitigatingFactors
    event_proximity_notice: EventProximityNotice


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


class DataValidationRulesConfig(StrictModel):
    version: int
    discrepancy_threshold_pct: float
    split_adjustment_lookback_days: int


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
