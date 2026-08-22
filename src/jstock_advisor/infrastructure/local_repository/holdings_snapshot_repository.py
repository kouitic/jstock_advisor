"""保有銘柄スナップショット(HoldingsSnapshotEntry)のローカルリポジトリ(BUY候補裾野拡大機能2026-08)。"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holdings_snapshot import HoldingsSnapshotEntry
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store

PRODUCTION_FILE_NAME = "holdings_snapshots_v2.json"
VALIDATION_FILE_NAME = "validation_holdings_snapshots_v2.json"
_VALIDATION_TTL_SECONDS = 2 * 60 * 60


class HoldingsSnapshotRepository:
    """M3: 主キーをstock_codeからholding_idへ変更し、M2移行済みのV2テーブル
    (holdings_snapshots_v2.json / validation_holdings_snapshots_v2.json)を
    参照する。"""

    def __init__(
        self,
        store_dir: Path | None = None,
        file_name: str = PRODUCTION_FILE_NAME,
        ttl_seconds: int | None = None,
    ) -> None:
        self._store: CollectionStore[HoldingsSnapshotEntry] = build_collection_store(
            HoldingsSnapshotEntry, file_name, "holding_id", store_dir, ttl_seconds=ttl_seconds
        )

    @classmethod
    def for_execution_context(
        cls, execution_context: ExecutionContext, store_dir: Path | None = None
    ) -> HoldingsSnapshotRepository:
        if execution_context.is_validation:
            return cls(
                store_dir=store_dir,
                file_name=VALIDATION_FILE_NAME,
                ttl_seconds=_VALIDATION_TTL_SECONDS,
            )
        return cls(store_dir=store_dir, file_name=PRODUCTION_FILE_NAME, ttl_seconds=None)

    def get(self, holding_id: str) -> HoldingsSnapshotEntry | None:
        return self._store.get(holding_id)

    def list_all(self) -> list[HoldingsSnapshotEntry]:
        return self._store.list_all()

    def list_by_stock(self, stock_code: str) -> list[HoldingsSnapshotEntry]:
        """owner横断検索用(BUY候補側でのstock横断cooldown判定)。"""
        return self._store.find(lambda entry: entry.stock_code == stock_code)

    def upsert(self, entry: HoldingsSnapshotEntry) -> None:
        self._store.upsert(entry)
