"""LINE通知履歴(要求仕様10節・16節)。同一内容の重複通知を防止するために使用する。"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import NotificationType


class NotificationLog(Entity):
    notification_id: str
    notification_type: NotificationType
    stock_code: str | None = None
    content_hash: str
    sent_at: dt.datetime
    related_recommendation_id: str | None = None
