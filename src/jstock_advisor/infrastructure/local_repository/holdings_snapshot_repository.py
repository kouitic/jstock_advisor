"""保有銘柄スナップショット(HoldingsSnapshotEntry)のローカルリポジトリ(BUY候補裾野拡大機能2026-08)。"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holdings_snapshot import HoldingsSnapshotEntry
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store

PRODUCTION_FILE_NAME = "holdings_snapshots.json"
VALIDATION_FILE_NAME = "validation_holdings_snapshots.json"
_VALIDATION_TTL_SECONDS = 2 * 60 * 60


class HoldingsSnapshotRepository:
    def __init__(
        self,
        store_dir: Path | None = None,
        file_name: str = PRODUCTION_FILE_NAME,
        ttl_seconds: int | None = None,
    ) -> None:
        self._store: CollectionStore[HoldingsSnapshotEntry] = build_collection_store(
            HoldingsSnapshotEntry, file_name, "stock_code", store_dir, ttl_seconds=ttl_seconds
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

    def get(self, stock_code: str) -> HoldingsSnapshotEntry | None:
        return self._store.get(stock_code)

    def list_all(self) -> list[HoldingsSnapshotEntry]:
        return self._store.list_all()

    def upsert(self, entry: HoldingsSnapshotEntry) -> None:
        self._store.upsert(entry)
