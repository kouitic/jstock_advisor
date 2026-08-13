"""cross-pipeline通知優先度記録のローカルリポジトリ(コードレビュー対応2026-08、指摘5)。"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.daily_notification_priority import (
    DailyNotificationPriorityRecord,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store

PRODUCTION_FILE_NAME = "daily_notification_priorities.json"
VALIDATION_FILE_NAME = "validation_daily_notification_priorities.json"
# 当日の重複抑止にのみ使う一過性データのため、本番でも短いTTLで自動失効させる
# (HoldingsSnapshotEntryのような長期保持は不要)。
_TTL_SECONDS = 48 * 60 * 60


class DailyNotificationPriorityRepository:
    def __init__(
        self,
        store_dir: Path | None = None,
        file_name: str = PRODUCTION_FILE_NAME,
        ttl_seconds: int | None = _TTL_SECONDS,
    ) -> None:
        self._store: CollectionStore[DailyNotificationPriorityRecord] = build_collection_store(
            DailyNotificationPriorityRecord,
            file_name,
            "record_id",
            store_dir,
            ttl_seconds=ttl_seconds,
        )

    @classmethod
    def for_execution_context(
        cls, execution_context: ExecutionContext, store_dir: Path | None = None
    ) -> DailyNotificationPriorityRepository:
        file_name = (
            VALIDATION_FILE_NAME if execution_context.is_validation else PRODUCTION_FILE_NAME
        )
        return cls(store_dir=store_dir, file_name=file_name, ttl_seconds=_TTL_SECONDS)

    def get(self, record_id: str) -> DailyNotificationPriorityRecord | None:
        return self._store.get(record_id)

    def upsert(self, record: DailyNotificationPriorityRecord) -> None:
        self._store.upsert(record)
