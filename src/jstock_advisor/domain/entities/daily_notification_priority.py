"""cross-pipeline通知優先度記録(コードレビュー対応2026-08、指摘5)。

BUY候補Lambdaと保有銘柄Lambdaは別々のLambdaであり、共有の排他制御機構を
持たない。本エンティティは「当日・当該銘柄について、これまでに送信された
通知のうち最も高い優先度」を記録し、best-effortで低優先度の重複通知を
抑止するために使う(完全な排他制御ではない、要求仕様どおりの合意事項)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.base import Entity


def build_daily_notification_priority_id(stock_code: str, business_date: dt.date) -> str:
    return f"{business_date.isoformat()}:{stock_code}"


class DailyNotificationPriorityRecord(Entity):
    record_id: str
    stock_code: str
    business_date: dt.date
    # 数値が大きいほど優先度が高い(CRITICAL_RISK > BUY到達 > SELL > BUY >
    # NEAR_BUY > WATCH_BEFORE_EARNINGS。line_notification_service.py
    # _notification_priority()参照)。
    priority: int
    category: str
