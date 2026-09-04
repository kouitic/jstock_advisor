"""設定ファイル(config/*.yaml, config/*.json)に対応するpydanticモデル群。

すべてのモデルは `extra="forbid"` とし、YAML側のタイプミスや未知キーを
起動時に検出できるようにする。数値の閾値は要求仕様のデフォルト値をそのまま
Pythonのデフォルトとしては持たせず、YAMLの値のみを正とする(設定ファイル必須)。
"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jstock_advisor.domain.entities.enums import (
    FinancialIndustryCategory,
    ShareholderReturnPolicyType,
)


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
    # Issue #29(2026-08-28): 旧形式はTSE33業種の日本語ラベル(例: "銀行業")の
    # リストだったが、判定対象のindustry値はyfinance由来の英語GICS値
    # (例: "Banks - Diversified")のため一度も一致せず、金融業除外が機能して
    # いなかった。classify_industry()(domain/classification/financial_industry.py)
    # の金融細分類enum値で指定する形式へ変更。不正な値はconfigロード時に
    # pydanticのenum検証でfail-fastする。
    target_industry_classification: list[FinancialIndustryCategory]


class DataQualityRules(StrictModel):
    max_data_age_business_days: int
    # Issue #52 Phase B3: 決算期末から報告期限までの猶予「暦日」数。
    # max_data_age_business_daysが「いつ取得したか」の鮮度であるのに対し、
    # こちらは「財務データの対象期間が報告サイクル上最新か」を判定する。
    # 負値は意味を持たないため設定段階で弾く(判定時のUNKNOWNへ流さない)。
    financial_reporting_lag_calendar_days: int = Field(ge=0)


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
    # --- コードレビュー対応(2026-08、上値余地の導入): 以下2フィールドは判定
    # レベル(raw_level)の決定にはもう使わない。_compute_sell_prices()の価格
    # フィールド候補選択(level_gain)専用として残す(PricePositionThresholds
    # が判定レベルの主軸を担う) ---
    unrealized_gain_partial_pct: float
    unrealized_gain_full_pct: float
    # --- 利確判定レビュー再対応(2026-07): 中立値ではなく強気適正価格を主軸にする。
    # コードレビュー対応(2026-08)により、以下2フィールドも判定レベルの決定には
    # 使わず、_compute_sell_prices()の価格フィールド候補選択(level_fv)専用 ---
    # 強気適正価格をこの%以上超過した場合にPARTIAL候補水準とする
    fair_value_excess_partial_pct: float
    # 強気適正価格をこの%以上超過した場合にFULL候補水準とする
    fair_value_excess_full_pct: float
    total_yield_caution_pct: float
    total_yield_strong_caution_pct: float


class PricePositionThresholds(StrictModel):
    """含み益率×上値余地(ceilingまでの距離)の基本アクションレベル判定
    (コードレビュー対応2026-08)。詳細はprofit_taking.py._level_from_price_position()参照。
    """

    watch_gain_pct: float
    partial_gain_pct: float
    full_gain_pct: float
    partial_upside_max_pct: float
    full_upside_max_pct: float
    ceiling_exceeded_pct: float


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

    # Issue #55 Phase B-2(F-G2 / N6・N11)。`MitigatingFactor` は6要因で共有される
    # ため、パラメータ2種(min_consecutive_years / within_business_days)は
    # Optional として定義せざるを得ない。しかしそれを使う要因が `enabled: true` の
    # ときは**必須**であり、未設定は設定ミスである。
    #
    # 従来は YAML から該当行を1行消すと、警告もログも無く当該緩和要因だけが
    # 黙って無効化されていた(`min_years > 0` / `within_days is None` の
    # ガードで False へ落ちるため)。設定値の欠落を「無効化の意思表示」と
    # 解釈せず、起動時に fail-fast させる。
    #
    # 値 0 も不正とする: `profit_taking.py` の連続増配要因は成立時に
    # 「実績で{n}年連続増配している」という文言を生成するため、0 では
    # 事実に反する文言になる(`min_years > 0` ガードが load-bearing な理由)。
    @model_validator(mode="after")
    def _validate_required_parameters(self) -> MitigatingFactors:
        required_parameters = (
            ("continuous_dividend_increase", "min_consecutive_years"),
            ("long_term_holding_benefit_imminent", "within_business_days"),
        )
        for factor_name, parameter_name in required_parameters:
            factor: MitigatingFactor = getattr(self, factor_name)
            if not factor.enabled:
                continue
            value = getattr(factor, parameter_name)
            if value is None:
                raise ValueError(
                    f"profit_taking.mitigating_factors.{factor_name}.{parameter_name} は "
                    f"enabled: true のとき必須です(未設定だと当該緩和要因が"
                    f"黙って無効化されます)。値を設定するか enabled: false にしてください。"
                )
            if value <= 0:
                raise ValueError(
                    f"profit_taking.mitigating_factors.{factor_name}.{parameter_name} は "
                    f"1以上である必要があります(指定値: {value})。"
                )
        return self


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


class PartialSellRatios(StrictModel):
    """PARTIAL_PROFIT_TAKE成立後の売却強度(SellIntensity)別の目安売却比率
    (コードレビュー対応2026-08、指摘Part B)。保有株数に対する比率であり、
    実際の売却株数は売買単位に切り下げて算出する(trading_unit_feasibility.py
    のcompute_suggested_sell_shares()参照)。
    """

    light: float
    standard: float
    strong: float
    very_strong: float

    @model_validator(mode="after")
    def _check_ordering(self) -> PartialSellRatios:
        if not (0 < self.light <= self.standard <= self.strong <= self.very_strong < 1):
            raise ValueError(
                "partial_sell_ratiosは0 < light <= standard <= strong <= "
                "very_strong < 1を満たす必要があります"
            )
        return self


def _check_profit_protection_threshold_ranges(
    label: str,
    min_current_gain_pct: float,
    min_drawdown_from_peak_pct: float,
    min_gain_giveback_ratio_pct: float,
) -> None:
    """Profit Protection閾値の単体としての妥当性(コードレビュー対応2026-08、
    指摘3)。min_current_gain_pctは株価が取得価格の2倍・3倍になることも
    あるため上限を設けない。drawdown/givebackは比率(%)のため0〜100が
    自然な範囲とする。
    """
    if min_current_gain_pct < 0:
        raise ValueError(
            f"profit_protection.{label}.min_current_gain_pctは0以上である必要があります"
        )
    if not (0 <= min_drawdown_from_peak_pct <= 100):
        raise ValueError(
            f"profit_protection.{label}.min_drawdown_from_peak_pctは0〜100の範囲である必要があります"
        )
    if not (0 <= min_gain_giveback_ratio_pct <= 100):
        raise ValueError(
            f"profit_protection.{label}.min_gain_giveback_ratio_pctは0〜100の範囲である必要があります"
        )


class ProfitProtectionCandidateThresholds(StrictModel):
    """通常のProfit Protection候補(§3A)の閾値。単独では無条件にPARTIALとせず、
    condition_based_judgment.min_conditions_for_partialの一条件として数える。
    """

    min_current_gain_pct: float
    min_drawdown_from_peak_pct: float
    min_gain_giveback_ratio_pct: float

    @model_validator(mode="after")
    def _check_ranges(self) -> ProfitProtectionCandidateThresholds:
        _check_profit_protection_threshold_ranges(
            "candidate",
            self.min_current_gain_pct,
            self.min_drawdown_from_peak_pct,
            self.min_gain_giveback_ratio_pct,
        )
        return self


class ProfitProtectionStrongThresholds(StrictModel):
    """Strong Profit Protection(§3B)の閾値。Fair Value confidenceに依存せず、
    単独でPARTIAL_PROFIT_TAKEを成立可能とする。
    """

    min_current_gain_pct: float
    min_drawdown_from_peak_pct: float
    min_gain_giveback_ratio_pct: float

    @model_validator(mode="after")
    def _check_ranges(self) -> ProfitProtectionStrongThresholds:
        _check_profit_protection_threshold_ranges(
            "strong",
            self.min_current_gain_pct,
            self.min_drawdown_from_peak_pct,
            self.min_gain_giveback_ratio_pct,
        )
        return self


class ProfitProtectionConfig(StrictModel):
    enabled: bool
    candidate: ProfitProtectionCandidateThresholds
    strong: ProfitProtectionStrongThresholds

    @model_validator(mode="after")
    def _check_strong_at_least_as_strict_as_candidate(self) -> ProfitProtectionConfig:
        """コードレビュー対応(2026-08、指摘3): strongはcandidateより厳しい
        (以上の)閾値である必要がある。この前提が崩れると「strongの方が
        candidateより緩い」という設計矛盾が起動時に検出できないまま本番へ
        流れてしまう。
        """
        if self.strong.min_current_gain_pct < self.candidate.min_current_gain_pct:
            raise ValueError(
                "profit_protection.strong.min_current_gain_pctは"
                "candidate.min_current_gain_pct以上である必要があります"
            )
        if self.strong.min_drawdown_from_peak_pct < self.candidate.min_drawdown_from_peak_pct:
            raise ValueError(
                "profit_protection.strong.min_drawdown_from_peak_pctは"
                "candidate.min_drawdown_from_peak_pct以上である必要があります"
            )
        if self.strong.min_gain_giveback_ratio_pct < self.candidate.min_gain_giveback_ratio_pct:
            raise ValueError(
                "profit_protection.strong.min_gain_giveback_ratio_pctは"
                "candidate.min_gain_giveback_ratio_pct以上である必要があります"
            )
        return self


class ProfitTakingRulesConfig(StrictModel):
    version: int
    thresholds: ProfitTakingThresholds
    price_position: PricePositionThresholds
    mitigating_factors: MitigatingFactors
    event_proximity_notice: EventProximityNotice
    condition_based_judgment: ConditionBasedJudgment
    trading_unit: TradingUnitRules
    profit_protection: ProfitProtectionConfig
    partial_sell_ratios: PartialSellRatios


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


class NearBuyNotificationPolicy(StrictModel):
    notify_every_business_day: bool


class WatchBeforeEarningsNotificationPolicy(StrictModel):
    notify_every_business_day: bool


class NotificationPolicyConfig(StrictModel):
    """通知種別ごとの再送ポリシー分離(BUY候補裾野拡大機能2026-08)。

    BUY/SELL系は既存の`resend_after_days`/`price_change_resend_threshold_pct`
    (トップレベル、変更なし)をそのまま使い続ける。NEAR BUY/
    WATCH_BEFORE_EARNINGSのみ、このセクションで`notify_every_business_day`を
    見て通常の再送防止をバイパスする。
    """

    near_buy: NearBuyNotificationPolicy
    watch_before_earnings: WatchBeforeEarningsNotificationPolicy


class TradeCooldownConfig(StrictModel):
    """保有銘柄リストの変化から推定した売買イベント後、通常の売買推奨通知を
    抑止する営業日数(BUY候補裾野拡大機能2026-08)。重大リスク通知は貫通する。
    """

    enabled: bool
    buy_business_days: int
    sell_business_days: int
    partial_trade_business_days: int

    @model_validator(mode="after")
    def _check_values(self) -> TradeCooldownConfig:
        for value in (
            self.buy_business_days,
            self.sell_business_days,
            self.partial_trade_business_days,
        ):
            if value < 1:
                raise ValueError("trade_cooldownの営業日数はいずれも1以上である必要があります")
        return self


class WatchEndNotificationConfig(StrictModel):
    """NEAR BUYのWATCH終了通知(長期間監視していた場合のみ送る)の設定。"""

    enabled: bool
    min_consecutive_business_days: int


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
    # --- BUY候補裾野拡大機能(2026-08)で追加。既存キー(上記)は意味・既定値を
    # 変更しないため、BUY/SELL系の再送挙動は完全後方互換 ---
    notification_policy: NotificationPolicyConfig
    trade_cooldown: TradeCooldownConfig
    watch_end_notification: WatchEndNotificationConfig


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
            raise ValueError("category_thresholdsはスコアの定義域[-100, 100]に収まる必要があります")
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
            0
            <= self.rsi_oversold_boundary
            < self.rsi_neutral_boundary
            < self.rsi_sweet_spot_boundary
            < self.rsi_caution_boundary
            < self.rsi_overheat_boundary
            <= 100
        ):
            raise ValueError(
                "RSI区分境界は0 <= oversold < neutral < sweet_spot < caution "
                "< overheat <= 100の順である必要があります"
            )

        if self.drawdown_near_high_pct > 0:
            raise ValueError("drawdown_near_high_pctは0以下である必要があります")
        if not (
            self.drawdown_near_high_pct > self.drawdown_pullback_pct > self.drawdown_neutral_pct
        ):
            raise ValueError(
                "drawdown区分境界はnear_high > pullback > neutralの順(同値不可)である必要があります"
            )

        if not (
            self.ma20_breakdown_pct
            < self.ma20_pullback_low_pct
            < self.ma20_near_high_pct
            < self.ma20_overheat_pct
        ):
            raise ValueError(
                "ma20区分境界はbreakdown < pullback_low < near_high < overheatの"
                "順である必要があります"
            )
        if not (
            self.ma60_breakdown_pct
            < self.ma60_pullback_low_pct
            < self.ma60_near_high_pct
            < self.ma60_overheat_pct
        ):
            raise ValueError(
                "ma60区分境界はbreakdown < pullback_low < near_high < overheatの"
                "順である必要があります"
            )

        if not (
            0
            <= self.volume_low_threshold
            < self.volume_moderate_low
            <= self.volume_moderate_high
            < self.volume_extreme_threshold
        ):
            raise ValueError(
                "volume区分境界は0 <= low < moderate_low <= moderate_high < extremeの"
                "順である必要があります"
            )

        if self.overheat_five_day_return_pct_threshold <= 0:
            raise ValueError("overheat_five_day_return_pct_thresholdは正の値である必要があります")
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
            self.min_coverage_required
            <= self.coverage_medium_threshold
            < self.coverage_high_threshold
            <= 1
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
            raise ValueError("category_thresholdsはスコアの定義域[-100, 100]に収まる必要があります")
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
            self.min_coverage_required
            <= self.coverage_medium_threshold
            < self.coverage_high_threshold
            <= 1
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
            self.strong_improving > self.improving > self.deteriorating > self.strong_deteriorating
        ):
            raise ValueError(
                "category_thresholdsはstrong_improving > improving > deteriorating > "
                "strong_deterioratingの順である必要があります"
            )
        if not (self.strong_deteriorating >= -100 and self.strong_improving <= 100):
            raise ValueError("category_thresholdsはスコアの定義域[-100, 100]に収まる必要があります")
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
            self.trend_strong_decline_pct
            < self.trend_decline_pct
            < self.trend_improve_pct
            < self.trend_strong_improve_pct
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
            self.min_coverage_required
            <= self.coverage_medium_threshold
            < self.coverage_high_threshold
            <= 1
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
    # 「高配当株」の主条件(配当利回り単体基準。総合利回りは使わない。
    # BUY候補裾野拡大機能2026-08レビュー: 株主優待込みの総合利回りは
    # 「高配当」の定義とは区別する)。
    min_dividend_yield_pct: float
    max_payout_ratio_pct: float


class GrowthClassificationRules(StrictModel):
    # BUY候補裾野拡大機能2026-08: 配当利回り条件は撤廃(成長株が配当を
    # 出さない/低いことを要求しない)。営業利益トレンドのみを主条件とする。
    min_consecutive_growth_quarters: int


class ValueClassificationRules(StrictModel):
    # BUY候補裾野拡大機能2026-08: 配当利回り条件は撤廃。PBR/PERいずれかの
    # 現在水準のみで独立判定する(過去中央値比較=Historical Valuationは
    # Shadow計測専用のため使わない)。
    max_pbr: float
    max_per: float


class DividendGrowthClassificationRules(StrictModel):
    # 主条件は連続増配年数。min_dividend_growth_pctはハードなAND条件では
    # 使わず、classification_basisの補助表示にのみ用いる(BUY候補裾野
    # 拡大機能2026-08レビュー: 一時的な増配率の低さで長期連続増配企業を
    # 分類対象から落とさないため)。
    min_consecutive_dividend_increase_years: int
    min_dividend_growth_pct: float


class QualityClassificationRules(StrictModel):
    # screening.financial_health.min_equity_ratio_pct(健全性の最低ライン)
    # とは意味の異なる、「優良」と呼べる水準の独自閾値(BUY候補裾野拡大
    # 機能2026-08レビュー: 無理な閾値統合はしない)。
    min_equity_ratio_pct: float
    min_roe_pct: float
    require_earnings_trend_non_decreasing: bool


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
    dividend_growth: DividendGrowthClassificationRules
    quality: QualityClassificationRules


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
            raise ValueError("adjustment_multipliersはentry <= standard <= strongの順序が必要です")
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
            raise ValueError("maximum_marginはentry <= standard <= strongの順序が必要です")
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


class NearBuyConfig(StrictModel):
    """NEAR BUY(BuyAction.WATCH_FOR_PRICEのうち積極監視対象)の閾値
    (BUY候補裾野拡大機能2026-08)。"""

    start_required_decline_pct: float
    continue_required_decline_pct: float
    min_company_quality_score: float
    daily_max_notifications: int
    # 評価不能(DATA_INSUFFICIENT)が続いた場合にWatchStateを終了させる
    # までの最大営業日数(安全弁)。「監視終了」通知を送るかどうかの閾値は
    # notification_rules.yamlのwatch_end_notification.min_consecutive_
    # business_daysが正本(重複定義しない)。
    max_stale_business_days: int

    @model_validator(mode="after")
    def _check_order(self) -> NearBuyConfig:
        if self.start_required_decline_pct > self.continue_required_decline_pct:
            raise ValueError(
                "near_buyはstart_required_decline_pct <= continue_required_decline_pctが必要です"
            )
        for value in (self.daily_max_notifications, self.max_stale_business_days):
            if value < 1:
                raise ValueError("near_buyの営業日数・件数はいずれも1以上である必要があります")
        return self


class BuyDecisionRulesConfig(StrictModel):
    version: int
    score_thresholds: BuyScoreThresholds
    valuation_dispersion: ValuationDispersionThresholds
    earnings_window: BuyEarningsWindowConfig
    margin_of_safety: MarginOfSafetyConfig
    undervaluation_category_caps: UndervaluationCategoryCaps
    near_buy: NearBuyConfig


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


class MonitoringScoreConfig(StrictModel):
    """ウォッチリスト自動追加基準の再設計(2026-08)で追加。

    合否(対象StockTypeへの該当有無)とは独立した、ランキング専用の
    MonitoringScore(「ウォッチリストへ優先して入れる価値」)の配点。
    価格の割安さは一切含めない(高い銘柄も将来の下落監視に価値があるため)。
    自己資本比率の閾値はscreening.financial_health.min_equity_ratio_pctを、
    時価総額の閾値はWatchlistScreeningThresholds.minimum_market_cap_yenを
    それぞれ再利用し、ここでは重複定義しない。100点が上限(コード側で固定)。
    """

    base_score: float = Field(gt=0)
    # 2タイプ目以降、1タイプにつき加算するボーナス。
    additional_type_bonus: float = Field(ge=0)
    max_type_bonus: float = Field(ge=0)
    equity_ratio_bonus: float = Field(ge=0)
    positive_operating_cashflow_bonus: float = Field(ge=0)
    no_deficit_bonus: float = Field(ge=0)
    no_recent_dividend_cut_bonus: float = Field(ge=0)
    market_cap_bonus: float = Field(ge=0)


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


class RotationConfig(StrictModel):
    """ウォッチリスト新規候補選定の永続ラウンドロビン方式(2026-08)の設定。

    enabled=falseの場合、従来の固定スライス方式(候補ユニバース出現順の
    先頭からcandidate_limit件)へフォールバックする(移行時の安全弁)。
    """

    enabled: bool


class AutoRemovalConfig(StrictModel):
    """AUTO_SCREENING銘柄の自動メンテナンス(再評価・自動削除)設定(2026-08)。

    MANUAL登録銘柄はこの設定の対象外(常に保護される、コード側で強制)。
    """

    enabled: bool
    # 登録からこの日数が経過するまでは、非該当が続いても削除対象にしない
    # (企業として長期監視する価値そのものを保護する)。
    minimum_age_days: int = Field(gt=0)
    # この回数だけ連続して非該当(対象5タイプいずれにも非該当、またはハード除外)と
    # 判定された場合に削除候補となる(即時削除対象の理由を除く)。
    consecutive_not_qualified_required: int = Field(gt=0)
    # 初回非該当(removal_candidate_since)からこの日数以上経過していることも
    # 削除の必須条件とする(週次実行回数だけに依存した拙速な削除を防ぐ)。
    minimum_not_qualified_span_days: int = Field(gt=0)
    # 運用ドキュメント記載用の目安値(メンテナンス実行間隔)。判定ロジックには
    # 直接使わない。
    stale_recheck_days: int = Field(gt=0)
    # データ取得エラー等で有効な再評価ができないままこの日数を超えた場合、
    # 削除はせず監査記録・運用警告に留める。
    maximum_unconfirmed_days: int = Field(gt=0)
    # 削除された銘柄が同一条件で再追加されるまでの最低待機日数
    # (削除→即再追加→削除、という振動を防ぐ)。
    readd_cooldown_days: int = Field(gt=0)


class WatchlistScreeningRulesConfig(StrictModel):
    enabled: bool
    # EventBridge(スケジュール自動実行)からの起動だけをON/OFFする。CLI手動
    # 実行には影響しない。横断整合性レビュー対応(2026-08、指摘9)で
    # weekly_schedule_enabledから改称: このフラグはNEW_CANDIDATE_SCREENING
    # (平日毎日06:00のEventBridge起動)だけでなく、その正常finalize後に
    # 自己invokeで起動するWATCHLIST_MAINTENANCE(独立したEventBridge Scheduleは
    # 2026-08-16改訂で廃止済み)も含め、Dispatcherへのあらゆる起動経路を
    # 一括で制御しており、特定のcadence(週次/日次)専用ではないため。
    scheduled_run_enabled: bool
    notification_enabled: bool
    candidate_universe: CandidateUniverseConfig
    # Part B(高速化): "lightweight"は必要最小限の項目のみ取得するProviderへ
    # 切り替える(既定は引き続き"stock_snapshot"、同値性テスト通過後に本番既定を
    # 変更する)。
    screening_data_provider: Literal["stock_snapshot", "lightweight"]
    # multi_style_monitoring: ウォッチリスト自動追加基準の再設計(2026-08)で追加した
    # 本番既定Policy。high_dividend_financial_healthは後方互換・比較用に残す。
    screening_policy: Literal["high_dividend_financial_health", "multi_style_monitoring"]
    max_watchlist_additions_per_run: int = Field(gt=0)
    max_missing_fields: int = Field(ge=0)
    thresholds: WatchlistScreeningThresholds
    scoring: WatchlistScreeningScoringConfig
    # ウォッチリスト自動追加基準の再設計(2026-08)で追加。multi_style_monitoring専用。
    monitoring_score: MonitoringScoreConfig

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

    # --- ウォッチリスト自動運用の改善(ローテーション・自動メンテナンス、2026-08)で追加 ---
    rotation: RotationConfig
    auto_removal: AutoRemovalConfig


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


# --- entry_exit_price_rules.yaml(判定精度向上機能次フェーズSTEP2、Entry/Exit
# Price Range Shadow) ---------------------------------------------------


class EntryMarginByConfidenceTier(StrictModel):
    """信頼度tier1つ分の、4レベル(max/starter/preferred/strong)ベースmargin
    (fraction、Historical Valuation調整前)。price = anchor * (1 - margin)の
    marginであり、値が大きいほど価格は下がる(=より安く買う設定)。"""

    max: float
    starter: float
    preferred: float
    strong: float

    @model_validator(mode="after")
    def _check_order(self) -> EntryMarginByConfidenceTier:
        if not (self.max < self.starter < self.preferred < self.strong):
            raise ValueError(
                "margin_by_confidence_fractionはmax < starter < preferred < strongの"
                "順である必要があります"
            )
        if self.max < 0:
            raise ValueError("max marginは0以上である必要があります")
        return self


class EntryMarginByConfidence(StrictModel):
    high: EntryMarginByConfidenceTier
    medium: EntryMarginByConfidenceTier


class HistoricalValuationMarginAdjustmentConfig(StrictModel):
    """Historical Valuation Categoryごとの、Entry margin調整量(fraction、
    4レベル全てへ同一加算)。CHEAP系は負(margin縮小=高めに買っても良い)、
    EXPENSIVE系は正(margin拡大=より安くならないと買わない)。

    category→config値の対応はdict[Category, str]による型安全なlookup
    (domain/signals/entry_price_range.py)を介して行い、Category.valueでの
    暗黙dictインデックスは行わない(将来のCategory追加時に無条件でNORMAL
    扱いされることを防ぐため)。
    """

    historically_very_cheap: float
    cheap: float
    normal: float
    expensive: float
    very_expensive: float

    @model_validator(mode="after")
    def _check_finite(self) -> HistoricalValuationMarginAdjustmentConfig:
        for name, value in self.model_dump().items():
            if not math.isfinite(value):
                raise ValueError(
                    f"historical_valuation_margin_adjustment_fraction.{name}は"
                    "有限の値である必要があります"
                )
        return self


class TimingNudgeStrengthConfig(StrictModel):
    """Timing Categoryごとの、preferred_entry_priceへのnudge強度(fraction、
    非負)。方向(現在値側/技術的target側)はcategory自体(TAILWIND系/
    HEADWIND系)から決定するため、ここでは符号付き値を許可しない(符号付き
    直接blendは外挿になりうるため設計上禁止)。"""

    strong_tailwind: float
    tailwind: float
    neutral: float
    headwind: float
    strong_headwind: float

    @model_validator(mode="after")
    def _check_non_negative_and_finite(self) -> TimingNudgeStrengthConfig:
        for name, value in self.model_dump().items():
            if not math.isfinite(value):
                raise ValueError(
                    f"timing_nudge_strength_fraction.{name}は有限の値である必要があります"
                )
            if value < 0:
                raise ValueError(
                    "timing_nudge_strength_fractionは全て0以上(非負の強度)である"
                    "必要があります(方向はcategoryから決定するため符号付き値は禁止)"
                )
        return self


class EntryPriceRangeConfig(StrictModel):
    """Entry Price Range Shadow(判定精度向上機能次フェーズSTEP2)の設定。

    アルゴリズム全体: 信頼度tier別base margin表 → Historical Valuation
    adjustmentを4レベル全てへ同一加算(floor 0) → Timing nudge(preferredの
    みtarget+strength方式) → top-down正規化(max→starter→preferred→strongへ
    min()による一方向キャップ、valuation_ceiling=fair_value_range.neutralを
    絶対上限とする)。
    """

    model_version: str
    margin_by_confidence_fraction: EntryMarginByConfidence
    historical_valuation_margin_adjustment_fraction: HistoricalValuationMarginAdjustmentConfig
    timing_nudge_strength_fraction: TimingNudgeStrengthConfig
    min_price_gap_fraction: float
    historical_valuation_overlay_weight: float
    timing_overlay_weight: float
    technical_ma_overlay_weight: float
    min_coverage_required: float
    coverage_high_threshold: float
    coverage_medium_threshold: float

    @model_validator(mode="after")
    def _check_values(self) -> EntryPriceRangeConfig:
        if not (math.isfinite(self.min_price_gap_fraction) and self.min_price_gap_fraction > 0):
            raise ValueError("min_price_gap_fractionは正の有限値である必要があります")
        if self.min_price_gap_fraction >= 1:
            raise ValueError("min_price_gap_fractionは1未満である必要があります")
        weights = (
            self.historical_valuation_overlay_weight,
            self.timing_overlay_weight,
            self.technical_ma_overlay_weight,
        )
        if any((not math.isfinite(w)) or w < 0 for w in weights):
            raise ValueError("overlay weightは全て0以上の有限値である必要があります")
        if not math.isclose(sum(weights), 1.0, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(
                "historical_valuation/timing/technical_ma のoverlay weight合計は"
                "1.0である必要があります"
            )
        if not (0 < self.min_coverage_required <= 1):
            raise ValueError("min_coverage_requiredは0より大きく1以下である必要があります")
        if not (
            self.min_coverage_required
            <= self.coverage_medium_threshold
            < self.coverage_high_threshold
            <= 1
        ):
            raise ValueError(
                "min_coverage_required <= coverage_medium_threshold < "
                "coverage_high_threshold <= 1である必要があります"
            )
        # コードレビュー対応(STEP2 §7): adjusted_margin = max(0, base_margin +
        # historical_adjustment)がどの信頼度tier・どのHistorical Valuation
        # categoryの組み合わせでも1未満であることを起動時に保証する(1以上に
        # なるとEntry priceが0以下になり得るため)。floorは0側のみなので、
        # 起こりうる最悪ケースは「最大のadjustment値」を加算した場合。
        max_adjustment = max(
            self.historical_valuation_margin_adjustment_fraction.model_dump().values()
        )
        for tier in (
            self.margin_by_confidence_fraction.high,
            self.margin_by_confidence_fraction.medium,
        ):
            for base_margin in (tier.max, tier.starter, tier.preferred, tier.strong):
                worst_case_margin = max(0.0, base_margin + max_adjustment)
                if worst_case_margin >= 1:
                    raise ValueError(
                        "margin_by_confidence_fractionとhistorical_valuation_margin_"
                        "adjustment_fractionの組み合わせで、adjusted_marginが1以上に"
                        "なり得ます(Entry priceが0以下になるため設定を見直してください)"
                    )
        return self


class HistoricalValuationExitAdjustmentConfig(StrictModel):
    """Historical Valuation Categoryごとの、Exit adjustment量(fraction、
    neutral/bull両方のfair valueへ同一適用)。CHEAP系は正(=遅める)、
    EXPENSIVE系は負(=早める)。"""

    historically_very_cheap: float
    cheap: float
    normal: float
    expensive: float
    very_expensive: float

    @model_validator(mode="after")
    def _check_finite(self) -> HistoricalValuationExitAdjustmentConfig:
        for name, value in self.model_dump().items():
            if not math.isfinite(value):
                raise ValueError(
                    f"historical_valuation_adjustment_fraction.{name}は有限の値である必要があります"
                )
        return self


class TimingExitAdjustmentConfig(StrictModel):
    """Timing Categoryごとの、Exit adjustment量(fraction、neutral/bull両方の
    fair valueへ同一適用)。TAILWIND系は正(=遅める)、HEADWIND系は負
    (=早める)。"""

    strong_tailwind: float
    tailwind: float
    neutral: float
    headwind: float
    strong_headwind: float

    @model_validator(mode="after")
    def _check_finite(self) -> TimingExitAdjustmentConfig:
        for name, value in self.model_dump().items():
            if not math.isfinite(value):
                raise ValueError(f"timing_adjustment_fraction.{name}は有限の値である必要があります")
        return self


class ExitPriceRangeConfig(StrictModel):
    """Exit Price Range Shadow(判定精度向上機能次フェーズSTEP2)の設定。

    アルゴリズム全体: neutral/bull fair valueへHistorical Valuation/Timing
    adjustmentを同一適用しadjusted_neutral_fv/adjusted_bull_fvを算出 →
    partial_low/high(adjusted_neutral_fv基準)・strong(adjusted_bull_fv基準)の
    3価格算出 → 防御的な下限方向のみの正規化。downside_review_price/
    exit_review_priceはaverage_purchase_price基準の別系統(loss_tolerance/
    review_return_thresholdのみに依存し、上記3価格には一切影響しない)。
    """

    model_version: str
    historical_valuation_adjustment_fraction: HistoricalValuationExitAdjustmentConfig
    timing_adjustment_fraction: TimingExitAdjustmentConfig
    partial_zone_width_fraction: float
    min_price_gap_fraction: float
    loss_tolerance_fraction: float
    review_return_threshold_fraction: float
    historical_valuation_overlay_weight: float
    timing_overlay_weight: float
    min_coverage_required: float
    coverage_high_threshold: float
    coverage_medium_threshold: float

    @model_validator(mode="after")
    def _check_values(self) -> ExitPriceRangeConfig:
        if not (
            math.isfinite(self.partial_zone_width_fraction)
            and 0 <= self.partial_zone_width_fraction < 1
        ):
            raise ValueError("partial_zone_width_fractionは0以上1未満の有限値である必要があります")
        if not (math.isfinite(self.min_price_gap_fraction) and 0 < self.min_price_gap_fraction < 1):
            raise ValueError("min_price_gap_fractionは0より大きく1未満の有限値である必要があります")
        if not (
            math.isfinite(self.loss_tolerance_fraction) and 0 <= self.loss_tolerance_fraction < 1
        ):
            raise ValueError("loss_tolerance_fractionは0以上1未満の有限値である必要があります")
        if not (
            math.isfinite(self.review_return_threshold_fraction)
            and self.review_return_threshold_fraction >= 0
        ):
            raise ValueError("review_return_threshold_fractionは0以上の有限値である必要があります")
        weights = (self.historical_valuation_overlay_weight, self.timing_overlay_weight)
        if any((not math.isfinite(w)) or w < 0 for w in weights):
            raise ValueError("overlay weightは全て0以上の有限値である必要があります")
        if not math.isclose(sum(weights), 1.0, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(
                "historical_valuation/timing のoverlay weight合計は1.0である必要があります"
            )
        if not (0 < self.min_coverage_required <= 1):
            raise ValueError("min_coverage_requiredは0より大きく1以下である必要があります")
        if not (
            self.min_coverage_required
            <= self.coverage_medium_threshold
            < self.coverage_high_threshold
            <= 1
        ):
            raise ValueError(
                "min_coverage_required <= coverage_medium_threshold < "
                "coverage_high_threshold <= 1である必要があります"
            )
        # コードレビュー対応(STEP2 §8): 1 + historical_adjustment +
        # timing_adjustmentがどのcategoryの組み合わせでも0より大きいことを
        # 起動時に保証する(0以下になるとExit priceが0以下になり得るため)。
        # 最悪ケースは両方の最小値を同時に採用した場合。
        min_hv_adjustment = min(self.historical_valuation_adjustment_fraction.model_dump().values())
        min_timing_adjustment = min(self.timing_adjustment_fraction.model_dump().values())
        if not (1 + min_hv_adjustment + min_timing_adjustment > 0):
            raise ValueError(
                "historical_valuation_adjustment_fractionとtiming_adjustment_"
                "fractionの最小値同士を同時に適用した場合でも、1+adjustment合計は"
                "0より大きい必要があります(Exit priceが0以下になるため設定を"
                "見直してください)"
            )
        return self


class EntryExitPriceRulesConfig(StrictModel):
    entry: EntryPriceRangeConfig
    exit: ExitPriceRangeConfig


class EnvironmentCategoryThresholds(StrictModel):
    """scoreからEnvironmentCategoryへ変換する閾値(Market/Sector/Environment
    それぞれ独立に持つ)。strong_headwind < headwind < 0 < tailwind <
    strong_tailwindの順序をvalidatorで保証する。"""

    strong_tailwind: float
    tailwind: float
    headwind: float
    strong_headwind: float

    @model_validator(mode="after")
    def _check_order(self) -> EnvironmentCategoryThresholds:
        values = (self.strong_tailwind, self.tailwind, self.headwind, self.strong_headwind)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("category_thresholdsは全て有限値である必要があります")
        if not (self.strong_headwind < self.headwind < 0 < self.tailwind < self.strong_tailwind):
            raise ValueError(
                "strong_headwind < headwind < 0 < tailwind < strong_tailwindである必要があります"
            )
        return self


class TrendClassificationScoreConfig(StrictModel):
    """TrendClassification(momentum.pyのclassify_trend()が返す5区分)を
    そのままtrend_structure_componentの点数へ変換する対応表。"""

    strong_uptrend: float
    uptrend: float
    neutral: float
    downtrend: float
    strong_downtrend: float

    @model_validator(mode="after")
    def _check_finite(self) -> TrendClassificationScoreConfig:
        for name, value in self.model_dump().items():
            if not math.isfinite(value):
                raise ValueError(f"trend_classification_score.{name}は有限の値である必要があります")
        return self


class MarketEnvironmentComponentWeights(StrictModel):
    trend_structure: float
    medium_term_return: float
    drawdown: float


class SectorEnvironmentComponentWeights(StrictModel):
    trend_structure: float
    medium_term_return: float
    relative_strength: float


class EnvironmentCompositeWeights(StrictModel):
    market: float
    sector: float


class MarketEnvironmentConfig(StrictModel):
    """Market Environment Score(判定精度向上機能Phase D)の設定。TOPIX bars
    のみを使用し、trend_structure(MA20/60・slopeをclassify_trend()で分類)・
    medium_term_return(20d/60d return)・drawdown(直近高値からの下落率)の
    3成分の加重平均でscoreを算出する(評価可能成分のみで再正規化)。
    """

    model_version: str
    component_weights: MarketEnvironmentComponentWeights
    trend_classification_score: TrendClassificationScoreConfig
    ma_slope_lookback_days: int
    return_score_scale_pct: float
    drawdown_window_days: int
    drawdown_neutral_threshold_pct: float
    drawdown_scale_pct: float
    min_bars_ma60: int
    min_bars_return_60d: int
    max_bar_staleness_business_days: int
    min_coverage_required: float
    coverage_high_threshold: float
    coverage_medium_threshold: float
    category_thresholds: EnvironmentCategoryThresholds

    @model_validator(mode="after")
    def _check_values(self) -> MarketEnvironmentConfig:
        weights = (
            self.component_weights.trend_structure,
            self.component_weights.medium_term_return,
            self.component_weights.drawdown,
        )
        if any((not math.isfinite(w)) or w < 0 for w in weights):
            raise ValueError("component_weightsは全て0以上の有限値である必要があります")
        if not math.isclose(sum(weights), 1.0, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("component_weightsの合計は1.0である必要があります")
        if not (0 < self.min_coverage_required <= 1):
            raise ValueError("min_coverage_requiredは0より大きく1以下である必要があります")
        if not (
            self.min_coverage_required
            <= self.coverage_medium_threshold
            < self.coverage_high_threshold
            <= 1
        ):
            raise ValueError(
                "min_coverage_required <= coverage_medium_threshold < "
                "coverage_high_threshold <= 1である必要があります"
            )
        if not (math.isfinite(self.return_score_scale_pct) and self.return_score_scale_pct > 0):
            raise ValueError("return_score_scale_pctは正の有限値である必要があります")
        if not (math.isfinite(self.drawdown_scale_pct) and self.drawdown_scale_pct > 0):
            raise ValueError("drawdown_scale_pctは正の有限値である必要があります")
        if not math.isfinite(self.drawdown_neutral_threshold_pct):
            raise ValueError("drawdown_neutral_threshold_pctは有限値である必要があります")
        if self.ma_slope_lookback_days <= 0:
            raise ValueError("ma_slope_lookback_daysは正の整数である必要があります")
        if self.drawdown_window_days <= 0:
            raise ValueError("drawdown_window_daysは正の整数である必要があります")
        if self.min_bars_ma60 <= 0 or self.min_bars_return_60d <= 0:
            raise ValueError("min_bars_ma60/min_bars_return_60dは正の整数である必要があります")
        if self.max_bar_staleness_business_days < 0:
            raise ValueError("max_bar_staleness_business_daysは0以上の整数である必要があります")
        return self


class SectorEnvironmentConfig(StrictModel):
    """Sector Environment Score(判定精度向上機能Phase D)の設定。
    config.momentum.sector_etf_mapに対応ETFが登録されている業種のみ評価する。
    trend_structure/medium_term_returnはMarketと同じ算出方式をsector barsへ
    適用し、drawdown成分の代わりにTOPIXへの相対強度(relative_strength)を
    主要成分として持つ。
    """

    model_version: str
    component_weights: SectorEnvironmentComponentWeights
    trend_classification_score: TrendClassificationScoreConfig
    ma_slope_lookback_days: int
    return_score_scale_pct: float
    relative_strength_scale_pct: float
    min_bars_return_60d: int
    max_bar_staleness_business_days: int
    min_coverage_required: float
    coverage_high_threshold: float
    coverage_medium_threshold: float
    category_thresholds: EnvironmentCategoryThresholds

    @model_validator(mode="after")
    def _check_values(self) -> SectorEnvironmentConfig:
        weights = (
            self.component_weights.trend_structure,
            self.component_weights.medium_term_return,
            self.component_weights.relative_strength,
        )
        if any((not math.isfinite(w)) or w < 0 for w in weights):
            raise ValueError("component_weightsは全て0以上の有限値である必要があります")
        if not math.isclose(sum(weights), 1.0, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("component_weightsの合計は1.0である必要があります")
        if not (0 < self.min_coverage_required <= 1):
            raise ValueError("min_coverage_requiredは0より大きく1以下である必要があります")
        if not (
            self.min_coverage_required
            <= self.coverage_medium_threshold
            < self.coverage_high_threshold
            <= 1
        ):
            raise ValueError(
                "min_coverage_required <= coverage_medium_threshold < "
                "coverage_high_threshold <= 1である必要があります"
            )
        if not (math.isfinite(self.return_score_scale_pct) and self.return_score_scale_pct > 0):
            raise ValueError("return_score_scale_pctは正の有限値である必要があります")
        if not (
            math.isfinite(self.relative_strength_scale_pct) and self.relative_strength_scale_pct > 0
        ):
            raise ValueError("relative_strength_scale_pctは正の有限値である必要があります")
        if self.ma_slope_lookback_days <= 0:
            raise ValueError("ma_slope_lookback_daysは正の整数である必要があります")
        if self.min_bars_return_60d <= 0:
            raise ValueError("min_bars_return_60dは正の整数である必要があります")
        if self.max_bar_staleness_business_days < 0:
            raise ValueError("max_bar_staleness_business_daysは0以上の整数である必要があります")
        return self


class EnvironmentCompositeConfig(StrictModel):
    """Environment Composite Score(判定精度向上機能Phase D)の設定。Marketを
    必須バックボーンとし、Sectorが評価可能なら加重平均、評価不能ならMarket
    のみで評価を継続する(sector_missing_confidence_capでconfidence上限を
    キャップする)。

    コードレビュー対応(2026-08): Environment自身のcoverage(Market/Sectorの
    coverageから合成した値)が低い場合にNOT_EVALUATEDとする閾値、および
    DecisionPerformanceのcoverage tier分析(historical_valuation等の既存
    4スコアと同様の仕組み)を機能させるためのcoverage閾値を追加した。
    """

    model_version: str
    composite_weights: EnvironmentCompositeWeights
    sector_missing_confidence_cap: Literal["LOW", "MEDIUM", "HIGH"]
    min_coverage_required: float
    coverage_high_threshold: float
    coverage_medium_threshold: float
    category_thresholds: EnvironmentCategoryThresholds

    @model_validator(mode="after")
    def _check_values(self) -> EnvironmentCompositeConfig:
        weights = (self.composite_weights.market, self.composite_weights.sector)
        if any((not math.isfinite(w)) or w < 0 for w in weights):
            raise ValueError("composite_weightsは全て0以上の有限値である必要があります")
        if not math.isclose(sum(weights), 1.0, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("composite_weightsの合計は1.0である必要があります")
        if not (0 < self.min_coverage_required <= 1):
            raise ValueError("min_coverage_requiredは0より大きく1以下である必要があります")
        if not (
            self.min_coverage_required
            <= self.coverage_medium_threshold
            < self.coverage_high_threshold
            <= 1
        ):
            raise ValueError(
                "min_coverage_required <= coverage_medium_threshold < "
                "coverage_high_threshold <= 1である必要があります"
            )
        return self


class MarketSectorEnvironmentRulesConfig(StrictModel):
    market: MarketEnvironmentConfig
    sector: SectorEnvironmentConfig
    environment: EnvironmentCompositeConfig


# --- shareholder_return_policies.yaml(Issue #30 Phase 1) --------------------


class ShareholderReturnPolicyEntry(StrictModel):
    """株主還元方針レジストリの1銘柄分。

    Trueの正式認定(累進配当/DOE)は、会社の一次情報を人間が確認して
    このレジストリへ登録した場合のみ。過去実績(減配なし・連続増配等)・
    キーワードヒット・LLM出力から推測して登録してはならない。
    policy_type=NONEは「確認したが方針なし」であり、単に見つからなかった
    ことを理由に登録してはならない(未確認はレジストリ非掲載=UNKNOWNで表す)。
    確認実施者はGit履歴(commit author)で追跡する(CLAUDE.mdのPIIルールにより
    実在人物名をこのファイルへ記録しない)。
    """

    stock_code: str = Field(pattern=r"^[0-9][0-9A-Z]{3}$")
    policy_type: ShareholderReturnPolicyType
    status: Literal["CONFIRMED"]
    source_type: Literal["COMPANY_IR", "EDINET_ANNUAL_REPORT", "TDNET_DISCLOSURE", "OTHER"]
    # 一次情報への参照(IRページURL・EDINET docID等)。監査の起点となるため必須
    source_reference: str = Field(min_length=1)
    source_date: _dt.date
    # 根拠文の引用(全文はRecommendationへはコピーせず、正本はこのレジストリのみが持つ)
    evidence_text: str = Field(min_length=1)
    checked_at: _dt.date


class ShareholderReturnPoliciesConfig(StrictModel):
    version: int
    policies: list[ShareholderReturnPolicyEntry]

    @model_validator(mode="after")
    def _validate_unique_stock_codes(self) -> ShareholderReturnPoliciesConfig:
        """1 stock_code = 1有効レコード。過去の方針変更履歴はGit履歴で追跡する。"""
        seen: set[str] = set()
        for entry in self.policies:
            if entry.stock_code in seen:
                raise ValueError(f"stock_codeが重複しています: {entry.stock_code}")
            seen.add(entry.stock_code)
        return self

    def entry_for(self, stock_code: str) -> ShareholderReturnPolicyEntry | None:
        for entry in self.policies:
            if entry.stock_code == stock_code:
                return entry
        return None


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
    # --- 判定精度向上機能次フェーズSTEP2: Entry/Exit Price Range Shadow
    # (2026-08)で追加 ---
    entry_exit_price: EntryExitPriceRulesConfig
    # --- 判定精度向上機能Phase D: Market/Sector Environment Shadow
    # (2026-08)で追加 ---
    market_sector_environment: MarketSectorEnvironmentRulesConfig
    # --- 株主還元方針レジストリ(Issue #30 Phase 1、2026-08)で追加 ---
    shareholder_return_policies: ShareholderReturnPoliciesConfig
