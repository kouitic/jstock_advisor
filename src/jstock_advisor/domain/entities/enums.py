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


class BenefitUtilityCategory(StrEnum):
    """要求仕様7節: 株主優待評価額の利用可能性係数カテゴリ。"""

    CASH_EQUIVALENT = "CASH_EQUIVALENT"
    VERSATILE_POINT = "VERSATILE_POINT"
    IN_HOUSE_SERVICE = "IN_HOUSE_SERVICE"
    IN_HOUSE_PRODUCT = "IN_HOUSE_PRODUCT"
    DISCOUNT_VOUCHER = "DISCOUNT_VOUCHER"
    LOTTERY_OR_COMMEMORATIVE = "LOTTERY_OR_COMMEMORATIVE"
