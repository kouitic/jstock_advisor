"""銘柄名の手動オーバーライドのローカルリポジトリ(2026-07 BUYパイプライン
第2次修正。要求仕様19節)。

EDINET提出書類のfilerNameでは日本語社名を解決できない、または表記の見直しが
必要な銘柄のみ、運用者が手動で登録する(株主優待・企業行動と同じ設計方針)。
銘柄コードを主キーとし、1銘柄1レコード。
"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store
from jstock_advisor.interfaces.types import StockNameOverride


class StockNameOverrideRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[StockNameOverride] = build_collection_store(
            StockNameOverride, "stock_name_overrides.json", "stock_code", store_dir
        )

    def list_all(self) -> list[StockNameOverride]:
        return self._store.list_all()

    def get(self, stock_code: str) -> str | None:
        override = self._store.get(stock_code)
        return override.stock_name if override is not None else None

    def save(self, stock_code: str, stock_name: str) -> None:
        self._store.upsert(StockNameOverride(stock_code=stock_code, stock_name=stock_name))

    def delete(self, stock_code: str) -> bool:
        return self._store.delete(stock_code)
