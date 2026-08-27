"""推奨記録(要求仕様26節)。推奨時点の情報を変更不能なスナップショットとして保存する。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from pydantic import Field

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
    EarningsDecisionRelevance,
    EarningsReleaseConfirmationState,
    PriceRangeEvaluationState,
    ProfitTakingIndustrySector,
    RecommendationType,
    RecordDateUnknownReason,
    SourceType,
    StockType,
    WatchType,
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
    # Phase 2-B「銘柄分析」向け(2026-08): company_quality_score算出に実際に
    # 使用した判定時点の入力事実のうち、score_breakdown(算出結果)にも
    # config_values_used(設定パラメータ)にも残らないもの(PER/PBR実数値、
    # 自己資本比率、配当性向、割安度6シグナルの真偽値等)を保存する。
    # 表示専用のスナップショットであり、投資判断ロジックには一切使用しない。
    buy_score_input_facts: dict[str, Any] | None = None

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

    # --- 判定精度向上機能Phase B: Historical Valuation Score(2026-08追加、
    # コードレビュー対応で構造化。Shadow計測専用)。銘柄自身の過去PER/PBR水準に
    # 対する現在値のランクベース評価結果。StockSnapshot.historical_valuationを
    # そのままコピーしたものであり、DecisionSnapshotへ記録する以外の用途では
    # 一切使わない。BUY候補判定・保有判断スコア・旧売却判定・ProfitTaking判定・
    # LINE通知など既存の判定ロジックからは参照しないこと ---
    historical_valuation_score: float | None = None
    historical_valuation_confidence: ConfidenceLevel | None = None
    historical_valuation_coverage: float | None = None
    historical_valuation_reason_codes: tuple[str, ...] = ()
    historical_valuation_metrics: dict[str, Any] = Field(default_factory=dict)

    # --- 判定精度向上機能Phase B第二弾: Timing Score(2026-08追加、Shadow計測
    # 専用)。既存MomentumSnapshotを基にしたモメンタムベースの技術的タイミング
    # 評価結果。StockSnapshot.timingをそのままコピーしたものであり、
    # DecisionSnapshotへ記録する以外の用途では一切使わない。BUY候補判定・
    # 保有判断スコア・旧売却判定・ProfitTaking判定・LINE通知など既存の判定
    # ロジックからは参照しないこと ---
    timing_score: float | None = None
    timing_confidence: ConfidenceLevel | None = None
    timing_coverage: float | None = None
    timing_reason_codes: tuple[str, ...] = ()
    timing_metrics: dict[str, Any] = Field(default_factory=dict)

    # --- 判定精度向上機能Phase C: Earnings Surprise Score(2026-08追加、
    # Shadow計測専用、コードレビュー対応でv3へ再設計)。直近確定四半期の
    # Yahoo Finance Earnings Historyが返すEPS実績/予想値の乖離(Analyst
    # Consensus Surprise単一成分)を基にした決算サプライズ評価結果。
    # StockSnapshot.earnings_surpriseをそのままコピーしたものであり、
    # DecisionSnapshotへ記録する以外の用途では一切使わない。BUY候補判定・
    # 保有判断スコア・旧売却判定・ProfitTaking判定・LINE通知など既存の判定
    # ロジックからは参照しないこと ---
    earnings_surprise_score: float | None = None
    earnings_surprise_confidence: ConfidenceLevel | None = None
    earnings_surprise_coverage: float | None = None
    earnings_surprise_reason_codes: tuple[str, ...] = ()
    earnings_surprise_metrics: dict[str, Any] = Field(default_factory=dict)

    # --- 判定精度向上機能Phase C: Earnings Trend Score(2026-08追加、Shadow
    # 計測専用)。営業利益/営業CFトレンド+配当方向を基にした業績トレンド評価
    # 結果。StockSnapshot.earnings_trendをそのままコピーしたものであり、
    # DecisionSnapshotへ記録する以外の用途では一切使わない。BUY候補判定・
    # 保有判断スコア・旧売却判定・ProfitTaking判定・LINE通知など既存の判定
    # ロジックからは参照しないこと ---
    earnings_trend_score: float | None = None
    earnings_trend_confidence: ConfidenceLevel | None = None
    earnings_trend_coverage: float | None = None
    earnings_trend_reason_codes: tuple[str, ...] = ()
    earnings_trend_metrics: dict[str, Any] = Field(default_factory=dict)

    # --- 判定精度向上機能次フェーズSTEP2: Entry Price Range Shadow(2026-08
    # 追加、Shadow計測専用)。4段階の目安買付価格帯(strong/preferred/starter/
    # max)とstop_review_price。StockSnapshot.entry_price_rangeをそのまま
    # コピーしたものであり、DecisionSnapshotへ記録する以外の用途では一切
    # 使わない。既存のentry_buy_price/standard_buy_price/strong_buy_price・
    # buy_prices・BUY候補判定・保有判断スコア・旧売却判定・ProfitTaking判定・
    # LINE通知など既存の判定ロジックからは参照しないこと。stateは他4スコアの
    # ようなscoreフィールドを持たないEntry/Exitにとって、DecisionPerformance
    # 分析等が「評価済みレコードだけを安全に抽出する」ための主要な識別子 ---
    entry_price_range_state: PriceRangeEvaluationState | None = None
    entry_price_range_confidence: ConfidenceLevel | None = None
    entry_price_range_coverage: float | None = None
    entry_price_range_reason_codes: tuple[str, ...] = ()
    entry_price_range_metrics: dict[str, Any] = Field(default_factory=dict)
    entry_price_range_starter_price: Decimal | None = None
    entry_price_range_preferred_price: Decimal | None = None
    entry_price_range_strong_price: Decimal | None = None
    entry_price_range_max_price: Decimal | None = None
    entry_price_range_stop_review_price: Decimal | None = None

    # --- 判定精度向上機能次フェーズSTEP2: Exit Price Range Shadow(2026-08
    # 追加、Shadow計測専用)。一部利確ゾーン(partial_low/high)・強気利確価格
    # (strong)・取得単価基準レビューライン(downside_review/exit_review)。
    # SELL(legacy)/ProfitTaking/HoldingDecisionの各パイプラインが個別に
    # 算出した結果をそのままコピーしたものであり(Builder自身は算出しない)、
    # DecisionSnapshotへ記録する以外の用途では一切使わない。既存の
    # sell_prices・旧売却判定・ProfitTaking判定・LINE通知など既存の判定
    # ロジックからは参照しないこと。BUYパイプラインはExitを計算しないため
    # 常にNone ---
    exit_price_range_state: PriceRangeEvaluationState | None = None
    exit_price_range_confidence: ConfidenceLevel | None = None
    exit_price_range_coverage: float | None = None
    exit_price_range_reason_codes: tuple[str, ...] = ()
    exit_price_range_metrics: dict[str, Any] = Field(default_factory=dict)
    exit_price_range_partial_low_price: Decimal | None = None
    exit_price_range_partial_high_price: Decimal | None = None
    exit_price_range_strong_price: Decimal | None = None
    exit_price_range_downside_review_price: Decimal | None = None
    exit_price_range_exit_review_price: Decimal | None = None

    # --- 判定精度向上機能Phase D: Market/Sector Environment Shadow(2026-08
    # 追加、Shadow計測専用)。TOPIX/所属セクターETFの地合いをそれぞれ独立に
    # 評価した結果、および両者を統合したEnvironment Composite。StockSnapshot.
    # market_environment/sector_environment/environmentをそのままコピーした
    # ものであり、DecisionSnapshotへ記録する以外の用途では一切使わない。
    # BUY候補判定・保有判断スコア・旧売却判定・ProfitTaking判定・Entry/Exit
    # Price Range・LINE通知など既存の判定ロジックからは参照しないこと。
    # 他4スコア(historical_valuation/timing/earnings_surprise/earnings_trend)
    # と同じ5フィールドパターン(state相当の情報はmetrics内に保存し、scoreの
    # 非None判定を評価済みの識別子とする既存規約に揃える) ---
    market_score: float | None = None
    market_confidence: ConfidenceLevel | None = None
    market_coverage: float | None = None
    market_reason_codes: tuple[str, ...] = ()
    market_metrics: dict[str, Any] = Field(default_factory=dict)
    sector_score: float | None = None
    sector_confidence: ConfidenceLevel | None = None
    sector_coverage: float | None = None
    sector_reason_codes: tuple[str, ...] = ()
    sector_metrics: dict[str, Any] = Field(default_factory=dict)
    environment_score: float | None = None
    environment_confidence: ConfidenceLevel | None = None
    environment_coverage: float | None = None
    environment_reason_codes: tuple[str, ...] = ()
    environment_metrics: dict[str, Any] = Field(default_factory=dict)

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

    # --- 基準日情報の情報源区分(2026-07仕様レビュー対応)。確定日または
    # 登録済み周期(=実データ)がある場合のみ設定する。Noneは「自己推定または
    # 情報なし」を意味する(通知層での「データ提供元」との誤表示を防ぐため、
    # 推定値には付与しない)。表示ラベルへの変換は通知層(line_notification_service.py)
    # に閉じ込め、ここでは既存のSourceTypeをそのまま保持する ---
    dividend_record_date_source_type: SourceType | None = None
    benefit_record_date_source_type: SourceType | None = None

    # --- WATCH通知フォーマット刷新(2026-07仕様レビュー対応)。「保有継続を支持する
    # 要因」(counter_factors)とは別に、「直ちに利確しない理由」(まだ強い判定へ
    # 進めない理由)を明示する ---
    not_yet_action_reasons: list[str] = []

    # --- 利確判定エンジン再レビュー対応(2026-07)で追加 ---
    # 現在株価が中立/強気適正価格をどれだけ超過(または下回る)しているか。
    # 監視開始価格(閾値ベースの価格)ではなく、必ず実際の現在株価を使って算出する。
    current_price_vs_neutral_fair_value_pct: float | None = None
    current_price_vs_bull_fair_value_pct: float | None = None

    # --- 再コードレビュー対応(2026-08、上値余地マトリクスとの整合性)で追加 ---
    # 利確判定エンジン(profit_taking.py)のraw_levelを実際に押し上げた根拠の種別
    # (_RawLevelOrigin.name。例: "PRICE_POSITION"、"FAIR_VALUE_STRONG"、
    # "FUNDAMENTAL_CRITICAL_RISK"、"OTHER_CONDITIONS")。利確判定以外の
    # RecommendationTypeでは常にNone。通知直前の整合性検証
    # (recommendation_consistency_validator.py)が、reasons文字列を解析せず
    # 構造化データで「価格マトリクス由来の判定か」を判定するために使う。
    profit_taking_origin: str | None = None
    # ceiling_price(fair_value_range.bull、_fair_value_action_usable=True時のみ)。
    # 利用不能の場合はNone。
    profit_taking_ceiling_price: Decimal | None = None
    # 現在値からceiling_priceまでの上値余地(%)。ceiling_price利用不能の場合はNone。
    profit_taking_upside_pct: float | None = None

    # --- 利益保全(Profit Protection)判定(2026-08追加、要求仕様§8: 判定理由の
    # 追跡可能性)。"NONE"/"CANDIDATE"/"STRONG"/"DATA_INSUFFICIENT"のいずれか。
    profit_protection_signal: str | None = None
    # peak探索の基準日(=最終購入日、実売却があればさらにその売却日、コードレビュー
    # 対応2026-08)。判定ロジックには使わず、ATTENTION通知のevent identity
    # (line_notification_service.py、2026-08通知意図3段階化)の構成要素として使う。
    profit_protection_basis_date: dt.date | None = None
    profit_protection_peak_price: Decimal | None = None
    # peak_price_since_entryを記録した日(同値の高値が複数ある場合は最新日、
    # profit_protection.pyのcompute_profit_protection_metrics()参照)。用途は
    # profit_protection_basis_dateと同じくevent identityの構成要素。
    profit_protection_peak_date: dt.date | None = None
    profit_protection_peak_gain_pct: float | None = None
    profit_protection_current_gain_pct: float | None = None
    profit_protection_drawdown_from_peak_pct: float | None = None
    profit_protection_gain_giveback_ratio_pct: float | None = None
    # profit_protection_signal="DATA_INSUFFICIENT"の場合の具体的理由(株式分割・
    # 履歴不足等)。監査・原因調査用であり、LINE通知への表示は必須ではない
    # (コードレビュー対応2026-08、指摘2)。
    profit_protection_insufficient_reason: str | None = None

    # 保有株数・売買単位を考慮した一部売却の実行可能性
    trading_unit: int | None = None
    minimum_sellable_shares: int | None = None
    partial_sale_executable: bool | None = None
    suggested_sell_shares: int | None = None
    odd_lot_trading_available: bool | None = None
    # PARTIAL_PROFIT_TAKE成立後の売却強度・実際の売却比率(コードレビュー対応
    # 2026-08、指摘Part B)。PARTIAL_PROFIT_TAKE以外では常にNone。
    # sell_intensityはSellIntensity.value("LIGHT"/"STANDARD"/"STRONG"/
    # "VERY_STRONG")のいずれか。
    sell_intensity: str | None = None
    suggested_sell_ratio: float | None = None

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
    # Issue #22 Phase 3.5(2026-08-28): 買い側company_quality_scoreのモデル版。
    # 既存の3つのversion概念(DecisionSnapshot.model_version=Decision Enhancement
    # Layer全体 / 各Shadowスコア個別のmodel_version / 保有判断側
    # HoldingDecisionResult.scoring_model_version)とは別の、買い側品質スコア
    # 専用の第4の版概念。本フィールドを持たない既存レコードはdefaultにより
    # "v1"として読む(backfillしない。判定時点の事実を書き換えない既存方針)。
    # "v2"の書き込み開始はPhase 4(人間承認後)であり、Phase 3.5では
    # read/write compatibilityの先行導入のみ行う。
    # ロールバック互換性の制約: 本フィールドの書き込み開始後は、本フィールドを
    # 知らないPhase 3.5より前のコード(extra="forbid")では読み込みに失敗する
    # ため、Phase 3.5より前のコードは安全なrollback先ではない
    # (docs/functional_spec.md参照)。
    company_quality_score_model_version: str = "v1"
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

    # --- BUY候補裾野拡大・NEAR BUY監視・通知制御の再設計(2026-08)で追加。
    # すべてOptional/デフォルト値付きのため既存レコードの読み込みに影響しない ---
    # 5タイプ(高配当/成長/割安/連続増配/優良)分類結果。複合タイプを許容する。
    stock_types: list[StockType] = []
    # buy_action=WATCH_FOR_PRICEの銘柄のうち、NEAR BUY(積極監視・毎営業日通知
    # 対象)である場合にのみ設定する付帯属性。BuyAction自体は変更しない
    # (WatchStateService参照)。
    watch_type: WatchType | None = None
    # NEAR BUY WatchStateの表示用連続営業日数(評価不能を挟んだ場合は1へ
    # リセットされる。WatchStateService参照)。
    near_buy_consecutive_business_days: int | None = None
    # 通知が送信されなかった理由(TRADE_COOLDOWN/RESEND_SUPPRESSED/
    # DAILY_LIMIT_NEAR_BUY等)。最終送信判断時点で
    # _record_notification_outcome_audit経由の監査ログへも記録されるが、
    # Recommendation側にも残すことで単一レコードから理由を追跡できるようにする。
    notification_suppression_reason: str | None = None

    # --- 通知簡潔化・WATCH終了通知のコードレビュー対応(2026-08)で追加。
    # すべてOptionalのため既存レコードへの影響なし ---
    # WatchTransitionResult.transition_type.value(STARTED/CONTINUED/RESUMED/
    # PROMOTED_TO_BUY/ENDED)。監視に一切関与しなかった場合はNoneのまま
    # (WatchTransitionType.NONEは保存しない)。watch_type/near_buy_
    # consecutive_business_daysが「現在アクティブに監視中か」を表すのに対し、
    # こちらは当日「何が起きたか」の遷移種別を表す(PROMOTED_TO_BUY/ENDEDでは
    # watch_typeがNoneになった後もこのフィールドで遷移を追跡できる)。
    watch_transition_type: str | None = None
    # 終了/昇格時点で、それまで何営業日連続で監視していたか
    # (「4営業日監視後にBUY到達」「6日継続してPRICE_OUT_OF_RANGEで終了」の
    # 通知文言生成に使う。継続中(CONTINUED/RESUMED)の場合は
    # near_buy_consecutive_business_daysと重複するが、ENDED/PROMOTED_TO_BUY後は
    # near_buy_consecutive_business_daysがNoneになるため、このフィールドのみが
    # 「監視していた日数」を保持する)。
    watch_previous_consecutive_business_days: int | None = None
    # WatchTransitionType.ENDEDの場合の終了理由(PRICE_OUT_OF_RANGE/
    # NOT_ATTRACTIVE/STALE)。監視終了通知の生成可否判定に使う
    # (TRADE_EVENTはWatchStateService.end_for_trade_events()経由のため
    # ここには現れない)。
    watch_end_reason: str | None = None

    # 次回決算予定日の妥当性検証結果(要求仕様12節: 評価日より過去の決算日を
    # 「次回決算予定日」として表示しない)。earnings_date_rawは検証前の生値
    # (監査用、STALE_PAST_DATEの場合でもnext_earnings_dateはNoneのまま)。
    earnings_date_status: EarningsDateStatus | None = None
    earnings_date_raw: dt.date | None = None
    # --- デプロイ前対応で追加。待機通知の重複抑止キー・監査用(既存レコードは
    # 欠落するため既定None、後方互換) ---
    earnings_release_confirmation_state: EarningsReleaseConfirmationState | None = None
    earnings_decision_relevance: EarningsDecisionRelevance | None = None

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

    # 保有銘柄オーナー機能(2026-08、移行専用)。SellSignalService/
    # ProfitTakingService/HoldingDecisionServiceが生成する保有銘柄由来の
    # Recommendation(shares_at_recommendationが設定されるもの)はholding-scope
    # であり、移行時にowner/holding_idをバックフィルする。BuySignalServiceが
    # 生成するBUY候補由来のRecommendation(shares_at_recommendationがNoneの
    # もの)はstock-scopeのまま、owner/holding_idともにNoneで維持する
    # (Cross Pipeline Priority等のscope設計と整合させるため、この2種を
    # 混同してはならない)。
    owner: str | None = None
    holding_id: str | None = None

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
