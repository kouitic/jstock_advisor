"""設定ファイル(config/*.yaml, config/*.json)に対応するpydanticモデル群。

すべてのモデルは `extra="forbid"` とし、YAML側のタイプミスや未知キーを
起動時に検出できるようにする。数値の閾値は要求仕様のデフォルト値をそのまま
Pythonのデフォルトとしては持たせず、YAMLの値のみを正とする(設定ファイル必須)。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    dcf_method: DcfMethod
    fair_value_usability: FairValueUsability


# --- profit_taking_rules.yaml ----------------------------------------------


class ProfitTakingThresholds(StrictModel):
    unrealized_gain_watch_pct: float
    unrealized_gain_partial_pct: float
    unrealized_gain_full_pct: float
    # --- 利確判定レビュー再対応(2026-07): 中立値ではなく強気適正価格を主軸にする ---
    # 強気適正価格をこの%以上超過した場合にPARTIAL候補水準とする
    fair_value_excess_partial_pct: float
    # 強気適正価格をこの%以上超過した場合にFULL候補水準とする
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
    # 適正価格ベースの期待リターンがこの%以下(例: -20%)ならFULLの強い条件の候補とする
    # (単独では不十分。他の複数条件と併せて要求する。利確判定レビュー対応)
    forward_return_inferior_threshold_pct: float
    # 現在値が適正価格レンジ上限(bull)をこの%以上超えている場合、上昇トレンドによる
    # 判定緩和(timing_action)を禁止する(トレンドだけで割高評価を無効化しない)
    timing_downgrade_block_margin_pct: float
    # --- 利確判定レビュー対応で追加: 中立適正価格単独でのFULL強条件化を廃止し、
    # 以下の複数条件をすべて満たす場合にのみ適正価格ベースの強いFULL条件とする ---
    min_fair_value_methods_for_full: int
    max_fair_value_spread_ratio_for_full: float
    bull_excess_margin_pct_for_full: float
    # --- 利確判定エンジン再レビュー対応(2026-07)で追加: MEDIUM信頼度でも
    # 適正価格ベースでPARTIAL相当を許可するための追加ゲート(要求仕様§5) ---
    min_business_days_to_earnings_for_fair_value_action: int
    min_fair_value_methods_for_partial: int
    max_fair_value_spread_ratio_for_partial: float


class TradingUnitRules(StrictModel):
    """保有株数・売買単位を考慮した実行可能性(2026-07仕様レビュー対応)。

    TSE上場銘柄の単元株数は2018年10月に全銘柄100株へ統一済みであり、
    Providerから個別に取得する手段が無いためこの既知の制度的事実を既定値とする。
    単元未満株取引(証券会社のミニ株サービス等)が実際に利用可能かどうかは
    銘柄・口座ごとに異なり自動判定できないため、既定はFalse(捏造しない)とする。
    """

    default_trading_unit: int
    default_odd_lot_trading_available: bool


class ProfitTakingRulesConfig(StrictModel):
    version: int
    thresholds: ProfitTakingThresholds
    mitigating_factors: MitigatingFactors
    event_proximity_notice: EventProximityNotice
    condition_based_judgment: ConditionBasedJudgment
    trading_unit: TradingUnitRules


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


class BuyCandidatesNotificationConfig(StrictModel):
    """BUY候補バッチ専用の通知設定(BUYパイプライン第3次修正2026-07で追加)。

    notify_data_errors: 個別銘柄のデータ取得エラーをLINEへ個別送信するか
    (既定false。「今日買える銘柄だけを通知する」方針のため、データ取得エラーは
    CloudWatch警告ログ+バッチサマリーの内訳件数のみとし、個別LINE送信はしない)。
    """

    notify_data_errors: bool


class OperationsNotificationConfig(StrictModel):
    """運用障害通知の設定(BUYパイプライン第3次修正2026-07で追加)。

    notify_batch_failure: バッチ全体が異常終了した場合の運用向け通知を
    行うかどうか。BUY候補の個別データ取得エラー(buy_candidates.notify_data_errors)
    とは目的が異なるため、意図的に別設定として分離している。

    shareholder_benefit_registry_min_expected_entries: 株主優待レジストリの
    読み込み件数がこれ未満の場合にWARNINGログを出す(2026-07仕様レビュー対応:
    CSVは用意されているのにレジストリへ未反映という運用ミスをすぐ検知できる
    ようにするため)。0で無効化(WARNINGのみ無効化。件数のINFOログは常に出る)。
    """

    notify_batch_failure: bool
    shareholder_benefit_registry_min_expected_entries: int


class NotificationRulesConfig(StrictModel):
    version: int
    resend_after_days: int
    price_change_resend_threshold_pct: float
    buy_candidate_max_notifications_per_run: int
    # --- BUYパイプライン第2次修正(2026-07)で追加。要求仕様16節: 購入候補が
    # 0件の場合、原則としてバッチ完了サマリー自体をLINEへ送らない
    # (運用確認のため送りたい場合のみtrueにする) ---
    send_empty_summary: bool
    buy_candidates: BuyCandidatesNotificationConfig
    operations: OperationsNotificationConfig
    # --- 統合BUY候補パイプライン(2026-07)で追加。要求仕様§15: 気になる銘柄・
    # 保有銘柄それぞれをBUY候補評価対象へ含めるかを個別に制御できるようにする ---
    include_watchlist: bool
    include_holdings: bool
    # --- 通知の正確性・説明可能性の修正(2026-07仕様レビュー対応)で追加。
    # WATCH通知で手法間乖離が大きい/信頼度が低い場合に注意書きを表示する閾値
    # (強気/弱気の比率がこの値以上で「手法間の推定差が大きい」とみなす) ---
    fair_value_large_spread_ratio: float


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
    # --- 利確判定エンジン再レビュー対応(2026-07)で追加: 決算までこの営業日数以内の
    # 場合、通常のPARTIAL/FULL_PROFIT_TAKE提案を保留してREVIEW_BEFORE_EARNINGSへ、
    # WATCHをWATCH_BEFORE_EARNINGSへ調整する(公式確認済みの即時criticalは例外) ---
    profit_taking_suppression_business_days: int


class PortfolioConcentrationRulesConfig(StrictModel):
    """ポートフォリオ集中リスク判定(2026-07仕様レビュー対応・要求仕様§14)。"""

    version: int
    single_stock_weight_threshold_pct: float


# --- buy_decision_rules.yaml (2026-07 BUYパイプライン再設計) ------------------


class BuyScoreThresholds(StrictModel):
    strong_buy: float
    buy: float
    small_entry: float
    watch: float

    @model_validator(mode="after")
    def _check_order(self) -> BuyScoreThresholds:
        if not (self.strong_buy >= self.buy >= self.small_entry >= self.watch):
            raise ValueError(
                "score_thresholdsはstrong_buy >= buy >= small_entry >= watchの順序が必要です"
            )
        return self


class ValuationDispersionThresholds(StrictModel):
    low_max: float
    medium_max: float
    auto_buy_block: float

    @model_validator(mode="after")
    def _check_order(self) -> ValuationDispersionThresholds:
        if not (0 < self.low_max < self.medium_max < self.auto_buy_block):
            raise ValueError(
                "valuation_dispersionはlow_max < medium_max < auto_buy_blockの順序が必要です"
            )
        return self


class BuyEarningsWindowConfig(StrictModel):
    block_buy_business_days: int
    add_margin_business_days: int
    added_margin: float

    @model_validator(mode="after")
    def _check_values(self) -> BuyEarningsWindowConfig:
        if self.block_buy_business_days >= self.add_margin_business_days:
            raise ValueError(
                "earnings_windowはblock_buy_business_days < add_margin_business_daysが必要です"
            )
        if not (0 <= self.added_margin < 1):
            raise ValueError("added_marginは0以上1未満である必要があります")
        return self


class MarginOfSafetyTier(StrictModel):
    entry: float
    standard: float
    strong: float

    @model_validator(mode="after")
    def _check_order_and_range(self) -> MarginOfSafetyTier:
        for value in (self.entry, self.standard, self.strong):
            if not (0 <= value < 1):
                raise ValueError("安全余裕率は0以上1未満である必要があります")
        if not (self.entry <= self.standard <= self.strong):
            raise ValueError("安全余裕率はentry <= standard <= strongの順序が必要です")
        return self


class MarginOfSafetyConfidenceTiers(StrictModel):
    high: MarginOfSafetyTier
    medium: MarginOfSafetyTier


class MarginAdjustments(StrictModel):
    earnings_within_3_business_days: float
    earnings_within_7_business_days: float
    high_valuation_dispersion: float
    very_high_valuation_dispersion: float
    industry_model_not_applied: float
    cyclical_industry: float
    small_cap_or_low_liquidity: float
    volatile_earnings: float
    temporary_earnings_boost_risk: float
    major_customer_dependency: float
    data_quality_warning: float


class MarginOfSafetyAdjustmentMultipliers(StrictModel):
    """カテゴリ集約後のリスク加算合計を、entry/standard/strongの各段階へ
    反映する際の感応度倍率(2026-07 BUYパイプライン第2次修正)。同額をそのまま
    3段階へ加算すると、上限到達時に3段階すべてが同一価格へ潰れるため、
    段階が進むほど強くリスクを反映させる。
    """

    entry: float
    standard: float
    strong: float

    @model_validator(mode="after")
    def _check_order(self) -> MarginOfSafetyAdjustmentMultipliers:
        for value in (self.entry, self.standard, self.strong):
            if value < 0:
                raise ValueError("adjustment_multipliersは0以上である必要があります")
        if not (self.entry <= self.standard <= self.strong):
            raise ValueError(
                "adjustment_multipliersはentry <= standard <= strongの順序が必要です"
            )
        return self


class MarginOfSafetyMaximumTiers(StrictModel):
    """段階別の安全余裕率上限(2026-07 BUYパイプライン第2次修正)。

    共通の上限(旧maximum)を全段階へ適用すると、リスク加算が大きい銘柄で
    3段階すべてが同一の上限値へ潰れてしまうため、段階ごとに異なる上限を
    設ける(entry <= standard <= strong)。
    """

    entry: float
    standard: float
    strong: float

    @model_validator(mode="after")
    def _check_order_and_range(self) -> MarginOfSafetyMaximumTiers:
        for value in (self.entry, self.standard, self.strong):
            if not (0 < value <= 0.45):
                raise ValueError("maximum_marginは0.45以下である必要があります")
        if not (self.entry <= self.standard <= self.strong):
            raise ValueError(
                "maximum_marginはentry <= standard <= strongの順序が必要です"
            )
        return self


class MarginOfSafetyConfig(StrictModel):
    confidence: MarginOfSafetyConfidenceTiers
    maximum_margin: MarginOfSafetyMaximumTiers
    minimum_margin_gap: float
    adjustment_multipliers: MarginOfSafetyAdjustmentMultipliers
    adjustments: MarginAdjustments

    @model_validator(mode="after")
    def _check_minimum_gap(self) -> MarginOfSafetyConfig:
        if not (0 <= self.minimum_margin_gap < 0.45):
            raise ValueError("minimum_margin_gapは0以上0.45未満である必要があります")
        return self


class UndervaluationCategoryCaps(StrictModel):
    valuation_multiple: float
    yield_: float = Field(alias="yield")
    fair_value: float
    market_price_action: float

    @model_validator(mode="after")
    def _check_sum(self) -> UndervaluationCategoryCaps:
        total = self.valuation_multiple + self.yield_ + self.fair_value + self.market_price_action
        if abs(total - 20.0) > 1e-9:
            raise ValueError("undervaluation_category_capsの合計は20点である必要があります")
        return self


class BuyDecisionRulesConfig(StrictModel):
    version: int
    score_thresholds: BuyScoreThresholds
    valuation_dispersion: ValuationDispersionThresholds
    earnings_window: BuyEarningsWindowConfig
    margin_of_safety: MarginOfSafetyConfig
    undervaluation_category_caps: UndervaluationCategoryCaps


# --- watchlist_screening_rules.yaml(ウォッチリスト自動追加機能) -------------


class CandidateUniverseConfig(StrictModel):
    provider: Literal["csv", "jpx"]
    csv_path: str
    # --- 候補ユニバース本格対応(2026-08)で追加。provider="jpx"の場合のみ使用する ---
    jpx_listed_issues_url: str | None = None
    jpx_400_weight_url: str | None = None
    target_market_segments: list[str] | None = None
    # v1では実質的に定数(45日/90日)だが、運用中に配信元の更新頻度が変わった場合に
    # コード変更なしで調整できるようconfig化する(8節)。
    listed_issues_max_stale_hours: int | None = None
    jpx400_max_stale_hours: int | None = None


class StagedRolloutConfig(StrictModel):
    """候補ユニバース本格対応(2026-08)の段階導入設定(15節)。

    3,122銘柄規模への全件移行前に、100→500→プライムのみ→全件の順で実測するための
    設定。candidate_limit/market_segment_filterのいずれもnull(未設定)であれば
    段階導入なし(=全件)として通常どおり動作する。
    """

    candidate_limit: int | None = Field(default=None, gt=0)
    market_segment_filter: list[str] | None = None


class WatchlistScreeningThresholds(StrictModel):
    minimum_market_cap_yen: int = Field(gt=0)
    require_positive_operating_cash_flow: bool
    exclude_dividend_cut_announced: bool
    exclude_debt_excess: bool
    exclude_deficit: bool
    exclude_going_concern_doubt: bool
    exclude_etf: bool
    exclude_reit: bool


class DividendYieldScoringConfig(StrictModel):
    weight: float = Field(gt=0)
    zero_at_pct: float
    full_at_pct: float


class EquityRatioScoringConfig(StrictModel):
    weight: float = Field(gt=0)
    zero_at_pct: float
    full_at_pct: float


class PayoutRatioScoringConfig(StrictModel):
    weight: float = Field(gt=0)
    healthy_min_pct: float
    healthy_max_pct: float


class DividendGrowthScoringConfig(StrictModel):
    weight: float = Field(gt=0)
    zero_at_years: int
    full_at_years: int


class ShareholderBenefitScoringConfig(StrictModel):
    weight: float = Field(gt=0)
    yield_full_at_pct: float
    presence_only_score_ratio: float = Field(ge=0, le=1)


class WatchlistScreeningScoringConfig(StrictModel):
    minimum_total_score: float
    dividend_yield: DividendYieldScoringConfig
    equity_ratio: EquityRatioScoringConfig
    payout_ratio: PayoutRatioScoringConfig
    dividend_growth: DividendGrowthScoringConfig
    shareholder_benefit: ShareholderBenefitScoringConfig

    @model_validator(mode="after")
    def _check_weights_sum_to_100(self) -> WatchlistScreeningScoringConfig:
        total = (
            self.dividend_yield.weight
            + self.equity_ratio.weight
            + self.payout_ratio.weight
            + self.dividend_growth.weight
            + self.shareholder_benefit.weight
        )
        if abs(total - 100.0) > 0.01:
            raise ValueError(f"配点合計は100点である必要があります(現在{total}点)")
        return self


class WatchlistScreeningRulesConfig(StrictModel):
    enabled: bool
    weekly_schedule_enabled: bool
    notification_enabled: bool
    candidate_universe: CandidateUniverseConfig
    screening_data_provider: Literal["stock_snapshot"]
    screening_policy: Literal["high_dividend_financial_health"]
    max_watchlist_additions_per_run: int = Field(gt=0)
    max_missing_fields: int = Field(ge=0)
    thresholds: WatchlistScreeningThresholds
    scoring: WatchlistScreeningScoringConfig

    # --- 候補ユニバース本格対応(2026-08、第6版修正プラン)で追加 -------------
    # 2節: 処理タイムアウト(業務判定)とDynamoDB TTL(物理削除)は独立した値。
    batch_processing_timeout_hours: int = Field(gt=0)
    batch_record_ttl_hours: int = Field(gt=0)
    candidate_progress_ttl_hours: int = Field(gt=0)
    # 17節: Reconcilerのタイムアウト確定処理1回あたりの処理件数上限。
    max_timeout_finalize_rows_per_run: int = Field(gt=0)
    # 14節: TIMED_OUT時に部分結果をウォッチリスト追加・通知に使うか(既定false)。
    allow_partial_result_on_timeout: bool
    # 10節: スロットリング率(429疑い件数/処理対象件数)がこれを超えるとABORTEDとする。
    high_throttle_rate_threshold_pct: float = Field(ge=0, le=100)
    # 15節: 段階導入(100→500→プライムのみ→全件)。
    staged_rollout: StagedRolloutConfig


# --- 集約 --------------------------------------------------------------------


class AddOnRulesConfig(StrictModel):
    """保有銘柄の買い増し固有リスクゲート(2026-07 統合BUY候補パイプライン)。

    既存のportfolio_concentration.single_stock_weight_threshold_pctは
    レビュー通知向けの警告閾値であり、買い増し禁止の判断には転用しない
    (意味が異なるため)。買い増し禁止に使う閾値はここで別途定義する。
    """

    version: int
    enabled: bool
    # 銘柄集中上限(比率0-1)。買い増し後の構成比がこれを超える場合、
    # add_on_eligibility=BLOCKED(POSITION_CONCENTRATION)とする。
    block_add_on_single_stock_ratio: float
    # 業種集中上限(比率0-1)。
    block_add_on_sector_ratio: float
    # 売却・利確判定と競合する場合に買い増し通知を禁止するか。
    block_on_sell_signal: bool
    # 保有データ(株数・平均取得単価)の整合性チェックを必須とするか。
    require_holding_data_consistency: bool
    # 単元未満株(端株)を保有データ不整合として買い増し禁止にするか
    # (既定False。単元未満株・株式分割等で正当に発生しうるため、
    # 即座にブロックはしない)。
    block_add_on_on_odd_lot: bool


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
    portfolio_concentration: PortfolioConcentrationRulesConfig
    buy_decision: BuyDecisionRulesConfig
    add_on: AddOnRulesConfig
    watchlist_screening: WatchlistScreeningRulesConfig
