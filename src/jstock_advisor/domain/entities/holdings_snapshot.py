"""保有銘柄の前回スナップショット(BUY候補裾野拡大機能2026-08)。

売買イベント(新規購入/買い増し/一部売却/全部売却)を保有銘柄リストの
変化から検知するための差分基準であり、同時に検知後のクールダウン期限も
保持する(TradeCooldownService参照)。

全部売却(FULL_SELL)後もレコードを削除せず、shares=0・active_holding=False の
tombstoneとして保持し続ける(クールダウン期間中に再び「新規購入」と誤検知
されるのを防ぎ、かつ次回以降の差分計算の基準として使い続けるため)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import TransactionType


class HoldingsSnapshotEntry(Entity):
    """owner/holding_id(M3): holding_id = owner + "#" + stock_code。同一
    stock_codeを複数ownerが保有する場合、owner別に別レコードとして保持する。
    """

    owner: str
    holding_id: str
    stock_code: str
    shares: int
    average_purchase_price: Decimal | None = None
    recorded_at: dt.date
    # 直近で検知した売買イベントの種別(検知が無い場合はNone)。
    last_trade_event_type: TransactionType | None = None
    trade_detected_at: dt.date | None = None
    # クールダウン終了日(この日を含めて抑止する。§6の境界値定義参照)。
    cooldown_until_date: dt.date | None = None
    # False = tombstone(全部売却後、保有していない状態)。
    active_holding: bool = True
