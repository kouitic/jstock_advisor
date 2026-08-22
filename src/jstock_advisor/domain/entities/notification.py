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

    # 保有銘柄オーナー機能(2026-08、移行専用)。related_recommendation_idが
    # 指す先のRecommendationのscope(owner/holding_id)をそのまま引き継ぐ
    # (holding-scopeならbackfill、stock-scopeまたは対応するRecommendationが
    # 無い場合はNoneのまま)。
    owner: str | None = None
    holding_id: str | None = None
