"""監査ログのローカルリポジトリ(要求仕様13節・21節)。"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.audit import AuditLogEntry
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store


class AuditLogRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[AuditLogEntry] = build_collection_store(
            AuditLogEntry, "audit_log.json", "audit_id", store_dir
        )

    def list_all(self) -> list[AuditLogEntry]:
        return self._store.list_all()

    def list_by_stock(self, stock_code: str) -> list[AuditLogEntry]:
        items = self._store.find(lambda e: e.stock_code == stock_code)
        return sorted(items, key=lambda e: e.timestamp)

    def list_by_decision_type(self, decision_type: str) -> list[AuditLogEntry]:
        items = self._store.find(lambda e: e.decision_type == decision_type)
        return sorted(items, key=lambda e: e.timestamp)

    def get(self, audit_id: str) -> AuditLogEntry | None:
        return self._store.get(audit_id)

    def save(self, entry: AuditLogEntry) -> None:
        self._store.upsert(entry)

    def save_if_absent(self, entry: AuditLogEntry) -> bool:
        """運用ハードニング第3弾3節: 既にaudit_idが存在すればFalse(何もしない)、
        無ければ保存してTrue(冪等な新規記録専用)。決定的なaudit_idと組み合わせて
        呼び出し側の重複記録防止に使う(CollectionStore.insert_if_absentの
        条件付き書き込みで原子的に保証される)。
        """
        return self._store.insert_if_absent(entry)
