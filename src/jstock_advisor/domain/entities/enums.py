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


class WatchlistRegistrationSource(StrEnum):
    """ウォッチリスト項目がどのように登録されたか(ウォッチリスト自動追加機能で追加)。"""

    MANUAL = "MANUAL"
    AUTO_SCREENING = "AUTO_SCREENING"


class ConfidenceLevel(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ExecutionMode(StrEnum):
    """通知検証モード機能(2026-08追加)。Lambdaイベントのexecution_modeキーで指定する。

    NORMAL: 通常運用(既定)。VALIDATION: 本番データ・本番ロジックを使いつつ
    再送防止のみを無効化し、通常運用の永続履歴を汚さずに実LINE送信で通知文面を
    確認できる検証専用モード(AWS Console/CLIからの明示的な手動起動のみを想定)。
    """

    NORMAL = "NORMAL"
    VALIDATION = "VALIDATION"


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

    # --- 保有判断スコア方式への移行(2026-08仕様)で追加 ---
    # SELL/URGENT_REVIEW/REVIEW(個別悪化シグナル方式)を段階的に置き換える。
    # 移行期間中は新旧の値が両方発行され得るため、SELL/URGENT_REVIEWは廃止せず残す。
    SELL_CONSIDERATION = "SELL_CONSIDERATION"  # 保有判断スコアに基づく「売却を検討」
    STRONG_SELL_CONSIDERATION = "STRONG_SELL_CONSIDERATION"  # 「全部売却を強く検討」
    URGENT_HOLDING_REVIEW = "URGENT_HOLDING_REVIEW"  # ハードゲート発動「重大リスクのため緊急確認」


# 売却系(投資前提悪化)の通知として扱うべきRecommendationTypeの唯一の判定ソース。
# 通知マッピング・整合性検証・買い増しゲート等、複数箇所が個別にenum値を列挙する
# 代わりにis_sell_like()を呼ぶことで、判定区分が増減しても呼び出し側の変更を不要にする
# (BUY_FAMILY_ACTIONSと同じパターン)。旧3値(SELL/URGENT_REVIEW/REVIEW)は新方式への
# 移行完了後も、過去データの再判定・監査のため削除しない。
SELL_LIKE_RECOMMENDATION_TYPES = frozenset(
    {
        RecommendationType.SELL,
        RecommendationType.URGENT_REVIEW,
        RecommendationType.REVIEW,
        RecommendationType.SELL_CONSIDERATION,
        RecommendationType.STRONG_SELL_CONSIDERATION,
        RecommendationType.URGENT_HOLDING_REVIEW,
    }
)


def is_sell_like(recommendation_type: RecommendationType) -> bool:
    return recommendation_type in SELL_LIKE_RECOMMENDATION_TYPES


# 横断整合性レビュー対応(2026-08、指摘3): SELL_LIKE_RECOMMENDATION_TYPESの
# サブセットのうち、「全部売却」に相当するRecommendationTypeの唯一の判定
# ソース。従来はrecommendation_adapter.py(個別LINE通知本文の「全部売却検討」
# ラベル付け)だけがこの2値を私的なfrozensetとして持っており、
# holdings_watchlist_handler.py(1日のまとめ通知の集計)は別途独自に
# FULL_PROFIT_TAKEのみをfull_sellバケットへ計上していたため、
# STRONG_SELL_CONSIDERATIONが「個別本文では全部売却検討」「まとめ通知では
# 通常売却」という矛盾した扱いになっていた。両呼び出し元は必ずこの定数
# (またはis_full_sell_like())を経由すること。
FULL_SELL_RECOMMENDATION_TYPES = frozenset(
    {
        RecommendationType.FULL_PROFIT_TAKE,
        RecommendationType.STRONG_SELL_CONSIDERATION,
    }
)


def is_full_sell_like(recommendation_type: RecommendationType) -> bool:
    return recommendation_type in FULL_SELL_RECOMMENDATION_TYPES


# 「重大リスクのため緊急確認」相当のRecommendationType(BUY候補裾野拡大機能
# 2026-08で新設)。ハードゲート発動時のURGENT_HOLDING_REVIEW(新方式、
# holding_decision_notification_builder.py)と、旧SellSignalService由来の
# URGENT_REVIEW(移行期間中も併存)の両方を指す。売買クールダウン・
# TradeDetection未確定時の抑止(fail-closed)のいずれも、この判定だけは
# 貫通させる(要求仕様: 重大リスク > 売買クールダウン > 通常の再送防止)。
CRITICAL_RISK_RECOMMENDATION_TYPES = frozenset(
    {
        RecommendationType.URGENT_HOLDING_REVIEW,
        RecommendationType.URGENT_REVIEW,
    }
)


def is_critical_risk(recommendation_type: RecommendationType) -> bool:
    return recommendation_type in CRITICAL_RISK_RECOMMENDATION_TYPES


class BacktestRecommendationSource(StrEnum):
    """backtest/compareのhistory replayが、あるRecommendationTypeを新旧どちらの
    エンジン由来として扱うかの分類(コードレビュー対応)。

    分類はrecommendation_type単独では決定できないため、実装箇所をgrepで全数確認した
    生成元(docs/functional_spec.md該当節参照)に基づき、3集合すべてを明示的に列挙する
    (差集合は使わない)。未知のRecommendationTypeを暗黙にLEGACY_SELLへ含めないための
    安全側設計であり、3集合いずれにも属さない新規メンバーは自動的にEXCLUDEDとなる。
    """

    LEGACY_SELL = "LEGACY_SELL"
    HOLDING_DECISION = "HOLDING_DECISION"
    EXCLUDED = "EXCLUDED"


# 旧SellSignalService(domain/signals/sell_signal.py)のみが生成することをgrepで
# 全数確認済み(他に代入箇所なし)。
_LEGACY_SELL_RECOMMENDATION_TYPES = frozenset(
    {
        RecommendationType.SELL,
        RecommendationType.URGENT_REVIEW,
        RecommendationType.REVIEW,
    }
)
# 新holding_decision_notification_builder.pyのみが生成することをgrepで全数確認済み
# (他に代入箇所なし)。
_HOLDING_DECISION_RECOMMENDATION_TYPES = frozenset(
    {
        RecommendationType.SELL_CONSIDERATION,
        RecommendationType.STRONG_SELL_CONSIDERATION,
        RecommendationType.URGENT_HOLDING_REVIEW,
    }
)
# 売却方式比較に無関係(BUY候補・利確・ポートフォリオ集中リスク)、または
# MANUAL_REVIEW_REQUIREDのようにどのサービスも生成していない値。
_EXCLUDED_RECOMMENDATION_TYPES = frozenset(
    {
        RecommendationType.BUY,
        RecommendationType.WATCH_BUY,
        RecommendationType.HOLD,
        RecommendationType.WATCH,
        RecommendationType.PARTIAL_PROFIT_TAKE,
        RecommendationType.FULL_PROFIT_TAKE,
        RecommendationType.WATCH_BEFORE_EARNINGS,
        RecommendationType.PARTIAL_RISK_REDUCTION,
        RecommendationType.REVIEW_AFTER_EARNINGS,
        RecommendationType.REVIEW_BEFORE_EARNINGS,
        RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW,
        RecommendationType.MANUAL_REVIEW_REQUIRED,
    }
)


def classify_recommendation_source(
    recommendation_type: RecommendationType,
) -> BacktestRecommendationSource:
    if recommendation_type in _LEGACY_SELL_RECOMMENDATION_TYPES:
        return BacktestRecommendationSource.LEGACY_SELL
    if recommendation_type in _HOLDING_DECISION_RECOMMENDATION_TYPES:
        return BacktestRecommendationSource.HOLDING_DECISION
    return BacktestRecommendationSource.EXCLUDED


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
    # kill switch(notification_enabled=False)により送信を見送った場合(コードレビュー対応)。
    # 判定・Recommendation保存自体は継続するが、LINE送信のみを止めたことを示す。
    KILL_SWITCH_SUPPRESSED = "KILL_SWITCH_SUPPRESSED"


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


class DecisionType(StrEnum):
    """判定精度向上機能(Phase A)。DecisionSnapshotがどのパイプラインで生成されたかの
    粗い分類。実際のRecommendationType(既存)はDecisionSnapshot.existing_actionに
    別途保持する。"""

    BUY = "BUY"
    SELL = "SELL"
    HOLDING_DECISION = "HOLDING_DECISION"
    PROFIT_TAKING = "PROFIT_TAKING"


class ValuationBasis(StrEnum):
    """PER/PBR等のバリュエーション倍率が、どのEPS/BPSを分母として算出されたか
    (判定精度向上機能Phase B: Historical Valuation Scoreのコードレビュー対応)。

    TRAILING = 実績値(既に確定した決算数値)ベース。
    FORWARD = 予想値(アナリスト予想等)ベース。
    UNKNOWN = 由来不明・算出不可(この場合は他のbasisと比較しない)。

    現在値と過去値を比較する際、basisが一致する場合のみ意味のある比較とみなす
    (forward基準の現在PERとtrailing基準の過去PERを単純比較しない)。
    """

    TRAILING = "TRAILING"
    FORWARD = "FORWARD"
    UNKNOWN = "UNKNOWN"


class HistoricalValuationEvaluationState(StrEnum):
    """Historical Valuation Scoreが実際に算出されたか、対象外か、算出不可
    だったかの区分(判定精度向上機能Phase Bコードレビュー対応)。"""

    EVALUATED = "EVALUATED"
    NOT_EVALUATED = "NOT_EVALUATED"
    # 将来、金融業等PER/PBRそのものが意味を持ちにくい銘柄区分向けに使う想定
    # (現時点では判定ロジックが無いため未使用)。
    NOT_APPLICABLE = "NOT_APPLICABLE"


class HistoricalValuationCategory(StrEnum):
    """Historical Valuation Scoreを人間が読みやすいカテゴリへ丸めたもの
    (Shadow記録専用、BUY/SELL等の判定ロジックには使わない)。"""

    HISTORICALLY_VERY_CHEAP = "HISTORICALLY_VERY_CHEAP"
    CHEAP = "CHEAP"
    NORMAL = "NORMAL"
    EXPENSIVE = "EXPENSIVE"
    VERY_EXPENSIVE = "VERY_EXPENSIVE"


class TimingScoreEvaluationState(StrEnum):
    """Timing Scoreが実際に算出されたか、データ不足で算出不可だったかの区分
    (判定精度向上機能Phase B第二弾)。"""

    EVALUATED = "EVALUATED"
    NOT_EVALUATED = "NOT_EVALUATED"


class TimingScoreCategory(StrEnum):
    """Timing Scoreを人間が読みやすいカテゴリへ丸めたもの
    (Shadow記録専用、BUY/SELL等の判定ロジックには使わない)。"""

    STRONG_TAILWIND = "STRONG_TAILWIND"
    TAILWIND = "TAILWIND"
    NEUTRAL = "NEUTRAL"
    HEADWIND = "HEADWIND"
    STRONG_HEADWIND = "STRONG_HEADWIND"


class EarningsSurpriseEvaluationState(StrEnum):
    """Earnings Surprise Scoreが実際に算出されたか、対象外か、算出不可
    だったかの区分(判定精度向上機能Phase C)。"""

    EVALUATED = "EVALUATED"
    NOT_EVALUATED = "NOT_EVALUATED"
    # 決算予定日を未経過等、そもそも評価対象外の場合(データ不足による
    # NOT_EVALUATEDとは区別する。HistoricalValuationEvaluationStateと同じ
    # 3区分パターン)。
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EarningsSurpriseCategory(StrEnum):
    """Earnings Surprise Scoreを人間が読みやすいカテゴリへ丸めたもの
    (Shadow記録専用、BUY/SELL等の判定ロジックには使わない)。"""

    STRONG_POSITIVE_SURPRISE = "STRONG_POSITIVE_SURPRISE"
    POSITIVE_SURPRISE = "POSITIVE_SURPRISE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE_SURPRISE = "NEGATIVE_SURPRISE"
    STRONG_NEGATIVE_SURPRISE = "STRONG_NEGATIVE_SURPRISE"


class EarningsTrendEvaluationState(StrEnum):
    """Earnings Trend Scoreが実際に算出されたか、対象外か、算出不可だった
    かの区分(判定精度向上機能Phase C)。"""

    EVALUATED = "EVALUATED"
    NOT_EVALUATED = "NOT_EVALUATED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EarningsTrendCategory(StrEnum):
    """Earnings Trend Scoreを人間が読みやすいカテゴリへ丸めたもの
    (Shadow記録専用、BUY/SELL等の判定ロジックには使わない)。"""

    STRONG_IMPROVING = "STRONG_IMPROVING"
    IMPROVING = "IMPROVING"
    STABLE = "STABLE"
    DETERIORATING = "DETERIORATING"
    STRONG_DETERIORATING = "STRONG_DETERIORATING"


class PriceRangeEvaluationState(StrEnum):
    """Entry/Exit Price Range(判定精度向上機能次フェーズSTEP2)が実際に
    算出されたか、算出不可だったか、対象外だったかの区分。Entry/Exitには
    他の4スコアのような単一のscoreが無いため、DecisionPerformance等が
    「評価済みレコードだけを安全に抽出する」ための主要な識別子となる
    (Recommendation/DecisionSnapshotへ明示フィールドとして保存する)。"""

    EVALUATED = "EVALUATED"
    NOT_EVALUATED = "NOT_EVALUATED"
    # 将来、決算反映待ち等の理由でPrice Range自体を意図的に見送る場合に
    # 使う想定で予約(v1では未使用でも他4スコアと同じ3区分構造に揃える)。
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EnvironmentCategory(StrEnum):
    """Market/Sector/Environment Composite Score(判定精度向上機能Phase D)を
    人間が読みやすいカテゴリへ丸めたもの(Shadow記録専用、BUY/SELL等の判定
    ロジックには使わない)。TimingScoreCategoryとラベルは同型だが、Market/
    Sector/Compositeの3つは「環境の追い風/逆風度」という同一概念を異なる
    スコープ(市場全体/セクター/統合)へ適用したものであるため、共有Enumと
    して1つにまとめ、不必要な3重複を避ける(個別銘柄のエントリー適性を
    表すTimingScoreCategoryとは意味的に別概念のため、そちらとは共有しない)。"""

    STRONG_TAILWIND = "STRONG_TAILWIND"
    TAILWIND = "TAILWIND"
    NEUTRAL = "NEUTRAL"
    HEADWIND = "HEADWIND"
    STRONG_HEADWIND = "STRONG_HEADWIND"


class MarketEnvironmentEvaluationState(StrEnum):
    """Market Environment Score(判定精度向上機能Phase D)が実際に算出された
    か、データ不足で算出不可だったかの区分。TOPIXは常に評価対象のため
    NOT_APPLICABLEは持たない。"""

    EVALUATED = "EVALUATED"
    NOT_EVALUATED = "NOT_EVALUATED"


class SectorEnvironmentEvaluationState(StrEnum):
    """Sector Environment Score(判定精度向上機能Phase D)が実際に算出された
    か、データ不足で算出不可だったか、対象外だったかの区分。"""

    EVALUATED = "EVALUATED"
    NOT_EVALUATED = "NOT_EVALUATED"
    # config.momentum.sector_etf_mapに対応ETFが無い業種(0点/NEUTRAL扱いには
    # せず、明示的に対象外として区別する)。
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EnvironmentEvaluationState(StrEnum):
    """Environment Composite Score(判定精度向上機能Phase D)が実際に算出さ
    れたか、算出不可だったかの区分。Marketが必須バックボーンのため
    NOT_APPLICABLEは持たない。"""

    EVALUATED = "EVALUATED"
    NOT_EVALUATED = "NOT_EVALUATED"


class ImprovementPriority(StrEnum):
    """週次改善レビュー(振り返り機能改修)の改善候補優先度。"""

    A = "A"
    B = "B"
    C = "C"
    NONE = "NONE"


class ImprovementAction(StrEnum):
    """週次改善レビューが改善候補へ付与する推奨アクション区分。"""

    ADJUST_THRESHOLD = "ADJUST_THRESHOLD"
    REVIEW_LOGIC = "REVIEW_LOGIC"
    # WATCH/REVIEW等、determine_evaluation_label()が無条件にINCONCLUSIVEを返す
    # (=自動評価の対象外)種別向け。「成績が悪い」ではなく「評価定義そのものが
    # 未整備」であることを表す。
    DEFINE_EVALUATION_CRITERIA = "DEFINE_EVALUATION_CRITERIA"
    INVESTIGATE_SEGMENT = "INVESTIGATE_SEGMENT"


class ImprovementTaskStatus(StrEnum):
    """GitHub Issue化フローの状態(振り返り機能改修)。

    SKIPPED_NOT_CONFIGUREDは issue_creation_enabled=false による正常なスキップ、
    CONFIGURATION_ERRORはissue_creation_enabled=trueなのにGitHub認証情報が
    不備・取得失敗している異常状態であり、両者を明確に区別する(前者は運用
    エラー通知を送らない、後者は送る)。ISSUE_CREATINGは新規Issue作成中の原子的な
    claim状態を表し、claim期限切れ(stale)を検出した場合もGitHub側の実在確認を
    先に行ってから復旧・再claimする(batch_tracker.pyと同様の排他制御パターン)。
    """

    CANDIDATE = "CANDIDATE"
    SKIPPED_NOT_CONFIGURED = "SKIPPED_NOT_CONFIGURED"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    ISSUE_CREATING = "ISSUE_CREATING"
    ISSUE_CREATED = "ISSUE_CREATED"
    ISSUE_CREATION_FAILED = "ISSUE_CREATION_FAILED"


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
    WATCHLIST_AUTO_ADDITION = "WATCHLIST_AUTO_ADDITION"
    # NEAR BUY監視の終了通知(コードレビュー対応2026-08、§3)。
    WATCH_END = "WATCH_END"


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


class DividendValidationStatus(StrEnum):
    """配当実績クロスバリデーション(yfinance×EDINET)の検証状態(配当データ
    クロスバリデーション根本修正)。

    DISCREPANCYは意図的に定義しない。真の乖離(共通のREPORTED決算期で説明不能な
    乖離)が生じた場合はDividendInfo全体をNoneで返す既存仕様を維持するため、
    「返却されたDividendInfoが取りうる状態」としてDISCREPANCYを含めると、
    呼び出し側が絶対に観測できない状態がAPI契約上存在することになり矛盾する。
    乖離の事実自体はwarningログでのみ表現する。
    """

    VALIDATED = "VALIDATED"
    NOT_YET_VALIDATABLE = "NOT_YET_VALIDATABLE"
    SECONDARY_UNAVAILABLE = "SECONDARY_UNAVAILABLE"


class DividendPeriodEndBasis(StrEnum):
    """AnnualDividendActual.period_endの由来(配当データクロスバリデーション根本修正)。

    「実測かどうか」の単純な真偽値ではなく由来別に区別する。yfinance側の
    period_endもfiscal_year_end_month(推定含む)+配当イベント日からシステムが
    導出した値であり、EDINET当期のような一次情報源からの直接取得値ではないため、
    「実測」とは表現しない。
    """

    REPORTED = "REPORTED"  # EDINET当期: 書類一覧APIのperiodEnd実値
    DERIVED_FROM_FISCAL_YEAR_END = "DERIVED_FROM_FISCAL_YEAR_END"  # yfinance
    DERIVED_FROM_RELATIVE_PERIOD = "DERIVED_FROM_RELATIVE_PERIOD"  # EDINET前期以前(当期から逆算)


class StockType(StrEnum):
    """銘柄タイプ分類(要求仕様7節)。複合タイプはlist[StockType]で表現する。

    INCOMEは「高配当株」の表示ラベルとして再利用する(配当利回り単体基準、
    HIGH_DIVIDENDという別メンバーは新設しない)。DIVIDEND_GROWTH(連続増配株)
    ・QUALITY(優良株)はBUY候補裾野拡大機能(2026-08)で追加。
    """

    INCOME = "INCOME"
    GROWTH = "GROWTH"
    VALUE = "VALUE"
    CYCLICAL = "CYCLICAL"
    DEFENSIVE = "DEFENSIVE"
    TURNAROUND = "TURNAROUND"
    ASSET_PLAY = "ASSET_PLAY"
    EVENT_DRIVEN = "EVENT_DRIVEN"
    DIVIDEND_GROWTH = "DIVIDEND_GROWTH"
    QUALITY = "QUALITY"


_STOCK_TYPE_LABELS: dict[StockType, str] = {
    StockType.INCOME: "高配当",
    StockType.GROWTH: "成長株",
    StockType.VALUE: "割安株",
    StockType.CYCLICAL: "景気敏感株",
    StockType.DEFENSIVE: "ディフェンシブ株",
    StockType.TURNAROUND: "ターンアラウンド株",
    StockType.ASSET_PLAY: "資産株",
    StockType.EVENT_DRIVEN: "イベント関連株",
    StockType.DIVIDEND_GROWTH: "連続増配株",
    StockType.QUALITY: "優良株",
}


def stock_type_label(stock_type: StockType) -> str:
    """通知等で使う表示ラベル。INCOMEは内部enum値のまま「高配当」と表示する。"""
    return _STOCK_TYPE_LABELS.get(stock_type, stock_type.value)


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


class SellIntensity(StrEnum):
    """PARTIAL_PROFIT_TAKE成立後、何株売るかを決める売却強度(コードレビュー
    対応2026-08、指摘Part B)。「売るべきか」(RecommendationType/origin)とは
    別に、「何株売るか」を少数の構造化条件から決める。

    profit_taking.pyのorigin(判定がどの経路で成立したか)から一意に決まる:
    - LIGHT: origin=OTHER_CONDITIONS(非価格系の複数条件のみで成立した、
      最も弱い根拠のPARTIAL)
    - STANDARD: origin=PRICE_POSITION/FAIR_VALUE_STRONG(価格マトリクス・
      適正価格ベースの通常のPARTIAL)
    - STRONG: origin=PROFIT_PROTECTION_STRONG(利益保全のStrong条件、上値
      根拠に依存しない強い一部利確)
    - VERY_STRONG: STRONGに加え、株価トレンドも悪化している(momentum.
      trend_classificationがDOWNTREND/STRONG_DOWNTREND)場合
    """

    LIGHT = "LIGHT"
    STANDARD = "STANDARD"
    STRONG = "STRONG"
    VERY_STRONG = "VERY_STRONG"


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


class EarningsReleaseConfirmationState(StrEnum):
    """決算予定日を経過した後、無償データで発表実績を確認できない期間の状態
    (コードレビュー対応: 明治ホールディングス(2269)事例)。

    予定日前(EarningsDateStatus.CONFIRMED)の抑制は既存の
    profit_taking_suppression_business_daysロジックが担当するため対象外。
    DATA_UPDATEDの判定はfiscal_period_endの近似(想定報告ラグ以内かどうか)で
    あり、実際の決算発表日そのものを厳密に突合するものではない
    (EarningsWindowStatus.RECENTLY_REPORTEDと同種の近似)。
    """

    NOT_APPLICABLE = "NOT_APPLICABLE"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    DATA_UPDATED = "DATA_UPDATED"
    DELAYED = "DELAYED"


class EarningsDecisionRelevance(StrEnum):
    """過去の決算予定日が現在の判断にまだ関連しているかどうか(デプロイ前対応)。

    無償データのProviderが何か月も前の過去の決算予定日を返し続けた場合に、
    利確判定が無期限にREVIEW_AFTER_EARNINGSへ抑制され続けることを防ぐために
    導入する。RELEVANTの場合のみ通常判定を保留する。
    """

    RELEVANT = "RELEVANT"
    NOT_RELEVANT = "NOT_RELEVANT"
    UNKNOWN = "UNKNOWN"


class RecentPeriodsSource(StrEnum):
    """FinancialSummary.recent_quartersの実際の生成元(由来精緻化対応)。

    yfinanceは四半期データ(quarterly_income_stmt)を取得できない銘柄で年次
    データ(income_stmt)へフォールバックする場合があり、recent_quartersという
    名前だけでは四半期実績由来か年次フォールバック由来かを区別できない。
    """

    QUARTERLY = "QUARTERLY"
    ANNUAL_FALLBACK = "ANNUAL_FALLBACK"
    UNAVAILABLE = "UNAVAILABLE"


class FinancialPeriodEndSource(StrEnum):
    """決算反映確認に使った最新財務期間末の由来(デプロイ前対応・由来精緻化対応)。

    FinancialSummary.fiscal_period_endは年次決算期末を表すため、四半期ごとの
    決算発表の反映確認にはrecent_quarters由来の期末日を優先する。ただし
    recent_quarters自体が四半期実績由来か年次フォールバック由来かを
    FinancialSummary.recent_periods_sourceで判別し、この2つを区別して報告する
    (RECENT_PERIODSという曖昧な単一値では、年次フォールバックを四半期実績と
    誤認してしまう可能性があったため分離した)。
    """

    RECENT_QUARTERLY_PERIOD = "RECENT_QUARTERLY_PERIOD"
    RECENT_ANNUAL_FALLBACK = "RECENT_ANNUAL_FALLBACK"
    ANNUAL_FISCAL_PERIOD_END = "ANNUAL_FISCAL_PERIOD_END"
    UNAVAILABLE = "UNAVAILABLE"
    # recent_quartersに有効な期間末があるのにrecent_periods_source=UNAVAILABLE
    # というデータ不整合を検知した場合の安全側ラベル(period_end自体は
    # 引き続き有効な最大値を使う。DATA_UPDATED判定条件は変更しない)。
    UNKNOWN = "UNKNOWN"


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


class WatchType(StrEnum):
    """BuyAction.WATCH_FOR_PRICEのうち、積極監視・毎営業日通知の対象と
    なっている理由を表す付帯属性(BUY候補裾野拡大機能2026-08で新設)。

    `decide_buy_action()`が返す`BuyAction`自体は変更しない設計とし、
    NEAR BUYを新しいBuyActionメンバーとしては追加しない(既存の
    BUY_FAMILY_ACTIONS/WATCH_FAMILY_ACTIONS・raw_buy_action/
    final_buy_actionの意味を変えないため)。`Recommendation.watch_type`
    が`buy_action=WATCH_FOR_PRICE`の銘柄にのみ設定され得る。
    """

    NEAR_BUY = "NEAR_BUY"


class WatchTransitionType(StrEnum):
    """WatchStateService.evaluate_and_update()が返す当日の遷移種別
    (コードレビュー対応2026-08: WatchState/通知層へ「4日監視後にBUY到達」
    「PAUSED後の監視再開」等の情報を伝播するため構造化)。

    STARTED: 当日新規に監視開始。CONTINUED: 前営業日から連続して継続。
    RESUMED: 評価不能を挟んだ後、連続日数を1へリセットして継続再開
    (PAUSED明け)。PROMOTED_TO_BUY: 監視中の銘柄がBUY家族へ昇格し監視終了。
    ENDED: PRICE_OUT_OF_RANGE/NOT_ATTRACTIVE/STALEのいずれかで監視終了
    (TRADE_EVENTによる終了はWatchStateService.end_for_trade_events()が
    別途担当し、この戻り値には現れない)。NONE: 監視に一切関与しなかった
    (元々対象外、または開始条件を満たさなかった)。
    """

    STARTED = "STARTED"
    CONTINUED = "CONTINUED"
    RESUMED = "RESUMED"
    PROMOTED_TO_BUY = "PROMOTED_TO_BUY"
    ENDED = "ENDED"
    NONE = "NONE"


class NotificationCategory(StrEnum):
    """通知テンプレート・優先度・再送ポリシー選択のためだけに使う表示層の
    分類(BUY候補裾野拡大機能2026-08で新設)。`resolve_notification_category()`
    (line_notification_service.py)が`Recommendation`から一意に決定する
    唯一の分類ソースであり、`buy_action`/`watch_type`/`recommendation_type`
    を個別に参照する判定を各所に重複させないためのenum。
    """

    CRITICAL_RISK = "CRITICAL_RISK"
    BUY = "BUY"
    NEAR_BUY = "NEAR_BUY"
    WATCH_BEFORE_EARNINGS = "WATCH_BEFORE_EARNINGS"
    SELL = "SELL"
    # --- LINE通知/監査分離(2026-08、コードレビュー対応)で追加 ---
    # 利確WATCH・決算前後のレビュー保留・ポートフォリオ集中リスクをまとめる
    # 「監視」系カテゴリ。価格目安を持たない場合はreasonのみ表示する。
    WATCH = "WATCH"
    # 一部利確・決算接近時の一部縮小(表示ラベルのみlabel_overrideで差し替え)。
    PARTIAL_SELL = "PARTIAL_SELL"
    # データ品質上の理由で自動判定を保留し、手動確認を促す通知(REVIEW・
    # MANUAL_REVIEW_REQUIREDの両方がここに分類される)。
    MANUAL_REVIEW = "MANUAL_REVIEW"
    OTHER = "OTHER"
    NOT_NOTIFIABLE = "NOT_NOTIFIABLE"


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


class CandidateSource(StrEnum):
    """統合BUY候補パイプライン(気になる銘柄+保有銘柄の統合)における評価対象の由来。

    同一銘柄が気になる銘柄(ウォッチリスト)と保有銘柄の両方に登録されている場合は
    BOTHとし、評価・ランキング・通知・再送防止キーはすべて1件に統合する
    (登録元の違いによる二重評価・二重通知は行わない)。
    """

    WATCHLIST = "WATCHLIST"
    HOLDING = "HOLDING"
    BOTH = "BOTH"


class AddOnEligibility(StrEnum):
    """保有銘柄の買い増し固有リスクゲートの判定結果。

    共通購入判断(BuySignalService)がBUY系判定を出しても、保有銘柄については
    集中度・売却判定との競合等の買い増し固有リスクを追加確認する。ELIGIBLEの
    場合のみ最終的にBUY系として通知対象になりうる。気になる銘柄単独(保有情報
    なし)の場合はNOT_APPLICABLE(ゲート自体が適用対象外)。
    """

    ELIGIBLE = "ELIGIBLE"
    BLOCKED = "BLOCKED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EligibilityBlockCategory(StrEnum):
    """統合BUY候補パイプラインで通知対象外となった理由の分類(監査用)。

    集中度超過(ポートフォリオ制約)とデータ異常(データ品質)を混同しないよう、
    また「比率計算に使うデータそのものが信頼できない」場合(PORTFOLIO_DATA_
    RELIABILITY)と「比率は正常に計算できたが上限を超えた」場合(POSITION_
    CONCENTRATION/SECTOR_CONCENTRATION)を区別する。
    """

    DATA_QUALITY = "DATA_QUALITY"
    CONFLICTING_HOLDING_ACTION = "CONFLICTING_HOLDING_ACTION"
    HOLDING_DATA_INCONSISTENT = "HOLDING_DATA_INCONSISTENT"
    PORTFOLIO_DATA_RELIABILITY = "PORTFOLIO_DATA_RELIABILITY"
    POSITION_CONCENTRATION = "POSITION_CONCENTRATION"
    SECTOR_CONCENTRATION = "SECTOR_CONCENTRATION"
    EARNINGS_PROXIMITY = "EARNINGS_PROXIMITY"
    RECENTLY_NOTIFIED = "RECENTLY_NOTIFIED"
    OUTSIDE_TOP_5 = "OUTSIDE_TOP_5"

    # --- BUY候補裾野拡大・NEAR BUY監視・通知制御の再設計(2026-08)で追加 ---
    # 売買直後の通常クールダウン抑止(重大リスクは貫通する)。
    TRADE_COOLDOWN = "TRADE_COOLDOWN"
    # TradeDetectionRunLockがCOMPLETEDへ遷移したことを確認できなかった
    # (fail-closed)ため、当該実行では通常通知を抑止した場合。
    TRADE_DETECTION_IN_PROGRESS = "TRADE_DETECTION_IN_PROGRESS"
    # cross-pipeline重複抑止: 自分より優先度が高い/同等の通知が本日
    # 既に送信済みのため見送った場合。
    LOW_PRIORITY = "LOW_PRIORITY"
    # NEAR BUYの日次通知上限(既定5件)超過。
    DAILY_LIMIT_NEAR_BUY = "DAILY_LIMIT_NEAR_BUY"
    # NEAR BUY継続条件(continue_required_decline_pct)から外れた。
    WATCH_OUT_OF_RANGE = "WATCH_OUT_OF_RANGE"
    # 同一銘柄について同一バッチ内で既に別の通知を送信済み。
    DUPLICATE_STOCK_NOTIFICATION = "DUPLICATE_STOCK_NOTIFICATION"
    # --- LINE通知アクション限定化(2026-08)で追加 ---
    # WATCH/REVIEW(証拠品質系)/NEAR BUY等、ユーザーに明確な売買アクションを
    # 促さない判定のため、LINEへは送信しない(内部評価・Audit記録は継続する)。
    NON_ACTIONABLE = "NON_ACTIONABLE"


class PortfolioValuationBasis(StrEnum):
    """ポートフォリオ集中度計算に使った評価基準。

    保有銘柄全員分の現在値が取得できた場合のみMARKET_VALUE(時価総額ベース)
    とし、1件でも欠落・内容競合があればUNAVAILABLEとして時価ベースの比率を
    信頼できないものとして扱う(時価と取得金額を混在させない)。
    """

    MARKET_VALUE = "MARKET_VALUE"
    ACQUISITION_COST = "ACQUISITION_COST"
    UNAVAILABLE = "UNAVAILABLE"


# ============================================================================
# 保有判断スコア方式(2026-08仕様)で追加
# ============================================================================


class EvidenceCoverageStatus(StrEnum):
    """企業品質・投資ストーリー維持スコアの評価軸1件ごとの算出状況。

    NOT_EVALUATED(評価対象だがデータ不足で算出不能)とNOT_APPLICABLE(当該銘柄・
    投資前提では評価対象外)を区別する。前者はcomponent_scoreの分母(available_points)
    に残り不足として計上され、後者は分母から除外される(欠損項目を0点として
    扱わない、算出可能な項目だけで正規化する、という既存方針の実装)。
    """

    EVALUATED = "EVALUATED"
    NOT_EVALUATED = "NOT_EVALUATED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class HoldingDecisionCategory(StrEnum):
    """保有判断スコア(final_holding_decision_score)から決定する判定区分。

    通知対象はSELL_CONSIDERATION/STRONG_SELL_CONSIDERATIONのみ
    (score < notify_below_score、かつcoverage_gate_passed)。
    """

    STRONG_HOLD = "STRONG_HOLD"
    HOLD = "HOLD"
    CAUTION = "CAUTION"
    PARTIAL_SELL_CONSIDERATION = "PARTIAL_SELL_CONSIDERATION"
    SELL_WATCH = "SELL_WATCH"
    SELL_CONSIDERATION = "SELL_CONSIDERATION"
    STRONG_SELL_CONSIDERATION = "STRONG_SELL_CONSIDERATION"


class BaselineOrigin(StrEnum):
    """投資ストーリー維持スコアのbaseline(比較基準)がどう確定したかの由来。

    優先順位: HUMAN_APPROVED > PURCHASE_SNAPSHOT/HOLDING_REGISTRATION_SNAPSHOT
    > HISTORICAL_RECONSTRUCTED > COMMON_TEMPLATE > SYSTEM_INITIALIZED。
    """

    HUMAN_APPROVED = "HUMAN_APPROVED"
    PURCHASE_SNAPSHOT = "PURCHASE_SNAPSHOT"
    HOLDING_REGISTRATION_SNAPSHOT = "HOLDING_REGISTRATION_SNAPSHOT"
    HISTORICAL_RECONSTRUCTED = "HISTORICAL_RECONSTRUCTED"
    COMMON_TEMPLATE = "COMMON_TEMPLATE"
    SYSTEM_INITIALIZED = "SYSTEM_INITIALIZED"


class BaselineStatus(StrEnum):
    """InvestmentThesisBaseline専用の状態(人間承認の進行状態のみを表す)。

    既存のApprovalStatus(RuleVersion/RuleProposal共用)を拡張せず専用enumとして
    分離する。「現在有効なbaselineかどうか」はこのステータスではなく
    InvestmentThesisBaselinePointerが唯一の情報源として判定する
    (ACTIVE/SUPERSEDED/ROLLED_BACKに相当する値をここに持たせない)。
    """

    DRAFT = "DRAFT"
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ThesisConditionAttestationStatus(StrEnum):
    """CustomThesisCondition(個別購入理由)に対する人間の定期申告状態。

    自由記述の解釈やLLMによる自動判定ではなく、人間が明示的に申告した値のみを使う。
    """

    MAINTAINED = "MAINTAINED"
    BROKEN = "BROKEN"
    UNCERTAIN = "UNCERTAIN"


class HoldingDecisionConfidenceLevel(StrEnum):
    """保有判断スコアのcoverage_ratioから決まる信頼度(既存ConfidenceLevelとは別概念)。

    coverage_ratio < 0.6はINSUFFICIENT_EVIDENCEとし、通常の売却通知を禁止する
    (一次情報で確認できたハードゲートがある場合のみ例外的に緊急通知可)。
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ExecutionPlanReason(StrEnum):
    """HoldingDecisionExecutionPlanがこの組み合わせになった理由(監査用)。"""

    NORMAL_LEGACY = "NORMAL_LEGACY"
    NORMAL_SHADOW = "NORMAL_SHADOW"
    NORMAL_ACTIVE = "NORMAL_ACTIVE"
    FINANCIAL_MODEL_DEFERRED = "FINANCIAL_MODEL_DEFERRED"


class RuntimeConfigMode(StrEnum):
    """HoldingDecisionRuntimeConfig.modeの許容値。"""

    LEGACY = "legacy"
    SHADOW = "shadow"
    ACTIVE = "active"


class FinancialPolicyOverride(StrEnum):
    """HoldingDecisionRuntimeConfig.financial_policy_overrideの許容値。

    DEFAULTはYAML(industry_scoring_policy.yaml)のカテゴリ別deferred設定をそのまま使う。
    FORCE_DEFER_ALLはYAML側の解除状況に関わらず全金融業カテゴリを即座に退避させる
    緊急オーバーライド。RuntimeConfigから金融業をActiveへ強制する値は存在しない
    (Active化はYAML改版を伴う設計変更としてのみ実施する)。
    """

    DEFAULT = "DEFAULT"
    FORCE_DEFER_ALL = "FORCE_DEFER_ALL"


# ============================================================================
# LINEボタン起点の会話型UI(2026-08)で追加
# ============================================================================


class ConversationAction(StrEnum):
    """LINEリッチメニュー/postbackから開始する対話の種別。

    ChatCommandServiceのCSVコマンド語(買付/売却/ウォッチ)とは独立した
    英語enumとして定義する(postback data値`action=start_buy`等・
    DynamoDB ConversationStatesテーブルの`action`属性値として使う)。
    """

    BUY = "BUY"
    SELL = "SELL"
    WATCH = "WATCH"


class ConversationStateName(StrEnum):
    """ConversationStateの状態遷移(入力待ち/確認待ちの2段階のみ)。

    BUY/SELL/WATCHいずれも同型の2状態で表現し、どの対話種別かは
    ConversationAction(`action`属性)側で区別する(状態名自体をBUY_INPUT_
    WAITING等の複合値にしない設計。実装プランv2 2節)。
    """

    INPUT_WAITING = "INPUT_WAITING"
    CONFIRM_WAITING = "CONFIRM_WAITING"
