"""保有銘柄1回の評価結果レコード(Phase 2-B「銘柄分析」向け、2026-08)。

BUY側のBuyCandidateEvaluationRecordと対称的な、SELL/HOLD側の最小限の関連付け
レコード。Legacy SELL(sell_signal_service)・ProfitTaking(profit_taking_service)・
HoldingDecisionResult(holding_decision_service)・Recommendationのいずれの詳細
ペイロードも複製せず、参照IDのみを保持する。

実際に通知判断を担当したエンジン(authoritative_engine)は、kill switch適用後の
plan.allow_*_notificationではなく、kill switchの影響を受けないmode_plan
(holdings_watchlist_handler.py::_analyze_one_holding()のmode_plan、
notification_enabled=True相当)を基準に決定する。notification_enabled(kill switch
の実際の値)とauthoritative_notification_sent(実際にLINE送信されたか)は
別フィールドとして保持し、「本来の判定担当」と「実際に送信されたか」を混同しない。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.base import Entity


class HoldingEvaluationRecord(Entity):
    # PK: f"{holding_id}:{evaluated_at.isoformat()}"(1回の評価につき1件、
    # 同一holding_idでも評価時刻が異なれば別レコード)。
    holding_evaluation_id: str
    # owner + "#" + stock_code(Holding.holding_idと同一値)。GSI(holding_id-index)
    # のHASHキーとしてトップレベル属性でも書き込む(リポジトリ参照)。
    holding_id: str
    owner: str
    stock_code: str
    evaluated_at: dt.datetime
    rule_version: str

    # 評価時点のExecutionPlanのスナップショット。stock_snapshot取得自体が失敗した
    # 場合はplan計算前のためNoneのまま(現行データではその時点のmode等を
    # 復元できない)。
    execution_plan_mode: str | None = None
    execution_plan_reason: str | None = None
    # kill switchの実際の値。この値**だけ**では「本来の判定担当」は分からない
    # (モジュールdocstring参照。authoritative_engineはmode_plan基準で別途決定する)。
    notification_enabled: bool | None = None

    # 「本来の判定担当」エンジン。"LEGACY_SELL" | "PROFIT_TAKING" |
    # "HOLDING_DECISION_SCORE" | None(担当エンジンが無い、またはスナップショット
    # 取得失敗により判定自体に至らなかった場合)。
    authoritative_engine: str | None = None
    # _HoldingResult.categoryをそのまま保持("hold" / "data_insufficient" /
    # summary_category()が返す各種区分)。
    authoritative_outcome_category: str
    authoritative_recommendation_id: str | None = None
    # 実際にLINE個別通知が送信されたか(notification_enabledとは独立。kill switch
    # 抑止・DataQualityブロック等で送信されなければFalse)。
    authoritative_notification_sent: bool = False

    legacy_sell_ran: bool = False
    legacy_sell_recommendation_id: str | None = None
    profit_taking_ran: bool = False
    profit_taking_recommendation_id: str | None = None
    holding_decision_ran: bool = False
    holding_decision_result_id: str | None = None
    holding_decision_notified: bool = False


def build_holding_evaluation_id(holding_id: str, evaluated_at: dt.datetime) -> str:
    return f"{holding_id}:{evaluated_at.isoformat()}"
