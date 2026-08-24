"""保有銘柄1件ごとの評価・通知結果の監査記録(2026-07仕様レビュー対応・要求仕様§12)。

通知が送られなかった銘柄について、正常なHOLDなのか、データ不足なのか、
データ品質チェックでブロックされたのか、処理自体が失敗したのかを区別できるように
する。既存のAuditLogTable(AuditService)へ出力値の一部として記録されるほか、
CloudWatch Logsへも構造化して出力し、バッチサマリーの内訳集計に使う。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EvaluationStatus,
    NotificationIntent,
    NotificationStatus,
    RecommendationType,
)


@dataclass(frozen=True)
class HoldingEvaluationAudit:
    stock_code: str
    evaluated_at: dt.datetime
    evaluation_status: EvaluationStatus
    raw_sell_recommendation_type: RecommendationType | None
    raw_profit_recommendation_type: RecommendationType | None
    final_recommendation_type: RecommendationType | None
    notification_status: NotificationStatus
    notification_suppression_reason: str | None
    sell_signal_status: str
    profit_taking_status: str
    fair_value_status: str
    data_quality_status: str
    confidence: ConfidenceLevel | None
    error_code: str | None
    # 通知意図3段階化(2026-08)。ACTIONABLE/ATTENTION/INTERNAL_ONLYのいずれか、
    # または評価対象外(recommendation自体が無い等)の場合はNone。
    notification_intent: NotificationIntent | None = None
    # ATTENTIONの場合のみ設定("PROFIT_PROTECTION_CANDIDATE"/
    # "PROFIT_PROTECTION_STRONG_NOT_EXECUTABLE")。それ以外は常にNone。
    attention_origin: str | None = None


# バッチサマリーの内訳集計(要求仕様§13)で使う集約カテゴリ。
_SUMMARY_CATEGORY_SENT = "sent"
_SUMMARY_CATEGORY_HOLD = "hold"
_SUMMARY_CATEGORY_REVIEW = "review"
_SUMMARY_CATEGORY_DATA_INSUFFICIENT = "data_insufficient"
_SUMMARY_CATEGORY_SUPPRESSED = "suppressed"
_SUMMARY_CATEGORY_FAILED = "failed"
# 買い候補分析専用: 買いシグナル自体は成立したが、1回あたりの通知上限
# (スコア上位N件)により今回は通知を見送った銘柄(2026-07仕様追加)。
_SUMMARY_CATEGORY_CANDIDATE_NOT_RANKED = "candidate_not_ranked"
# 買い候補分析専用: 価格待ち(WATCH_FOR_PRICE/WATCH_BEFORE_EARNINGS)判定だが、
# 1回あたりの通知上限により今回は通知を見送った銘柄(BUYパイプライン再設計§17)。
_SUMMARY_CATEGORY_WATCH_NOT_RANKED = "watch_not_ranked"
# 買い候補分析専用(購入判定カテゴリ改修2026-08): WATCH_FOR_PRICEのうち
# watch_type==NEAR_BUY(買い間近)。
_SUMMARY_CATEGORY_NEAR_BUY = "near_buy"
# 買い候補分析専用(購入判定カテゴリ改修2026-08): WATCH_FOR_PRICE(非NEAR_BUY)
# およびWATCH_BEFORE_EARNINGS(買い待ち)。
_SUMMARY_CATEGORY_WATCH_WAIT = "watch_wait"
# ウォッチリスト自動追加機能専用: スクリーニング(必須条件+スコア条件)に合格した銘柄。
_SUMMARY_CATEGORY_PASSED = "passed"
# ウォッチリスト自動追加機能専用: 必須条件(時価総額・営業CF等)を満たさず不合格。
_SUMMARY_CATEGORY_REQUIRED_CONDITION_FAILED = "required_condition_failed"
# ウォッチリスト自動追加機能専用: 必須条件は満たすがスコアが合格基準未満で不合格。
_SUMMARY_CATEGORY_SCORE_FAILED = "score_failed"

SUMMARY_CATEGORIES = (
    _SUMMARY_CATEGORY_SENT,
    _SUMMARY_CATEGORY_HOLD,
    _SUMMARY_CATEGORY_REVIEW,
    _SUMMARY_CATEGORY_DATA_INSUFFICIENT,
    _SUMMARY_CATEGORY_SUPPRESSED,
    _SUMMARY_CATEGORY_FAILED,
    _SUMMARY_CATEGORY_CANDIDATE_NOT_RANKED,
    _SUMMARY_CATEGORY_WATCH_NOT_RANKED,
    _SUMMARY_CATEGORY_NEAR_BUY,
    _SUMMARY_CATEGORY_WATCH_WAIT,
    _SUMMARY_CATEGORY_PASSED,
    _SUMMARY_CATEGORY_REQUIRED_CONDITION_FAILED,
    _SUMMARY_CATEGORY_SCORE_FAILED,
)


def summary_category(audit: HoldingEvaluationAudit) -> str:
    """バッチサマリーの6区分(通知送信/保有継続/要確認/データ不足/再通知抑止/処理失敗)
    のいずれに属するかを判定する。
    """
    if audit.evaluation_status == EvaluationStatus.ANALYSIS_FAILED:
        return _SUMMARY_CATEGORY_FAILED
    if audit.evaluation_status == EvaluationStatus.DATA_INSUFFICIENT:
        return _SUMMARY_CATEGORY_DATA_INSUFFICIENT
    if audit.evaluation_status == EvaluationStatus.DATA_QUALITY_BLOCKED:
        return _SUMMARY_CATEGORY_REVIEW
    if audit.notification_status == NotificationStatus.SENT:
        return _SUMMARY_CATEGORY_SENT
    if audit.notification_status == NotificationStatus.NOT_REQUIRED:
        return _SUMMARY_CATEGORY_HOLD
    # DUPLICATE_SUPPRESSED / RESEND_INTERVAL_NOT_REACHED / PRICE_CHANGE_BELOW_THRESHOLD
    return _SUMMARY_CATEGORY_SUPPRESSED
