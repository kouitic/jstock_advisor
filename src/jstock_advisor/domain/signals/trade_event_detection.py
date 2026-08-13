"""保有銘柄リストの変化から売買イベントを検知する純粋関数(BUY候補裾野拡大機能2026-08)。

`Transaction`(ユーザー手動入力専用)は使わず、`Holding`(現在状態のみの
キャッシュ)と前回の`HoldingsSnapshotEntry`を比較する自動検知方式を採る。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.domain.entities.enums import TransactionType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holdings_snapshot import HoldingsSnapshotEntry


@dataclass(frozen=True)
class TradeEvent:
    stock_code: str
    event_type: TransactionType
    detected_at: dt.date
    shares: int
    average_purchase_price: Decimal | None


def detect_trade_events(
    previous_snapshots: dict[str, HoldingsSnapshotEntry],
    current_holdings: dict[str, Holding],
    today: dt.date,
) -> list[TradeEvent]:
    """前回スナップショットと現在保有銘柄一覧の和集合を走査し、差分から
    売買イベントを判定する(和集合を使うことで、全部売却によりcurrent_holdings
    から消えた銘柄も正しく検知できる)。

    - 前回0株 → 現在正の株数: BUY(新規購入。tombstone=前回shares=0からの
      再購入も同じ経路でBUYとして検知される)
    - 株数増加: ADDITIONAL_BUY(買い増し)
    - 株数減少(0にはならない): PARTIAL_SELL(一部売却)
    - 前回正の株数 → 現在0株: FULL_SELL(全部売却)

    呼び出し側(TradeCooldownService)が「初回実行(前回スナップショットが
    全く存在しない)」の判定・スキップを担当する(このモジュールはあくまで
    渡された2つの状態の差分のみを機械的に返す)。
    """
    events: list[TradeEvent] = []
    all_codes = set(previous_snapshots) | set(current_holdings)
    for code in sorted(all_codes):
        previous = previous_snapshots.get(code)
        prev_shares = previous.shares if previous is not None else 0
        current = current_holdings.get(code)
        current_shares = current.shares if current is not None else 0
        if prev_shares == current_shares:
            continue

        if prev_shares == 0 and current_shares > 0:
            event_type = TransactionType.BUY
        elif current_shares == 0 and prev_shares > 0:
            event_type = TransactionType.FULL_SELL
        elif current_shares > prev_shares:
            event_type = TransactionType.ADDITIONAL_BUY
        else:
            event_type = TransactionType.PARTIAL_SELL

        events.append(
            TradeEvent(
                stock_code=code,
                event_type=event_type,
                detected_at=today,
                shares=current_shares,
                average_purchase_price=(
                    current.average_purchase_price if current is not None else None
                ),
            )
        )
    return events
