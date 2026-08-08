"""DecisionSnapshot(判定精度向上機能Phase A)のローカルリポジトリ。"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.decision_snapshot import DecisionSnapshot
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store


class DecisionSnapshotRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[DecisionSnapshot] = build_collection_store(
            DecisionSnapshot, "decision_snapshots.json", "decision_id", store_dir
        )

    def list_all(self) -> list[DecisionSnapshot]:
        return self._store.list_all()

    def get(self, decision_id: str) -> DecisionSnapshot | None:
        return self._store.get(decision_id)

    def save(self, decision: DecisionSnapshot) -> None:
        self._store.upsert(decision)
