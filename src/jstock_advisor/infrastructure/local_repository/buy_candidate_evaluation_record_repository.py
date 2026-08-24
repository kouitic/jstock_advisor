"""BuyCandidateEvaluationRecordのローカルリポジトリ(買い候補サマリー表示改修2026-08)。

将来のLINE詳細理由照会機能に向けた参照用ストアであり、既存のRecommendation/
DecisionSnapshot/NotificationLog/AuditLogTableの代替ではない。無期限に増加させない
ため既定90日のTTLを設定する(config.notification.buy_candidatesで調整可能)。
"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.buy_candidate_evaluation_record import (
    BuyCandidateEvaluationRecord,
)
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store

_TABLE_FILE_NAME = "buy_candidate_evaluation_records.json"
DEFAULT_TTL_SECONDS = 90 * 24 * 60 * 60


class BuyCandidateEvaluationRecordRepository:
    def __init__(
        self, store_dir: Path | None = None, ttl_seconds: int | None = DEFAULT_TTL_SECONDS
    ) -> None:
        self._store: CollectionStore[BuyCandidateEvaluationRecord] = build_collection_store(
            BuyCandidateEvaluationRecord,
            _TABLE_FILE_NAME,
            "evaluation_id",
            store_dir,
            ttl_seconds=ttl_seconds,
        )

    def get(self, evaluation_id: str) -> BuyCandidateEvaluationRecord | None:
        return self._store.get(evaluation_id)

    def list_by_stock(self, stock_code: str) -> list[BuyCandidateEvaluationRecord]:
        items = self._store.find(lambda r: r.stock_code == stock_code)
        return sorted(items, key=lambda r: r.evaluated_at)

    def upsert(self, record: BuyCandidateEvaluationRecord) -> None:
        self._store.upsert(record)
