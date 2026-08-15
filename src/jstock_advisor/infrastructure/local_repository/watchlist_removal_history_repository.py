"""AUTO_SCREENING銘柄の自動削除における、再追加クールダウン判定専用リポジトリ
(計画Part C-4)。

`readd_cooldown_days`は固定値(config.watchlist_screening.auto_removal)のため、
DynamoDB Native TTL(`ttl_seconds`、書き込み時刻からの固定オフセット)でそのまま
表現できる(クールダウン終了と同時に行が自動的に消える)。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from jstock_advisor.domain.entities.watchlist import WatchlistRemovalHistory
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store

_SECONDS_PER_DAY = 86400


class WatchlistRemovalHistoryRepository:
    def __init__(self, readd_cooldown_days: int, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[WatchlistRemovalHistory] = build_collection_store(
            WatchlistRemovalHistory,
            "watchlist_removal_history.json",
            "stock_code",
            store_dir,
            ttl_seconds=readd_cooldown_days * _SECONDS_PER_DAY,
        )

    def get(self, stock_code: str) -> WatchlistRemovalHistory | None:
        return self._store.get(stock_code)

    def upsert(self, item: WatchlistRemovalHistory) -> None:
        self._store.upsert(item)

    def is_in_cooldown(self, stock_code: str, now: dt.datetime) -> bool:
        record = self.get(stock_code)
        return record is not None and record.cooldown_until > now
