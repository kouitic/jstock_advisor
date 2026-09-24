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

    def get_raw_data(self, stock_code: str) -> str | None:
        return self._store.get_raw_data(stock_code)

    def upsert(self, item: WatchlistItem) -> None:
        self._store.upsert(item)

    def add_if_new(self, item: WatchlistItem) -> bool:
        """既存があれば触らずFalse、無ければ追加してTrue(冪等な新規追加専用)。

        ウォッチリスト自動追加機能向け。手動登録用のupsert/WatchlistService.add_item
        (常に上書き)とは異なり、既存の手動登録内容を一切変更しない。
        """
        return self._store.insert_if_absent(item)

    def insert_if_absent(self, item: WatchlistItem) -> bool:
        """Issue #530: `add_if_new()`と同じ(CASを汎用の名前でも呼べるようにする)。"""
        return self._store.insert_if_absent(item)

    def replace_if_raw_matches(
        self, stock_code: str, expected_raw_data: str, item: WatchlistItem
    ) -> bool:
        """Issue #530: 保存中の生JSONがexpected_raw_dataと完全一致する場合のみ
        置き換える(楽観ロック)。"""
        return self._store.replace_if_raw_matches(stock_code, expected_raw_data, item)

    def delete(self, stock_code: str) -> bool:
        return self._store.delete(stock_code)
