"""DecisionSnapshot(判定精度向上機能Phase A)のローカルリポジトリ。

コードレビュー対応: DecisionSnapshotはinsert-onlyとする(一度保存された記録は
後から絶対に上書きしない)。upsert()は使用せず、CollectionStore.insert_if_absent()
(DynamoDBでは条件付き書き込みによる原子的なinsert-only)のみを使う。
"""

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

    def insert_if_absent(self, decision: DecisionSnapshot) -> bool:
        """decision_idが未存在の場合のみ追加してTrue、既に存在すればFalse。

        DecisionSnapshotに対してupsert()は使用しない(insert-only保証)。
        """
        return self._store.insert_if_absent(decision)
