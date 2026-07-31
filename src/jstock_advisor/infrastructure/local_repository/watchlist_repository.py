"""ウォッチリストのローカルリポジトリ。"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store


class WatchlistRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[WatchlistItem] = build_collection_store(
            WatchlistItem, "watchlist.json", "stock_code", store_dir
        )

    def list_all(self) -> list[WatchlistItem]:
        return self._store.list_all()

    def get(self, stock_code: str) -> WatchlistItem | None:
        return self._store.get(stock_code)

    def upsert(self, item: WatchlistItem) -> None:
        self._store.upsert(item)

    def add_if_new(self, item: WatchlistItem) -> bool:
        """既存があれば触らずFalse、無ければ追加してTrue(冪等な新規追加専用)。

        ウォッチリスト自動追加機能向け。手動登録用のupsert/WatchlistService.add_item
        (常に上書き)とは異なり、既存の手動登録内容を一切変更しない。
        """
        return self._store.insert_if_absent(item)

    def delete(self, stock_code: str) -> bool:
        return self._store.delete(stock_code)
