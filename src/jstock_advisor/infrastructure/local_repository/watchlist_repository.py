"""ウォッチリストのローカルリポジトリ。"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.infrastructure.local_repository.json_store import JsonCollectionStore


class WatchlistRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: JsonCollectionStore[WatchlistItem] = JsonCollectionStore(
            WatchlistItem, "watchlist.json", "stock_code", store_dir
        )

    def list_all(self) -> list[WatchlistItem]:
        return self._store.list_all()

    def get(self, stock_code: str) -> WatchlistItem | None:
        return self._store.get(stock_code)

    def upsert(self, item: WatchlistItem) -> None:
        self._store.upsert(item)

    def delete(self, stock_code: str) -> bool:
        return self._store.delete(stock_code)
