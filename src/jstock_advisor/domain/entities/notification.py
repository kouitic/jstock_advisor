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

    # 保有銘柄オーナー機能(2026-08)。related_recommendation_idが指す先の
    # Recommendationのscope(owner/holding_id)をそのまま引き継ぐ。M3移行が
    # 既存レコードをbackfillしたのに加え、Issue #33以降は通常の通知保存時にも
    # 送信対象Recommendationのscopeを転記する(holding-scope再送判定
    # latest_by_holding_and_type()が過去実績を発見できるようにするため)。
    # stock-scope(holding_id=None)のRecommendation、またはRecommendationを
    # 持たない通知(バッチサマリー・ウォッチリスト追加・開示速報等)では
    # Noneのまま。
    owner: str | None = None
    holding_id: str | None = None
