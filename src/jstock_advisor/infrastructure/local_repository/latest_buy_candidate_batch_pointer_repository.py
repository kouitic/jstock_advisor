"""最新完了BUY候補batchポインタのリポジトリ(LINE UI第二弾、2026-08)。"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.buy_candidate_batch_pointer import (
    DEFAULT_POINTER_ID,
    LatestBuyCandidateBatchPointer,
)
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store

_TABLE_FILE_NAME = "buy_candidate_batch_completion.json"


class LatestBuyCandidateBatchPointerRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[LatestBuyCandidateBatchPointer] = build_collection_store(
            LatestBuyCandidateBatchPointer, _TABLE_FILE_NAME, "pointer_id", store_dir
        )

    def get(self) -> LatestBuyCandidateBatchPointer | None:
        return self._store.get(DEFAULT_POINTER_ID)

    def update_latest_completed(self, pointer: LatestBuyCandidateBatchPointer) -> None:
        """単一行を無条件に上書きする。書き込みはNORMAL運用のfinalize時のみ
        (1回のfinalizeにつき高々1回)であり、同時書き込みの競合は想定しない
        ため楽観ロックは持たせない(過剰設計を避ける)。"""
        self._store.upsert(pointer)
