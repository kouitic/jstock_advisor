"""保有銘柄(PurchaseLot / Holding)のローカルリポジトリ。

PurchaseLotが正データであり、Holdingは平均購入単価等を計算済みでキャッシュした
非正規化データである。ロットが変更されたら portfolio_service 側で
domain.entities.holding.summarize_lots を使って Holding を再計算し upsert する。

M3(保有銘柄オーナー機能アプリ切替): HoldingRepositoryの主キーはstock_codeから
holding_id(= owner + "#" + stock_code)へ変更した。物理ファイル/テーブルも
M2で移行済みのholdings_v2.jsonを参照する。PurchaseLotRepositoryのPK自体は
lot_idのまま変更していない(M2でowner/holding_idを既存purchase_lots.jsonへ
in-place追加済みのため、ファイル自体も変更しない)が、主な検索単位を
stock_codeからholding_id(list_by_holding)へ変更する。
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

    def list_by_holding(self, holding_id: str) -> list[PurchaseLot]:
        lots = self._store.find(lambda lot: lot.holding_id == holding_id)
        return sorted(lots, key=lambda lot: lot.purchase_date)

    def list_by_stock(self, stock_code: str) -> list[PurchaseLot]:
        """owner横断検索用(BUY候補側でのstock横断cooldown判定等)。SELL/FIFO/
        平均取得単価計算にはlist_by_holding()を使うこと。"""
        lots = self._store.find(lambda lot: lot.stock_code == stock_code)
        return sorted(lots, key=lambda lot: lot.purchase_date)

    def get(self, lot_id: str) -> PurchaseLot | None:
        return self._store.get(lot_id)

    def get_raw_data(self, lot_id: str) -> str | None:
        return self._store.get_raw_data(lot_id)

    def upsert(self, lot: PurchaseLot) -> None:
        self._store.upsert(lot)

    def delete(self, lot_id: str) -> bool:
        return self._store.delete(lot_id)

    def delete_by_holding(self, holding_id: str) -> int:
        lots = self.list_by_holding(holding_id)
        for lot in lots:
            self._store.delete(lot.lot_id)
        return len(lots)


class HoldingRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[Holding] = build_collection_store(
            Holding, "holdings_v2.json", "holding_id", store_dir
        )

    def list_all(self) -> list[Holding]:
        return self._store.list_all()

    def get(self, holding_id: str) -> Holding | None:
        return self._store.get(holding_id)

    def get_raw_data(self, holding_id: str) -> str | None:
        return self._store.get_raw_data(holding_id)

    def upsert(self, holding: Holding) -> None:
        self._store.upsert(holding)

    def delete(self, holding_id: str) -> bool:
        return self._store.delete(holding_id)
