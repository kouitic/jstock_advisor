"""ドメイン全体で使う列挙型。"""

from __future__ import annotations

from enum import StrEnum


class AccountType(StrEnum):
    SPECIFIC = "SPECIFIC"  # 特定口座
    NISA = "NISA"
    GENERAL = "GENERAL"  # 一般口座


class Priority(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ConfidenceLevel(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class RecommendationType(StrEnum):
    """要求仕様26節。買い判定・保有判定・売却判定を一つの列挙で表現する。"""

    BUY = "BUY"
    WATCH_BUY = "WATCH_BUY"
    HOLD = "HOLD"
    WATCH = "WATCH"
    PARTIAL_PROFIT_TAKE = "PARTIAL_PROFIT_TAKE"
    FULL_PROFIT_TAKE = "FULL_PROFIT_TAKE"
    SELL = "SELL"
    URGENT_REVIEW = "URGENT_REVIEW"

    # --- 決算直前・直後ルール(要求仕様14節)で追加 ---
    WATCH_BEFORE_EARNINGS = "WATCH_BEFORE_EARNINGS"
    PARTIAL_RISK_REDUCTION = "PARTIAL_RISK_REDUCTION"
    REVIEW_AFTER_EARNINGS = "REVIEW_AFTER_EARNINGS"

    # --- 売却判定エンジンの再設計で追加(2026-07仕様) ---
    REVIEW = "REVIEW"  # 単一の根拠のみで、SELL/URGENT_REVIEWへ進めるには不十分
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"  # 自動判定の安全条件を満たさない

    # --- 利確判定エンジン再レビュー対応(2026-07)で追加 ---
    # 決算直前は、公式確認済みの即時criticalを除き、通常の一部/全部利確提案を
    # 一旦保留して決算内容を確認してから再評価する(WATCH_BEFORE_EARNINGSとは異なり、
    # 既に利確検討の水準に達していたことを示す)。
    REVIEW_BEFORE_EARNINGS = "REVIEW_BEFORE_EARNINGS"
    # 銘柄単体の企業価値評価とは独立した、ポートフォリオ内保有比率の高さに基づく通知。
    PORTFOLIO_CONCENTRATION_REVIEW = "PORTFOLIO_CONCENTRATION_REVIEW"


class EvaluationStatus(StrEnum):
    """保有銘柄1件ごとの評価処理の結果区分(2026-07仕様レビュー対応)。

    通知が送られなかった銘柄について、正常なHOLDなのか、データ不足なのか、
    データ品質チェックでブロックされたのか、処理自体が失敗したのかを区別する。
    """

    COMPLETED = "COMPLETED"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
    DATA_QUALITY_BLOCKED = "DATA_QUALITY_BLOCKED"
    ANALYSIS_FAILED = "ANALYSIS_FAILED"


class NotificationStatus(StrEnum):
    """通知送信の結果区分(2026-07仕様レビュー対応)。"""

    SENT = "SENT"
    NOT_REQUIRED = "NOT_REQUIRED"
    DUPLICATE_SUPPRESSED = "DUPLICATE_SUPPRESSED"
    RESEND_INTERVAL_NOT_REACHED = "RESEND_INTERVAL_NOT_REACHED"
    PRICE_CHANGE_BELOW_THRESHOLD = "PRICE_CHANGE_BELOW_THRESHOLD"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
    ANALYSIS_FAILED = "ANALYSIS_FAILED"


class ProfitTakingIndustrySector(StrEnum):
    """利確判定における業種別適正価格モデルの区分(2026-07仕様レビュー対応)。

    銀行・リース金融等は一般事業会社向けのPER/PBR/配当利回りモデルをそのまま
    適用すべきではない。ただし、指定された評価要素(CET1比率・DOE等)を安定して
    取得できるデータソースが現時点で存在しないため、専用の多変量モデル自体は
    実装せず、区分の識別とHIGH信頼度禁止ゲートのみを行う(推測で補完しない方針)。
    """

    BANKING = "BANKING"
    LEASING_FINANCE = "LEASING_FINANCE"
    FOOD = "FOOD"
    CHEMICAL = "CHEMICAL"
    GAS_UTILITY = "GAS_UTILITY"
    SMALL_GROWTH = "SMALL_GROWTH"
    GENERAL = "GENERAL"
    UNKNOWN = "UNKNOWN"


class TransactionType(StrEnum):
    BUY = "BUY"
    ADDITIONAL_BUY = "ADDITIONAL_BUY"
    PARTIAL_SELL = "PARTIAL_SELL"
    FULL_SELL = "FULL_SELL"


class SkipReason(StrEnum):
    """要求仕様27節: 推奨に従わなかった場合の理由。"""

    PRICE_NOT_REACHED = "PRICE_NOT_REACHED"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    PRIORITIZED_OTHER_STOCK = "PRIORITIZED_OTHER_STOCK"
    WAITED_FOR_EARNINGS = "WAITED_FOR_EARNINGS"
    NOT_CONVINCED = "NOT_CONVINCED"
    MANUAL_JUDGMENT = "MANUAL_JUDGMENT"
    OTHER = "OTHER"


class ApprovalStatus(StrEnum):
    DRAFT = "DRAFT"
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ACTIVE = "ACTIVE"
    ROLLED_BACK = "ROLLED_BACK"


class EvaluationLabel(StrEnum):
    SUCCESS = "SUCCESS"
    ACCEPTABLE = "ACCEPTABLE"
    EARLY = "EARLY"
    LATE = "LATE"
    PRICE_TOO_LOW = "PRICE_TOO_LOW"
    PRICE_TOO_HIGH = "PRICE_TOO_HIGH"
    PROFIT_TAKE_TOO_EARLY = "PROFIT_TAKE_TOO_EARLY"
    PROFIT_TAKE_TOO_LATE = "PROFIT_TAKE_TOO_LATE"
    SELL_TOO_SENSITIVE = "SELL_TOO_SENSITIVE"
    RISK_UNDERESTIMATED = "RISK_UNDERESTIMATED"
    DATA_ISSUE = "DATA_ISSUE"
    INCONCLUSIVE = "INCONCLUSIVE"


class NotificationType(StrEnum):
    DAILY_BUY_CANDIDATES = "DAILY_BUY_CANDIDATES"
    WATCHLIST_BUY_SIGNAL = "WATCHLIST_BUY_SIGNAL"
    PROFIT_TAKING_SIGNAL = "PROFIT_TAKING_SIGNAL"
    SELL_SIGNAL = "SELL_SIGNAL"
    IMPORTANT_DISCLOSURE = "IMPORTANT_DISCLOSURE"
    DATA_ERROR = "DATA_ERROR"
    DATA_QUALITY_ALERT = "DATA_QUALITY_ALERT"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    BATCH_SUMMARY = "BATCH_SUMMARY"
    WEEKLY_REVIEW = "WEEKLY_REVIEW"
    MONTHLY_REVIEW = "MONTHLY_REVIEW"
    QUARTERLY_LOGIC_REVIEW = "QUARTERLY_LOGIC_REVIEW"
    OUTLIER_REVIEW = "OUTLIER_REVIEW"
    LOGIC_CHANGE_PROPOSAL = "LOGIC_CHANGE_PROPOSAL"


class CorporateActionType(StrEnum):
    """要求仕様: 企業行動調整サービスが扱う事象種別。"""

    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    FREE_ALLOTMENT = "FREE_ALLOTMENT"  # 無償割当
    SPINOFF = "SPINOFF"
    TICKER_CHANGE = "TICKER_CHANGE"
    MERGER = "MERGER"
    DELISTING = "DELISTING"
    DIVIDEND_BASIS_CHANGE = "DIVIDEND_BASIS_CHANGE"


class SourceType(StrEnum):
    """データソースの優先順位付け(要求仕様15節)。数値の小さいものほど優先度が高い。"""

    COMPANY_IR = "COMPANY_IR"
    TDNET_EDINET = "TDNET_EDINET"
    EXCHANGE = "EXCHANGE"
    CONTRACTED_PROVIDER = "CONTRACTED_PROVIDER"
    SECONDARY = "SECONDARY"
    MANUAL_REGISTRY = "MANUAL_REGISTRY"
    OTHER_WEB = "OTHER_WEB"


_SOURCE_TYPE_PRIORITY = {
    SourceType.COMPANY_IR: 1,
    SourceType.TDNET_EDINET: 2,
    SourceType.EXCHANGE: 3,
    SourceType.CONTRACTED_PROVIDER: 4,
    SourceType.SECONDARY: 5,
    SourceType.MANUAL_REGISTRY: 5,
    SourceType.OTHER_WEB: 6,
}


def source_type_priority(source_type: SourceType) -> int:
    """数値が小さいほど優先度が高い(要求仕様15節の順序)。"""
    return _SOURCE_TYPE_PRIORITY[source_type]


class RecordDateUnknownReason(StrEnum):
    """権利確定日等が取得できない理由(要求仕様16節: 単に「不明」とだけ通知しない)。"""

    SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
    PARSE_ERROR = "PARSE_ERROR"
    CORPORATE_ACTION_UNRESOLVED = "CORPORATE_ACTION_UNRESOLVED"
    DATA_PROVIDER_MISSING = "DATA_PROVIDER_MISSING"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class DividendComparisonOutcome(StrEnum):
    """減配判定(要求仕様6節)。分割前後を未調整で比較した判定は禁止。"""

    ACTUAL_DIVIDEND_CUT = "ACTUAL_DIVIDEND_CUT"
    FORECAST_DIVIDEND_CUT = "FORECAST_DIVIDEND_CUT"
    SPLIT_ADJUSTMENT_ONLY = "SPLIT_ADJUSTMENT_ONLY"
    DIVIDEND_MAINTAINED = "DIVIDEND_MAINTAINED"
    DIVIDEND_INCREASE = "DIVIDEND_INCREASE"
    COMPARISON_NOT_POSSIBLE = "COMPARISON_NOT_POSSIBLE"


class StockType(StrEnum):
    """銘柄タイプ分類(要求仕様7節)。複合タイプはlist[StockType]で表現する。"""

    INCOME = "INCOME"
    GROWTH = "GROWTH"
    VALUE = "VALUE"
    CYCLICAL = "CYCLICAL"
    DEFENSIVE = "DEFENSIVE"
    TURNAROUND = "TURNAROUND"
    ASSET_PLAY = "ASSET_PLAY"
    EVENT_DRIVEN = "EVENT_DRIVEN"


class PriceBasisType(StrEnum):
    """価格フィールドがどの算定軸から導出されたかを明示する(利確判定レビュー対応)。

    異なる軸の価格(適正価格基準・取得価格基準・配当利回り基準等)を、
    根拠を示さずに同列の「売却価格」として並べない。
    """

    FAIR_VALUE_THRESHOLD = "FAIR_VALUE_THRESHOLD"
    PURCHASE_PRICE_RETURN_TARGET = "PURCHASE_PRICE_RETURN_TARGET"
    DIVIDEND_YIELD_THRESHOLD = "DIVIDEND_YIELD_THRESHOLD"
    TOTAL_YIELD_THRESHOLD = "TOTAL_YIELD_THRESHOLD"
    TECHNICAL_PRICE_LEVEL = "TECHNICAL_PRICE_LEVEL"
    USER_DEFINED_TARGET = "USER_DEFINED_TARGET"


class PriceFieldBasis(StrEnum):
    """価格フィールドが現在値と一致する場合の意味を明示する(要求仕様11節)。"""

    TARGET_PRICE = "TARGET_PRICE"
    IMMEDIATE_EXECUTION_REFERENCE = "IMMEDIATE_EXECUTION_REFERENCE"
    MONITORING_ONLY_NOT_A_SELL_TARGET = "MONITORING_ONLY_NOT_A_SELL_TARGET"


class JudgmentStrength(StrEnum):
    """推奨判定の安全制約(要求仕様22節)。強度順にINFO<...<URGENT_REVIEW。"""

    INFO = "INFO"
    WATCH = "WATCH"
    REVIEW = "REVIEW"
    PARTIAL_ACTION = "PARTIAL_ACTION"
    FULL_ACTION = "FULL_ACTION"
    URGENT_REVIEW = "URGENT_REVIEW"


_JUDGMENT_STRENGTH_ORDER = {
    JudgmentStrength.INFO: 0,
    JudgmentStrength.WATCH: 1,
    JudgmentStrength.REVIEW: 2,
    JudgmentStrength.PARTIAL_ACTION: 3,
    JudgmentStrength.FULL_ACTION: 4,
    JudgmentStrength.URGENT_REVIEW: 5,
}


def judgment_strength_rank(strength: JudgmentStrength) -> int:
    return _JUDGMENT_STRENGTH_ORDER[strength]


class TimingAction(StrEnum):
    """ファンダメンタル評価と分離したタイミング判断(要求仕様9節)。"""

    NEUTRAL = "NEUTRAL"
    WAIT_UPTREND_CONTINUES = "WAIT_UPTREND_CONTINUES"
    PROCEED_NO_TIMING_SIGNAL = "PROCEED_NO_TIMING_SIGNAL"
    ACCELERATE_DOWNTREND_CONFIRMED = "ACCELERATE_DOWNTREND_CONFIRMED"


class TrendClassification(StrEnum):
    """モメンタム・トレンド層の分類(要求仕様9節)。"""

    STRONG_UPTREND = "STRONG_UPTREND"
    UPTREND = "UPTREND"
    NEUTRAL = "NEUTRAL"
    DOWNTREND = "DOWNTREND"
    STRONG_DOWNTREND = "STRONG_DOWNTREND"


class EarningsWindowStatus(StrEnum):
    """決算直前・直後ルール(要求仕様14節)。

    RECENTLY_REPORTEDは実際の決算発表日ではなく、取得できた直近四半期の
    期末日(fiscal_period_end)を代理指標として用いた近似判定である
    (yfinance/EDINETいずれも決算発表日そのものは提供しないため)。
    """

    NONE = "NONE"
    APPROACHING_EARNINGS = "APPROACHING_EARNINGS"
    RECENTLY_REPORTED = "RECENTLY_REPORTED"


class TriggerStatus(StrEnum):
    """売却ルール1件ごとの該当有無を表現する(2026-07仕様§3、レビュー対応で拡張)。

    データ不足はNOT_TRIGGERED(Falseと同義に扱う)ではなくNOT_EVALUATEDとする。
    「推測で補完しない」という既存方針を売却ルールにも徹底するための区分。
    SUSPECTEDは、一次情報未確認の推測のみで該当が疑われる状態(例: yfinanceの
    予想配当0のみを根拠とする無配転落疑い)。TRIGGEREDとは異なり、major/critical
    件数・独立根拠グループ数のいずれにも算入しない(参考情報としてのみ保持する)。
    """

    TRIGGERED = "TRIGGERED"
    NOT_TRIGGERED = "NOT_TRIGGERED"
    NOT_EVALUATED = "NOT_EVALUATED"
    SUSPECTED = "SUSPECTED"


class EvidenceGroup(StrEnum):
    """独立根拠グループ(2026-07仕様§5)。

    同一の根本的な財務変化に由来する複数ルールは、同じグループに属する限り
    1件の独立根拠としてしか数えない(例: 自己資本比率低下+有利子負債急増は
    どちらもBALANCE_SHEETグループであり、合わせて1件)。
    """

    DIVIDEND = "DIVIDEND"
    EARNINGS = "EARNINGS"
    CASHFLOW = "CASHFLOW"
    BALANCE_SHEET = "BALANCE_SHEET"
    REGULATORY_CAPITAL = "REGULATORY_CAPITAL"
    GOVERNANCE = "GOVERNANCE"
    LISTING = "LISTING"
    SHAREHOLDER_BENEFIT = "SHAREHOLDER_BENEFIT"
    INVESTMENT_PREMISE = "INVESTMENT_PREMISE"


class FinancialIndustryCategory(StrEnum):
    """金融業の細分類(2026-07仕様§2)。一般事業会社向けの自己資本比率・D/Eレシオ等の
    財務健全性指標は、業態がまったく異なるこれらの業種には適用しない。
    """

    BANKING = "BANKING"
    INSURANCE = "INSURANCE"
    SECURITIES = "SECURITIES"
    OTHER_FINANCIAL = "OTHER_FINANCIAL"


class IndustryClassification(StrEnum):
    """業種分類の三値(2026-07仕様レビュー対応: 業種不明と一般事業会社を区別する)。

    sector/industryが欠損・空文字の場合はGENERAL_CORPORATEにフォールバックせず
    UNKNOWNとする。一般事業会社向けの財務健全性ルールは、GENERAL_CORPORATEと
    明確に判定できた場合にのみ適用する。
    """

    GENERAL_CORPORATE = "GENERAL_CORPORATE"
    FINANCIAL = "FINANCIAL"
    UNKNOWN = "UNKNOWN"


class PeriodType(StrEnum):
    """財務期間の種別(2026-07仕様レビュー対応)。異なる種別同士は比較しない。"""

    QUARTER = "QUARTER"
    YTD = "YTD"
    TTM = "TTM"
    ANNUAL = "ANNUAL"


class DisclosureRiskConfirmationLevel(StrEnum):
    """開示リスクキーワード検出の重大性確認段階(2026-07仕様レビュー対応)。

    キーワード一致のみでは、実際に企業価値へ重大な影響がある事象かどうか
    確認できない。重大事象を示す語(決算訂正・監査意見への影響等)が
    本文中に別途確認できた場合のみMATERIAL_EVENT_CONFIRMEDとする。
    """

    RISK_KEYWORD_DETECTED = "RISK_KEYWORD_DETECTED"
    MATERIAL_EVENT_CONFIRMED = "MATERIAL_EVENT_CONFIRMED"


class BenefitUtilityCategory(StrEnum):
    """要求仕様7節: 株主優待評価額の利用可能性係数カテゴリ。"""

    CASH_EQUIVALENT = "CASH_EQUIVALENT"
    VERSATILE_POINT = "VERSATILE_POINT"
    IN_HOUSE_SERVICE = "IN_HOUSE_SERVICE"
    IN_HOUSE_PRODUCT = "IN_HOUSE_PRODUCT"
    DISCOUNT_VOUCHER = "DISCOUNT_VOUCHER"
    LOTTERY_OR_COMMEMORATIVE = "LOTTERY_OR_COMMEMORATIVE"


class BuyAction(StrEnum):
    """購入対象判定の正本(2026-07 BUYパイプライン再設計)。

    「企業として投資候補になり得るか(company_quality_score)」と「現在の株価で
    実際に購入すべきか」を分離するための判定区分。旧`recommended: bool`は
    このBuyActionから導出される派生値(プロパティ)とし、直接更新しない。
    """

    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    SMALL_ENTRY = "SMALL_ENTRY"
    WATCH_FOR_PRICE = "WATCH_FOR_PRICE"
    WATCH_BEFORE_EARNINGS = "WATCH_BEFORE_EARNINGS"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    NOT_ATTRACTIVE = "NOT_ATTRACTIVE"
    EXCLUDED = "EXCLUDED"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"


_BUY_ACTION_LABELS: dict[BuyAction, str] = {
    BuyAction.STRONG_BUY: "積極購入候補",
    BuyAction.BUY: "購入候補",
    BuyAction.SMALL_ENTRY: "打診購入候補",
    BuyAction.WATCH_FOR_PRICE: "監視継続(価格待ち)",
    BuyAction.WATCH_BEFORE_EARNINGS: "監視継続(決算待ち)",
    BuyAction.MANUAL_REVIEW: "要確認",
    BuyAction.NOT_ATTRACTIVE: "購入見送り",
    BuyAction.EXCLUDED: "対象外",
    BuyAction.DATA_INSUFFICIENT: "データ不足",
}

# 購入候補ランキングに含めるBuyAction(価格条件を満たしている状態)。
BUY_FAMILY_ACTIONS = frozenset(
    {BuyAction.STRONG_BUY, BuyAction.BUY, BuyAction.SMALL_ENTRY}
)
# 価格待ちランキングに含めるBuyAction。
WATCH_FAMILY_ACTIONS = frozenset(
    {BuyAction.WATCH_FOR_PRICE, BuyAction.WATCH_BEFORE_EARNINGS}
)


def buy_action_label(action: BuyAction) -> str:
    return _BUY_ACTION_LABELS[action]


class BuyIndustrySector(StrEnum):
    """購入判断における業種別適正価格モデルの区分(2026-07 BUYパイプライン再設計)。

    利確判定用の`ProfitTakingIndustrySector`とはメンバー構成・用途が異なるため
    統合せず独立させる(統合すると利確側の既存キーワード判定・ゲートを壊す
    リスクがあるため)。専用の多変量モデル自体は未実装であり、区分の識別と
    信頼度HIGH禁止ゲート・安全余裕率加算のみを行う(推測で補完しない方針)。
    """

    BANK = "BANK"
    LEASE_FINANCE = "LEASE_FINANCE"
    PHARMACEUTICAL = "PHARMACEUTICAL"
    AUTOMOTIVE_PARTS = "AUTOMOTIVE_PARTS"
    CYCLICAL_MATERIALS = "CYCLICAL_MATERIALS"
    UTILITY = "UTILITY"
    FOOD = "FOOD"
    GENERAL_MANUFACTURING = "GENERAL_MANUFACTURING"
    SMALL_GROWTH = "SMALL_GROWTH"
    GENERAL = "GENERAL"
    UNKNOWN = "UNKNOWN"


class MarginRiskCategory(StrEnum):
    """安全余裕率リスク加算のカテゴリ(2026-07 BUYパイプライン第2次修正)。

    個別のリスクコード(industry_model_not_appliedやcyclical_industry等)を
    単純合算すると、実質的に同じリスクを複数回加算してしまう
    (例: 自動車部品業種ではindustry_model_not_applied/cyclical_industry/
    major_customer_dependencyが常に同時発生する)。カテゴリ内は最大値のみを
    採用し、カテゴリ間のみ合算することで二重加点を防ぐ。
    """

    VALUATION_UNCERTAINTY = "VALUATION_UNCERTAINTY"
    INDUSTRY_AND_BUSINESS = "INDUSTRY_AND_BUSINESS"
    EARNINGS_QUALITY = "EARNINGS_QUALITY"
    EVENT_TIMING = "EVENT_TIMING"
    DATA_QUALITY = "DATA_QUALITY"
    LIQUIDITY = "LIQUIDITY"


class BuyPriceReliability(StrEnum):
    """買付価格3段階の信頼性区分(2026-07 BUYパイプライン第2次修正)。

    安全余裕率が上限に張り付く、適正価格手法間のバラつきが大きい、
    有効な算出方式が少ない等、機械的に算出した買付価格をそのまま
    購入判断へ使ってよいか怪しい場合はLOWとし、BUY系判定を禁止する
    (無理に低い買付価格を提示するより、要確認・監視継続とする)。
    """

    OK = "OK"
    LOW = "LOW"


class EarningsDateStatus(StrEnum):
    """次回決算予定日の妥当性区分(2026-07 BUYパイプライン第2次修正)。

    yfinance等のデータ提供元が返す決算日は、更新遅延により評価日より
    過去の日付になっていることがある。過去日をそのまま「次回決算予定日」
    として通知・判定に使わないよう、取得値と検証結果を分けて保持する。
    """

    CONFIRMED = "CONFIRMED"
    STALE_PAST_DATE = "STALE_PAST_DATE"
    UNAVAILABLE = "UNAVAILABLE"


class NotificationContext(StrEnum):
    """evaluate_notification_statusの呼び出し元コンテキスト(BUYパイプライン第3次修正)。

    データ品質アラートでrequires_manual_review=Trueとなった場合、通常は
    notify_manual_review_required()で「要手動確認」LINEを即時送信する安全弁が
    働く。しかしBUY候補バッチ(BUY_CANDIDATE_BATCH)では「今日、現在の株価で
    実際に購入条件を満たした銘柄だけ」を通知する方針のため、この安全弁を
    LINE送信させず、data_quality_blocked=Trueとして黙って除外する
    (異常は監査ログには引き続き記録される)。SELL/保有銘柄レビュー系
    (HOLDING_REVIEW)・その他(DEFAULT)は従来通りLINE送信する。
    """

    DEFAULT = "DEFAULT"
    BUY_CANDIDATE_BATCH = "BUY_CANDIDATE_BATCH"
    HOLDING_REVIEW = "HOLDING_REVIEW"
