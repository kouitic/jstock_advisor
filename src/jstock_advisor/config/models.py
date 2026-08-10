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


# --- review_improvement.yaml(振り返り機能改修: 週次改善レビュー) ------------------


class ReviewImprovementConfig(StrictModel):
    version: int
    evaluation_horizon_days: int
    weekly_review_enabled: bool
    # falseの間はGitHub API・Secrets Manager呼び出しを一切行わない(正常状態)。
    # trueへ切り替える際はconfig/review_improvement.yaml編集後の再デプロイが必要
    # (Lambda LayerでYAMLを配布する静的設定のため、実行中の動的反映は無い)。
    issue_creation_enabled: bool
    history_weeks_for_comparison: int
    github_issue_claim_timeout_minutes: int
    # RecommendationType(値)ごとの最低サンプル数。定義されない種別は"default"を使う。
    min_sample_count: dict[str, int]
    # RecommendationType(値)ごとの最低成功率(0〜100スケール)。業績系種別のみ持つ。
    min_success_rate_pct: dict[str, float]
    min_average_excess_return_pct: float
    success_rate_drop_threshold_points: float
    critical_success_rate_drop_threshold_points: float
    critical_average_excess_return_pct: float
    consecutive_bad_weeks_for_issue: int
    issue_labels: list[str]


# --- decision_evaluation.yaml(判定精度向上機能Phase A) -------------------------


class DecisionEvaluationConfig(StrictModel):
    # DecisionPerformanceServiceがDecision成績評価の対象とみなす営業日ホライズン。
    # 専用のEvaluationResultを新規生成するためではなく、既存
    # recommendation_evaluation_service.pyが既に生成しているEvaluationResultの
    # うち、この一覧に含まれるhorizon_business_daysの行だけを集計対象として
    # 絞り込むために使う(既存のevaluation_horizons_business_days自体には
    # 一切影響しない)。
    horizons_business_days: list[int]


# --- historical_valuation_rules.yaml(判定精度向上機能Phase B) ----------------


class HistoricalValuationCategoryThresholds(StrictModel):
    # スコア(-100〜+100)をHistoricalValuationCategoryへ丸めるための閾値。
    # Shadow記録専用(BUY/SELL等の判定ロジックには使わない)。
    very_cheap: float
    cheap: float
    expensive: float
    very_expensive: float

    @model_validator(mode="after")
    def _check_order(self) -> HistoricalValuationCategoryThresholds:
        if not (self.very_cheap > self.cheap > self.expensive > self.very_expensive):
            raise ValueError(
                "category_thresholdsはvery_cheap > cheap > expensive > "
                "very_expensiveの順である必要があります"
            )
        return self


class HistoricalValuationRulesConfig(StrictModel):
    # アルゴリズム自体(percentile定義・品質フィルタ・coverage/confidence計算式)
    # のバージョン。DECISION_SNAPSHOT_MODEL_VERSIONとは別物(こちらはHistorical
    # Valuation Score単体のバージョニング)。
    model_version: str
    # PER/PBRそれぞれについて、この点数以上の有効な過去データが無い場合は
    # その指標をスコア対象から除外する(yfinance実質年次数点程度という制約を
    # 踏まえ、少なすぎるデータでのランク付けを避ける)。
    min_data_points_required: int
    per_weight: float
    pbr_weight: float
    # 外れ値検出(MAD方式)を行うために必要な最低データ件数。これ未満の場合は
    # 外れ値判定自体が不安定なため行わない。
    outlier_detection_min_data_points: int
    outlier_mad_threshold: float
    # 明らかなデータ異常(取得元の不具合等)を機械的に除外するための絶対レンジ。
    per_absolute_min: float
    per_absolute_max: float
    pbr_absolute_min: float
    pbr_absolute_max: float
    # coverage計算の基準データ件数(この件数以上使えていれば当該コンポーネントの
    # データ充足度を1.0とみなす)。
    full_confidence_data_points: int
    coverage_high_threshold: float
    coverage_medium_threshold: float
    category_thresholds: HistoricalValuationCategoryThresholds

    @model_validator(mode="after")
    def _check_values(self) -> HistoricalValuationRulesConfig:
        if self.min_data_points_required < 2:
            raise ValueError("min_data_points_requiredは2以上である必要があります")
        if self.per_weight < 0 or self.pbr_weight < 0:
            raise ValueError("per_weight/pbr_weightは0以上である必要があります")
        if self.per_weight + self.pbr_weight <= 0:
            raise ValueError("per_weightとpbr_weightの合計は0より大きい必要があります")
        if self.outlier_detection_min_data_points < 2:
            raise ValueError("outlier_detection_min_data_pointsは2以上である必要があります")
        if self.outlier_mad_threshold <= 0:
            raise ValueError("outlier_mad_thresholdは正の値である必要があります")
        if self.per_absolute_min >= self.per_absolute_max:
            raise ValueError("per_absolute_minはper_absolute_max未満である必要があります")
        if self.pbr_absolute_min >= self.pbr_absolute_max:
            raise ValueError("pbr_absolute_minはpbr_absolute_max未満である必要があります")
        if self.full_confidence_data_points < 1:
            raise ValueError("full_confidence_data_pointsは1以上である必要があります")
        if self.coverage_medium_threshold >= self.coverage_high_threshold:
            raise ValueError(
                "coverage_medium_thresholdはcoverage_high_threshold未満である必要があります"
            )
        return self


# --- timing_score_rules.yaml(判定精度向上機能Phase B第二弾) -----------------


class TimingScoreCategoryThresholds(StrictModel):
    # スコア(-100〜+100)をTimingScoreCategoryへ丸めるための閾値。
    # Shadow記録専用(BUY/SELL等の判定ロジックには使わない)。
    strong_tailwind: float
    tailwind: float
    headwind: float
    strong_headwind: float

    @model_validator(mode="after")
    def _check_order(self) -> TimingScoreCategoryThresholds:
        if not (self.strong_tailwind > self.tailwind > self.headwind > self.strong_headwind):
            raise ValueError(
                "category_thresholdsはstrong_tailwind > tailwind > headwind > "
                "strong_headwindの順である必要があります"
            )
        if not (self.strong_headwind >= -100 and self.strong_tailwind <= 100):
            raise ValueError(
                "category_thresholdsはスコアの定義域[-100, 100]に収まる必要があります"
            )
        return self


class TimingScoreRulesConfig(StrictModel):
    """Timing Score v3(判定精度向上機能Phase B第二弾、コードレビュー対応で
    「モメンタムの強さ」から「エントリータイミングの質」へ再設計)の設定。

    trend_quality/price_vs_ma20/price_vs_ma60/rsi/macd/drawdown/volumeの
    7成分を加重平均してbase_scoreを算出する(overheat_penaltyはこの加重平均に
    含めない。コードレビュー対応v3: 過熱情報の欠損によってweightごと分母から
    消えスコアが底上げされる不整合を防ぐため、base_score算出後に適用する
    modifierとして分離した。score(final_score)=clamp(base_score -
    overheat_penalty_points)、過熱情報欠損時はpenalty=0=base_scoreのまま)。
    RSIはtrend_quality算出には一切使わない(TrendClassificationのSTRONG判定に
    RSIが使われているため、trend_quality自体はcurrent_price/ma20/ma60/
    ma20_slope_pctのみから独立算出し、RSIの二重評価を構造的に防ぐ)。
    """

    # アルゴリズム自体(成分の定義・区分境界・coverage/confidence計算式)の
    # バージョン。計算方式を変更した場合はここを更新する。
    model_version: str

    # base_score算出に使う7成分の加重平均の重み。利用不可な成分はcoverageの
    # 分母からも除外する(0点として加算しない)。
    trend_quality_weight: float
    price_vs_ma20_weight: float
    price_vs_ma60_weight: float
    rsi_weight: float
    macd_weight: float
    drawdown_weight: float
    volume_weight: float

    # trend_quality: ma20_slope_pctを-100〜+100へ換算する際のフルスケール
    # (この%でスコア±100に達する)。
    trend_slope_full_scale_pct: float

    # RSI: 過熱・エントリー適性のみを見る段階評価の区分境界(昇順)。
    # RSIが高いほど加点、という単調評価は採用しない(過熱を明確にペナルティ化する)。
    rsi_oversold_boundary: float
    rsi_neutral_boundary: float
    rsi_sweet_spot_boundary: float
    rsi_caution_boundary: float
    rsi_overheat_boundary: float

    # drawdown_from_recent_high_pct(0以下)の区分境界。適度な押し目区分の
    # 正のスコアは、trend_quality_componentが0以下の場合は0へキャップされる
    # (「高値から下がった」だけを理由に押し目扱いしない)。
    drawdown_near_high_pct: float
    drawdown_pullback_pct: float
    drawdown_neutral_pct: float

    # MA20/MA60からのsigned乖離(%)の区分境界(符号付き、昇順)。abs()による
    # 対称評価は採用しない(MAより上/下は意味が異なるため)。適正位置区分の
    # 正のスコアはtrend_quality_componentが0以下の場合は0へキャップされる。
    ma20_breakdown_pct: float
    ma20_pullback_low_pct: float
    ma20_near_high_pct: float
    ma20_overheat_pct: float
    ma60_breakdown_pct: float
    ma60_pullback_low_pct: float
    ma60_near_high_pct: float
    ma60_overheat_pct: float

    # volume_ratio(短期/長期平均出来高比)の区分境界。単純に出来高が多いほど
    # 加点する設計は採用しない。
    volume_low_threshold: float
    volume_moderate_low: float
    volume_moderate_high: float
    volume_extreme_threshold: float

    # 短期急騰・過熱の複合ペナルティ条件(5日リターン・RSI・drawdownの3条件が
    # すべて成立した場合のみ発火)。overheat_penalty_pointsはbase_scoreから
    # 差し引く点数(通常の加重平均成分ではなくmodifier、コードレビュー対応v3)。
    overheat_five_day_return_pct_threshold: float
    overheat_rsi_threshold: float
    overheat_drawdown_pct_threshold: float
    overheat_penalty_points: float

    # coverage(利用可能な成分の重み比率)がこの値未満の場合はNOT_EVALUATEDとする。
    min_coverage_required: float
    coverage_high_threshold: float
    coverage_medium_threshold: float
    category_thresholds: TimingScoreCategoryThresholds

    @model_validator(mode="after")
    def _check_values(self) -> TimingScoreRulesConfig:
        weights = (
            self.trend_quality_weight,
            self.price_vs_ma20_weight,
            self.price_vs_ma60_weight,
            self.rsi_weight,
            self.macd_weight,
            self.drawdown_weight,
            self.volume_weight,
        )
        if any(w < 0 for w in weights):
            raise ValueError("各成分の重みは0以上である必要があります")
        if sum(weights) <= 0:
            raise ValueError("各成分の重みの合計は0より大きい必要があります")
        if self.trend_slope_full_scale_pct <= 0:
            raise ValueError("trend_slope_full_scale_pctは正の値である必要があります")

        if not (
            0 <= self.rsi_oversold_boundary < self.rsi_neutral_boundary
            < self.rsi_sweet_spot_boundary < self.rsi_caution_boundary
            < self.rsi_overheat_boundary <= 100
        ):
            raise ValueError(
                "RSI区分境界は0 <= oversold < neutral < sweet_spot < caution "
                "< overheat <= 100の順である必要があります"
            )

        if self.drawdown_near_high_pct > 0:
            raise ValueError("drawdown_near_high_pctは0以下である必要があります")
        if not (
            self.drawdown_near_high_pct > self.drawdown_pullback_pct
            > self.drawdown_neutral_pct
        ):
            raise ValueError(
                "drawdown区分境界はnear_high > pullback > neutralの順(同値不可)"
                "である必要があります"
            )

        if not (
            self.ma20_breakdown_pct < self.ma20_pullback_low_pct
            < self.ma20_near_high_pct < self.ma20_overheat_pct
        ):
            raise ValueError(
                "ma20区分境界はbreakdown < pullback_low < near_high < overheatの"
                "順である必要があります"
            )
        if not (
            self.ma60_breakdown_pct < self.ma60_pullback_low_pct
            < self.ma60_near_high_pct < self.ma60_overheat_pct
        ):
            raise ValueError(
                "ma60区分境界はbreakdown < pullback_low < near_high < overheatの"
                "順である必要があります"
            )

        if not (
            0 <= self.volume_low_threshold < self.volume_moderate_low
            <= self.volume_moderate_high < self.volume_extreme_threshold
        ):
            raise ValueError(
                "volume区分境界は0 <= low < moderate_low <= moderate_high < extremeの"
                "順である必要があります"
            )

        if self.overheat_five_day_return_pct_threshold <= 0:
            raise ValueError(
                "overheat_five_day_return_pct_thresholdは正の値である必要があります"
            )
        if not (0 <= self.overheat_rsi_threshold <= 100):
            raise ValueError("overheat_rsi_thresholdは0〜100の範囲である必要があります")
        if self.overheat_drawdown_pct_threshold > 0:
            raise ValueError("overheat_drawdown_pct_thresholdは0以下である必要があります")
        if self.overheat_penalty_points <= 0:
            raise ValueError("overheat_penalty_pointsは正の値である必要があります")

        if not (0 < self.min_coverage_required <= 1):
            raise ValueError("min_coverage_requiredは0より大きく1以下である必要があります")
        if not (0 <= self.coverage_medium_threshold <= 1):
            raise ValueError("coverage_medium_thresholdは0〜1の範囲である必要があります")
        if not (0 <= self.coverage_high_threshold <= 1):
            raise ValueError("coverage_high_thresholdは0〜1の範囲である必要があります")
        if not (
            self.min_coverage_required <= self.coverage_medium_threshold
            < self.coverage_high_threshold <= 1
        ):
            raise ValueError(
                "min_coverage_required <= coverage_medium_threshold < "
                "coverage_high_threshold <= 1である必要があります"
                "(coverage∈[min_coverage_required, coverage_medium_threshold)は"
                "意図的にLOW confidence帯として許容する)"
            )
        return self


class EarningsSurpriseCategoryThresholds(StrictModel):
    # スコア(-100〜+100)をEarningsSurpriseCategoryへ丸めるための閾値。
    # Shadow記録専用(BUY/SELL等の判定ロジックには使わない)。
    strong_positive: float
    positive: float
    negative: float
    strong_negative: float

    @model_validator(mode="after")
    def _check_order(self) -> EarningsSurpriseCategoryThresholds:
        if not (self.strong_positive > self.positive > self.negative > self.strong_negative):
            raise ValueError(
                "category_thresholdsはstrong_positive > positive > negative > "
                "strong_negativeの順である必要があります"
            )
        if not (self.strong_negative >= -100 and self.strong_positive <= 100):
            raise ValueError(
                "category_thresholdsはスコアの定義域[-100, 100]に収まる必要があります"
            )
        return self


class EarningsSurpriseRulesConfig(StrictModel):
    """Earnings Surprise Score v2(判定精度向上機能Phase C)の設定。

    実装前調査の結果、Analyst Consensus Surprise(決算実績 vs 決算発表前
    アナリストコンセンサス予想)のみで構成する(Historical Progress
    Surprise・Guidance Revisionは現行データソースでは実装しない。
    Dividend Surprise/Revisionはコードレビュー対応でv2にて除外した
    (既存DividendComparisonOutcomeは「前年度実績 vs 現在予想」の比較で
    あり「今回決算のサプライズ」ではないため、意味の異なるデータを流用
    しない)。いずれもdomain/entities/earnings_surprise.py参照)。
    """

    model_version: str

    # 単一成分のみだが、他の判定精度向上機能スコアと同じ重み付きcoverage
    # 機構を踏襲する(coverageは実質0か1の二値になり、「analyst consensus
    # が無ければNOT_EVALUATED」という仕様と自然に一致する)。
    analyst_consensus_weight: float

    # Analyst Consensus成分: surprise_pct(実績EPSがコンセンサス予想を
    # 上回った/下回った割合、例0.05=5%上振れ)の段階評価区分(符号付き、昇順)。
    analyst_consensus_strong_negative_pct: float
    analyst_consensus_negative_pct: float
    analyst_consensus_positive_pct: float
    analyst_consensus_strong_positive_pct: float

    min_coverage_required: float
    coverage_high_threshold: float
    coverage_medium_threshold: float
    category_thresholds: EarningsSurpriseCategoryThresholds

    @model_validator(mode="after")
    def _check_values(self) -> EarningsSurpriseRulesConfig:
        if self.analyst_consensus_weight <= 0:
            raise ValueError("analyst_consensus_weightは正の値である必要があります")

        if not (
            self.analyst_consensus_strong_negative_pct
            < self.analyst_consensus_negative_pct
            < self.analyst_consensus_positive_pct
            < self.analyst_consensus_strong_positive_pct
        ):
            raise ValueError(
                "analyst_consensus区分境界はstrong_negative < negative < positive "
                "< strong_positiveの順(同値不可)である必要があります"
            )

        if not (0 < self.min_coverage_required <= 1):
            raise ValueError("min_coverage_requiredは0より大きく1以下である必要があります")
        if not (0 <= self.coverage_medium_threshold <= 1):
            raise ValueError("coverage_medium_thresholdは0〜1の範囲である必要があります")
        if not (0 <= self.coverage_high_threshold <= 1):
            raise ValueError("coverage_high_thresholdは0〜1の範囲である必要があります")
        if not (
            self.min_coverage_required <= self.coverage_medium_threshold
            < self.coverage_high_threshold <= 1
        ):
            raise ValueError(
                "min_coverage_required <= coverage_medium_threshold < "
                "coverage_high_threshold <= 1である必要があります"
            )
        return self


class EarningsTrendCategoryThresholds(StrictModel):
    # スコア(-100〜+100)をEarningsTrendCategoryへ丸めるための閾値。
    # Shadow記録専用(BUY/SELL等の判定ロジックには使わない)。
    strong_improving: float
    improving: float
    deteriorating: float
    strong_deteriorating: float

    @model_validator(mode="after")
    def _check_order(self) -> EarningsTrendCategoryThresholds:
        if not (
            self.strong_improving > self.improving
            > self.deteriorating > self.strong_deteriorating
        ):
            raise ValueError(
                "category_thresholdsはstrong_improving > improving > deteriorating > "
                "strong_deterioratingの順である必要があります"
            )
        if not (self.strong_deteriorating >= -100 and self.strong_improving <= 100):
            raise ValueError(
                "category_thresholdsはスコアの定義域[-100, 100]に収まる必要があります"
            )
        return self


class EarningsTrendRulesConfig(StrictModel):
    """Earnings Trend Score v2(判定精度向上機能Phase C)の設定。

    実装前調査の結果、営業利益トレンド・営業CFトレンド・配当方向の3成分
    (+補助的なacceleration成分)で構成する(売上トレンド・EPSトレンド・
    利益率改善・会社予想方向は現行Providerでは算出できないため対象外。
    domain/entities/earnings_trend.py参照)。コードレビュー対応(v2):
    変化率計算の符号跨ぎ(赤字・マイナスCF時の改善/悪化逆転)バグを修正、
    FinancialSummary.recent_periods_source(四半期実績由来か年次フォール
    バック由来か)をconfidenceへ反映するようにした(この設定ファイル自体に
    新規フィールドは追加していない、domain/signals/earnings_trend.py参照)。
    """

    model_version: str

    operating_income_trend_weight: float
    operating_cashflow_trend_weight: float
    dividend_direction_weight: float
    # データが薄い(直近5四半期程度)ため信頼度が低い補助成分。他成分より
    # 小さい重みを設定すること(validatorでは強制しない、運用判断)。
    acceleration_weight: float

    # 営業利益/営業CFトレンド共通: 直近期の前期比変化率(%)の段階評価区分
    # (符号付き、昇順)。
    trend_strong_decline_pct: float
    trend_decline_pct: float
    trend_improve_pct: float
    trend_strong_improve_pct: float

    # acceleration成分: 前期比変化率の変化(2階差分、%ポイント)をこの値で
    # ±100へ換算する(この%ポイントでスコア±100に達する)。
    acceleration_full_scale_pct: float

    # Dividend Direction成分: Earnings Surprise Scoreと同じ
    # DividendComparisonOutcome区分に対応する固定点数(意味が異なるため
    # 独立して設定できるようにする)。
    dividend_actual_cut_score: float
    dividend_forecast_cut_score: float
    dividend_maintained_score: float
    dividend_increase_score: float

    min_coverage_required: float
    coverage_high_threshold: float
    coverage_medium_threshold: float
    category_thresholds: EarningsTrendCategoryThresholds

    @model_validator(mode="after")
    def _check_values(self) -> EarningsTrendRulesConfig:
        weights = (
            self.operating_income_trend_weight,
            self.operating_cashflow_trend_weight,
            self.dividend_direction_weight,
            self.acceleration_weight,
        )
        if any(w < 0 for w in weights):
            raise ValueError("各成分の重みは0以上である必要があります")
        if sum(weights) <= 0:
            raise ValueError("各成分の重みの合計は0より大きい必要があります")

        if not (
            self.trend_strong_decline_pct < self.trend_decline_pct
            < self.trend_improve_pct < self.trend_strong_improve_pct
        ):
            raise ValueError(
                "trend区分境界はstrong_decline < decline < improve < strong_improveの"
                "順(同値不可)である必要があります"
            )
        if self.acceleration_full_scale_pct <= 0:
            raise ValueError("acceleration_full_scale_pctは正の値である必要があります")

        dividend_scores = (
            self.dividend_actual_cut_score,
            self.dividend_forecast_cut_score,
            self.dividend_maintained_score,
            self.dividend_increase_score,
        )
        if any(not (-100 <= s <= 100) for s in dividend_scores):
            raise ValueError("dividend区分の各スコアは-100〜100の範囲である必要があります")
        if not (
            self.dividend_actual_cut_score
            < self.dividend_forecast_cut_score
            < self.dividend_maintained_score
            < self.dividend_increase_score
        ):
            raise ValueError(
                "dividend区分スコアはactual_cut < forecast_cut < maintained < increase"
                "の順(同値不可)である必要があります"
            )

        if not (0 < self.min_coverage_required <= 1):
            raise ValueError("min_coverage_requiredは0より大きく1以下である必要があります")
        if not (0 <= self.coverage_medium_threshold <= 1):
            raise ValueError("coverage_medium_thresholdは0〜1の範囲である必要があります")
        if not (0 <= self.coverage_high_threshold <= 1):
            raise ValueError("coverage_high_thresholdは0〜1の範囲である必要があります")
        if not (
            self.min_coverage_required <= self.coverage_medium_threshold
            < self.coverage_high_threshold <= 1
        ):
            raise ValueError(
                "min_coverage_required <= coverage_medium_threshold < "
                "coverage_high_threshold <= 1である必要があります"
            )
        return self


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
    # --- 決算待ち判定ロジック修正(コードレビュー対応)で追加 ---
    # 決算予定日から想定される決算期末日までの報告ラグ日数。fiscal_period_endが
    # この日数以内に更新されていれば、決算発表が財務データへ反映されたとみなす
    # (実際の決算発表日そのものの厳密な突合ではなく近似判定)。
    fiscal_period_reporting_lag_days: int
    # 決算予定日経過後、財務データの更新確認をこの時間(時間単位)待っても
    # 確認できない場合はDELAYEDへ遷移させる。
    maximum_data_reflection_wait_hours: int
    # --- デプロイ前対応で追加 ---
    # 過去の決算予定日からこの日数以内であれば、財務データが未更新でも現在の
    # 判断に関連するとみなす(RELEVANT)。これを超えて経過してもなお財務データの
    # 更新が確認できない場合はUNKNOWNとし、通常判定を無期限に停止しない。
    stale_earnings_relevance_days: int


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


class StockDisplayNameConfig(StrictModel):
    """銘柄表示名解決(通知品質改善、2026-08)向けの運用設定。

    既定値(60)の責務はこのモデルのみが持つ(StockDisplayNameResolver/
    JpxStockNameSource側にデフォルト値は重複定義しない)。
    """

    # JPX銘柄名キャッシュの取得・パースに失敗した際、直後の連続リトライを防ぐ
    # negative cacheの有効期間(秒)。1以上の整数。
    jpx_name_negative_cache_ttl_seconds: int = Field(default=60, ge=1)


class WatchlistDataCacheConfig(StrictModel):
    """ウォッチリスト専用の株価/財務/配当データキャッシュTTL(運用ハードニング4節)。

    共有yfinance Provider実装は変更せず、ウォッチリストLambdaハンドラ内でのみ
    ProviderBundleをラップして適用する(services/watchlist_data_cache.py)。
    """

    price_cache_ttl_hours: int = Field(gt=0)
    financial_cache_ttl_hours: int = Field(gt=0)
    # 運用ハードニング第2弾1節: None・DEGRADEDな値の短期キャッシュTTL(分単位)。
    # 5〜30分程度を想定。VALIDな値にはこのTTLは使わない(price/financial_cache_ttl_hoursを使う)。
    negative_cache_ttl_minutes: int = Field(gt=0)


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

    # --- 運用ハードニング(2026-08、本番運用レビュー対応)で追加 ---------------
    # 主要スコア項目の欠損率がこれを超えるとABORTEDとする(429疑い率とは独立、3節)。
    max_scoring_field_missing_rate_pct: float = Field(ge=0, le=100)
    # finalizeがFINALIZINGのまま応答が無い場合の異常判定しきい値(分、5節)。
    finalizing_stuck_threshold_minutes: int = Field(gt=0)
    # FINALIZE_FAILEDに対するReconcilerの自動再試行上限回数(5節)。
    max_finalize_retry_attempts: int = Field(gt=0)
    data_cache: WatchlistDataCacheConfig

    # --- 運用ハードニング第2弾(2026-08、レビュー対応)で追加 -----------------
    # provider_failure_classifierが分類できない未知の障害パターンでも安全に
    # 中止できるようにする独立の安全弁(5節)。母数の定義はcompute_batch_metrics参照。
    max_data_error_rate_pct: float = Field(ge=0, le=100)
    max_not_found_rate_pct: float = Field(ge=0, le=100)
    max_terminal_failure_rate_pct: float = Field(ge=0, le=100)
    max_required_field_missing_rate_pct: float = Field(ge=0, le=100)

    # --- 運用ハードニング第3弾(2026-08、レビュー対応)で追加 -----------------
    # NOTIFICATION_FAILEDに対するReconciler/CLIの自動再試行上限回数(1節)。
    max_notification_retry_attempts: int = Field(gt=0)

    # --- LINE通知品質改善(2026-08)で追加 --------------------------------------
    stock_display_name: StockDisplayNameConfig = Field(default_factory=StockDisplayNameConfig)


# --- holding_decision_rules.yaml ---------------------------------------------
# 保有判断スコア方式(2026-08仕様)。企業品質(0-50)+投資ストーリー維持(0-50)
# -リスク控除(0-100)を統合した単一スコアで保有銘柄の売却判定を行う。


class CompanyQualityWeights(StrictModel):
    """企業品質スコア(0-50点)の評価項目別配点。合計は50点。"""

    financial_health_equity_ratio: float
    financial_health_debt_excess: float
    cash_generation_cf_income_ratio: float
    cash_generation_cf_streak: float
    profitability_roe: float
    profitability_eps_stability: float
    stability_operating_income: float
    stability_deficit: float
    governance_going_concern: float
    governance_listing_risk: float

    @model_validator(mode="after")
    def _check_sum(self) -> CompanyQualityWeights:
        total = (
            self.financial_health_equity_ratio
            + self.financial_health_debt_excess
            + self.cash_generation_cf_income_ratio
            + self.cash_generation_cf_streak
            + self.profitability_roe
            + self.profitability_eps_stability
            + self.stability_operating_income
            + self.stability_deficit
            + self.governance_going_concern
            + self.governance_listing_risk
        )
        if abs(total - 50.0) > 0.01:
            raise ValueError(f"企業品質スコアの配点合計は50点である必要があります(現在{total}点)")
        return self


class InvestmentThesisWeights(StrictModel):
    """投資ストーリー維持スコア(0-50点)の評価項目別配点。合計は50点。"""

    dividend_policy: float
    total_yield: float
    benefit_condition: float
    profit_cf_premise: float
    financial_premise: float
    custom_conditions: float

    @model_validator(mode="after")
    def _check_sum(self) -> InvestmentThesisWeights:
        total = (
            self.dividend_policy
            + self.total_yield
            + self.benefit_condition
            + self.profit_cf_premise
            + self.financial_premise
            + self.custom_conditions
        )
        if abs(total - 50.0) > 0.01:
            raise ValueError(
                f"投資ストーリー維持スコアの配点合計は50点である必要があります(現在{total}点)"
            )
        return self


class JudgmentCategoryThresholds(StrictModel):
    """HoldingDecisionCategoryの境界値(final_holding_decision_scoreで判定)。"""

    strong_hold_min: float
    hold_min: float
    caution_min: float
    partial_sell_consideration_min: float
    sell_watch_min: float
    sell_consideration_min: float

    @model_validator(mode="after")
    def _check_order(self) -> JudgmentCategoryThresholds:
        if not (
            self.strong_hold_min
            > self.hold_min
            > self.caution_min
            > self.partial_sell_consideration_min
            > self.sell_watch_min
            > self.sell_consideration_min
        ):
            raise ValueError(
                "JudgmentCategoryThresholds: "
                "strong_hold_min > hold_min > caution_min > partial_sell_consideration_min "
                "> sell_watch_min > sell_consideration_min である必要があります"
            )
        return self


class HardGateConfig(StrictModel):
    """ハードゲート発動時のfinal_scoreへの上限補正(7節)。"""

    score_cap: float


class CoverageThresholds(StrictModel):
    """component別coverage_ratioの通知閾値(5節・8節)。"""

    overall_minimum: float
    company_quality_minimum: float
    investment_thesis_minimum: float
    risk_deduction_block_minimum: float
    risk_deduction_confidence_minimum: float

    @model_validator(mode="after")
    def _check_range_and_order(self) -> CoverageThresholds:
        for name, value in (
            ("overall_minimum", self.overall_minimum),
            ("company_quality_minimum", self.company_quality_minimum),
            ("investment_thesis_minimum", self.investment_thesis_minimum),
            ("risk_deduction_block_minimum", self.risk_deduction_block_minimum),
            ("risk_deduction_confidence_minimum", self.risk_deduction_confidence_minimum),
        ):
            if not (0.0 < value <= 1.0):
                raise ValueError(f"CoverageThresholds.{name}は0より大きく1以下である必要があります")
        if self.risk_deduction_block_minimum > self.risk_deduction_confidence_minimum:
            raise ValueError(
                "risk_deduction_block_minimumはrisk_deduction_confidence_minimum以下である必要があります"
            )
        return self


class ConfidenceThresholds(StrictModel):
    """overall_coverageからHoldingDecisionConfidenceLevelを決める閾値(8節)。"""

    high_minimum: float
    medium_minimum: float
    low_minimum: float

    @model_validator(mode="after")
    def _check_order(self) -> ConfidenceThresholds:
        if not (self.high_minimum > self.medium_minimum > self.low_minimum > 0.0):
            raise ValueError(
                "ConfidenceThresholds: high_minimum > medium_minimum > low_minimum > 0 "
                "である必要があります"
            )
        return self


class LinearScoreThreshold(StrictModel):
    """`_linear_score()`(domain/scoring/score.py)の0点/満点閾値1組。

    zero_at > full_atの場合は「値が小さいほど高得点」という向きになる
    (例: 変動係数は低いほど安定=高得点)。
    """

    zero_at: float
    full_at: float


class CompanyQualityScoreThresholds(StrictModel):
    """企業品質スコアの各採点式が使う0点/満点閾値。"""

    equity_ratio_pct: LinearScoreThreshold
    cf_income_ratio: LinearScoreThreshold
    cf_streak_quarters: LinearScoreThreshold
    roe: LinearScoreThreshold
    cv_based_stability: LinearScoreThreshold
    profit_quarter_ratio: LinearScoreThreshold


class RenotificationConfig(StrictModel):
    """再通知抑止条件(14節)。"""

    renotify_score_deterioration: float
    renotify_on_decision_change: bool
    renotify_on_new_hard_gate: bool
    renotify_after_earnings: bool
    renotify_on_sell_price_change_pct: float


class HoldingDecisionRulesConfig(StrictModel):
    version: int
    scoring_model_version: int
    company_quality_weights: CompanyQualityWeights
    company_quality_score_thresholds: CompanyQualityScoreThresholds
    investment_thesis_weights: InvestmentThesisWeights
    judgment_category_thresholds: JudgmentCategoryThresholds
    notify_below_score: float
    hard_gate: HardGateConfig
    coverage_thresholds: CoverageThresholds
    confidence_thresholds: ConfidenceThresholds
    renotification: RenotificationConfig
    # CustomThesisConditionの鮮度2段階(3節)。
    fresh_within_days: int
    stale_after_days: int
    # Baseline活性化(activate())の最大リトライ回数(2節)。
    baseline_activation_max_retries: int
    # 主な加点・減点要因(15節)の保存件数上限。
    top_positive_reasons_count: int
    top_negative_reasons_count: int
    # ランタイムConfigのキャッシュTTL(1節)。
    runtime_config_cache_ttl_seconds: int
    # 投資ストーリー維持スコアがこの値未満、かつHUMAN_APPROVEDのInvestmentThesisが
    # 存在する場合のみ、ハードゲート「中核となる投資ストーリーの完全消失」を機械判定する(7節)。
    investment_thesis_collapse_threshold: float

    @model_validator(mode="after")
    def _check_notify_below_score_matches_boundary(self) -> HoldingDecisionRulesConfig:
        if abs(self.notify_below_score - self.judgment_category_thresholds.sell_watch_min) > 0.001:
            raise ValueError(
                "notify_below_scoreはjudgment_category_thresholds.sell_watch_minと"
                "一致している必要があります(判定区分境界との矛盾防止)"
            )
        return self

    @model_validator(mode="after")
    def _check_hard_gate_cap_below_notify_threshold(self) -> HoldingDecisionRulesConfig:
        if self.hard_gate.score_cap >= self.notify_below_score:
            raise ValueError(
                "hard_gate.score_capはnotify_below_score未満である必要があります"
                "(ハードゲート発動時に必ず通知条件を満たすことを保証するため)"
            )
        return self

    @model_validator(mode="after")
    def _check_fresh_stale_order(self) -> HoldingDecisionRulesConfig:
        if self.fresh_within_days >= self.stale_after_days:
            raise ValueError("fresh_within_daysはstale_after_days未満である必要があります")
        return self

    @model_validator(mode="after")
    def _check_positive_counts(self) -> HoldingDecisionRulesConfig:
        if self.baseline_activation_max_retries < 1:
            raise ValueError("baseline_activation_max_retriesは1以上である必要があります")
        if self.top_positive_reasons_count < 1 or self.top_negative_reasons_count < 1:
            raise ValueError(
                "top_positive_reasons_count/top_negative_reasons_countは1以上である必要があります"
            )
        return self


# --- holding_decision_risk_rules.yaml -----------------------------------------


class RiskCategoryCaps(StrictModel):
    """リスク控除カテゴリ別上限(4節)。合計は100点。"""

    business_cashflow_deterioration: float
    shareholder_return_deterioration: float
    financial_crisis: float
    governance_and_listing_risk: float
    structural_change: float

    @model_validator(mode="after")
    def _check_sum(self) -> RiskCategoryCaps:
        total = (
            self.business_cashflow_deterioration
            + self.shareholder_return_deterioration
            + self.financial_crisis
            + self.governance_and_listing_risk
            + self.structural_change
        )
        if abs(total - 100.0) > 0.01:
            raise ValueError(
                f"リスク控除カテゴリ上限の合計は100点である必要があります(現在{total}点)"
            )
        return self


class RiskFactorTable(StrictModel):
    """severity/persistence/confidence係数(9節: risk_points = base_points × 各係数)。"""

    persistence_single_occurrence: float
    persistence_two_periods: float
    persistence_structural: float
    confidence_primary_source_confirmed: float
    confidence_secondary_source_only: float

    @model_validator(mode="after")
    def _check_non_negative(self) -> RiskFactorTable:
        for name, value in (
            ("persistence_single_occurrence", self.persistence_single_occurrence),
            ("persistence_two_periods", self.persistence_two_periods),
            ("persistence_structural", self.persistence_structural),
            ("confidence_primary_source_confirmed", self.confidence_primary_source_confirmed),
            ("confidence_secondary_source_only", self.confidence_secondary_source_only),
        ):
            if value < 0:
                raise ValueError(f"RiskFactorTable.{name}は負値にできません")
        return self


class RiskSignal(StrictModel):
    """リスク控除の個別シグナル1件の定義。

    hard_gate_excluded=Trueのシグナルは、7節のハードゲートに該当するイベントであり、
    リスク控除の対象から除外する(4節: ハードゲートとの三重評価防止)。
    """

    base_points: float
    category: Literal[
        "business_cashflow_deterioration",
        "shareholder_return_deterioration",
        "financial_crisis",
        "governance_and_listing_risk",
        "structural_change",
    ]
    hard_gate_excluded: bool = False


class HoldingDecisionRiskRulesConfig(StrictModel):
    version: int
    category_caps: RiskCategoryCaps
    factors: RiskFactorTable
    signals: dict[str, RiskSignal]

    @model_validator(mode="after")
    def _check_signal_categories_have_caps(self) -> HoldingDecisionRiskRulesConfig:
        valid_categories = {
            "business_cashflow_deterioration",
            "shareholder_return_deterioration",
            "financial_crisis",
            "governance_and_listing_risk",
            "structural_change",
        }
        for name, signal in self.signals.items():
            if signal.category not in valid_categories:
                raise ValueError(f"signals.{name}.categoryが不正です: {signal.category}")
        return self


# --- holding_decision_ratio_rules.yaml -----------------------------------------


class RatioClampRange(StrictModel):
    ratio_clamp_min: float
    ratio_clamp_max: float
    roe_clamp_min: float
    roe_clamp_max: float

    @model_validator(mode="after")
    def _check_order(self) -> RatioClampRange:
        if self.ratio_clamp_min >= self.ratio_clamp_max:
            raise ValueError("ratio_clamp_minはratio_clamp_max未満である必要があります")
        if self.roe_clamp_min >= self.roe_clamp_max:
            raise ValueError("roe_clamp_minはroe_clamp_max未満である必要があります")
        return self


class HoldingDecisionRatioRulesConfig(StrictModel):
    version: int
    clamp: RatioClampRange
    min_operating_income_absolute_yen: float
    min_mean_for_cv_yen: float
    outlier_clip_zscore: float
    min_periods_for_stability_score: int

    @model_validator(mode="after")
    def _check_positive(self) -> HoldingDecisionRatioRulesConfig:
        for name, value in (
            ("min_operating_income_absolute_yen", self.min_operating_income_absolute_yen),
            ("min_mean_for_cv_yen", self.min_mean_for_cv_yen),
            ("outlier_clip_zscore", self.outlier_clip_zscore),
        ):
            if value <= 0:
                raise ValueError(
                    f"HoldingDecisionRatioRulesConfig.{name}は正値である必要があります"
                )
        if self.min_periods_for_stability_score < 2:
            raise ValueError("min_periods_for_stability_scoreは2以上である必要があります")
        return self


# --- investment_thesis_template.yaml -------------------------------------------


class InvestmentThesisTemplateConfig(StrictModel):
    """個別購入理由が未登録の銘柄向けの共通投資ストーリーテンプレート(3節)。

    個別購入理由軸(5点)の代用には使わない。標準5軸のみに適用する。
    """

    version: int
    min_total_yield_pct: float
    dividend_cut_tolerance: Literal["none", "minor_only"]
    require_positive_operating_cashflow_trend: bool


# --- industry_scoring_policy.yaml -----------------------------------------------


class FinancialCategoryPolicy(StrictModel):
    deferred: bool


class FinancialIndustryPolicy(StrictModel):
    """金融業(銀行/保険/証券/その他金融)のActive移行状況の正の設定元(3節)。

    RuntimeConfig.financial_policy_overrideはこのYAMLを緊急退避方向にのみ
    上書きできる(FORCE_DEFER_ALL)。Active化を強制する経路は存在しない。
    """

    financial_model_version: int | None = None
    supported_financial_categories: list[
        Literal["BANKING", "INSURANCE", "SECURITIES", "OTHER_FINANCIAL"]
    ] = []
    categories: dict[
        Literal["BANKING", "INSURANCE", "SECURITIES", "OTHER_FINANCIAL"],
        FinancialCategoryPolicy,
    ]

    @model_validator(mode="after")
    def _check_all_categories_defined(self) -> FinancialIndustryPolicy:
        required = {"BANKING", "INSURANCE", "SECURITIES", "OTHER_FINANCIAL"}
        missing = required - set(self.categories.keys())
        if missing:
            raise ValueError(
                f"industry_scoring_policy.categoriesに未定義の業種があります: {missing}"
            )
        return self

    @model_validator(mode="after")
    def _check_supported_categories_consistency(self) -> FinancialIndustryPolicy:
        for category in self.supported_financial_categories:
            policy = self.categories.get(category)
            if policy is None or policy.deferred:
                raise ValueError(
                    f"supported_financial_categoriesに含まれる{category}は"
                    "categories側でdeferred:falseである必要があります"
                )
            if self.financial_model_version is None:
                raise ValueError(
                    "supported_financial_categoriesが1件以上ある場合、"
                    "financial_model_versionは非nullである必要があります"
                )
        return self


class IndustryScoringPolicyConfig(StrictModel):
    version: int
    financial_industry_policy: FinancialIndustryPolicy


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
    # --- 保有判断スコア方式(2026-08仕様)で追加 ---
    holding_decision: HoldingDecisionRulesConfig
    holding_decision_risk: HoldingDecisionRiskRulesConfig
    holding_decision_ratio: HoldingDecisionRatioRulesConfig
    investment_thesis_template: InvestmentThesisTemplateConfig
    industry_scoring_policy: IndustryScoringPolicyConfig
    # --- 振り返り機能改修(2026-08)で追加 ---
    review_improvement: ReviewImprovementConfig
    # --- 判定精度向上機能Phase A(2026-08)で追加 ---
    decision_evaluation: DecisionEvaluationConfig
    # --- 判定精度向上機能Phase B: Historical Valuation Score(2026-08)で追加 ---
    historical_valuation: HistoricalValuationRulesConfig
    # --- 判定精度向上機能Phase B第二弾: Timing Score(2026-08)で追加 ---
    timing_score: TimingScoreRulesConfig
    # --- 判定精度向上機能Phase C: Earnings Surprise/Trend Score(2026-08)で追加 ---
    earnings_surprise: EarningsSurpriseRulesConfig
    earnings_trend: EarningsTrendRulesConfig
