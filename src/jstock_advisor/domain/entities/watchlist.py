"""ウォッチリスト(要求仕様6節)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import Priority


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
