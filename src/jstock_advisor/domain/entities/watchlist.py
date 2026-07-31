"""ウォッチリスト(要求仕様6節)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import Priority, WatchlistRegistrationSource


class WatchlistItem(Entity):
    stock_code: str
    stock_name: str | None = None
    reason: str | None = None
    desired_total_yield_pct: float | None = None
    desired_buy_price: Decimal | None = None
    benefit_interest: bool = False
    priority: Priority = Priority.MEDIUM
    notify_enabled: bool = True
    memo: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime

    # --- ウォッチリスト自動追加機能で追加。既存レコードには存在しないため、
    # 後方互換のためデフォルト値(手動登録)を持たせる ---
    registration_source: WatchlistRegistrationSource = WatchlistRegistrationSource.MANUAL
    registration_policy: str | None = None
