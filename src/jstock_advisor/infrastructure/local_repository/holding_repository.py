"""保有銘柄(PurchaseLot / Holding)のローカルリポジトリ。

PurchaseLotが正データであり、Holdingは平均購入単価等を計算済みでキャッシュした
非正規化データである。ロットが変更されたら portfolio_service 側で
domain.entities.holding.summarize_lots を使って Holding を再計算し upsert する。
"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.holding import Holding, PurchaseLot
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store


class PurchaseLotRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[PurchaseLot] = build_collection_store(
            PurchaseLot, "purchase_lots.json", "lot_id", store_dir
        )

    def list_all(self) -> list[PurchaseLot]:
        return self._store.list_all()

    def list_by_stock(self, stock_code: str) -> list[PurchaseLot]:
        lots = self._store.find(lambda lot: lot.stock_code == stock_code)
        return sorted(lots, key=lambda lot: lot.purchase_date)

    def get(self, lot_id: str) -> PurchaseLot | None:
        return self._store.get(lot_id)

    def upsert(self, lot: PurchaseLot) -> None:
        self._store.upsert(lot)

    def delete(self, lot_id: str) -> bool:
        return self._store.delete(lot_id)

    def delete_by_stock(self, stock_code: str) -> int:
        lots = self.list_by_stock(stock_code)
        for lot in lots:
            self._store.delete(lot.lot_id)
        return len(lots)


class HoldingRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[Holding] = build_collection_store(
            Holding, "holdings.json", "stock_code", store_dir
        )

    def list_all(self) -> list[Holding]:
        return self._store.list_all()

    def get(self, stock_code: str) -> Holding | None:
        return self._store.get(stock_code)

    def upsert(self, holding: Holding) -> None:
        self._store.upsert(holding)

    def delete(self, stock_code: str) -> bool:
        return self._store.delete(stock_code)
