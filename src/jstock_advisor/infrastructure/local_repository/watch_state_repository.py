"""NEAR BUY監視状態(WatchState)のローカルリポジトリ(BUY候補裾野拡大機能2026-08)。"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.enums import WatchType
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.watch_state import WatchState, build_watch_id
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store

PRODUCTION_FILE_NAME = "watch_states.json"
# 通知検証モード機能との整合(2026-08): VALIDATION実行では本番のWatchStateを
# 一切書き換えないよう、既存のRecommendationRepository等と同じ「別ファイル名+
# 短TTL」パターンを適用する。
VALIDATION_FILE_NAME = "validation_watch_states.json"
_VALIDATION_TTL_SECONDS = 2 * 60 * 60


class WatchStateRepository:
    def __init__(
        self,
        store_dir: Path | None = None,
        file_name: str = PRODUCTION_FILE_NAME,
        ttl_seconds: int | None = None,
    ) -> None:
        self._store: CollectionStore[WatchState] = build_collection_store(
            WatchState, file_name, "watch_id", store_dir, ttl_seconds=ttl_seconds
        )

    @classmethod
    def for_execution_context(
        cls, execution_context: ExecutionContext, store_dir: Path | None = None
    ) -> WatchStateRepository:
        if execution_context.is_validation:
            return cls(
                store_dir=store_dir,
                file_name=VALIDATION_FILE_NAME,
                ttl_seconds=_VALIDATION_TTL_SECONDS,
            )
        return cls(store_dir=store_dir, file_name=PRODUCTION_FILE_NAME, ttl_seconds=None)

    def get_active(self, stock_code: str, watch_type: WatchType) -> WatchState | None:
        """指定した銘柄・種別の、まだ終了していない(ended_at is None)WatchStateを返す。"""
        watch_id = build_watch_id(stock_code, watch_type)
        state = self._store.get(watch_id)
        if state is None or state.ended_at is not None:
            return None
        return state

    def upsert(self, state: WatchState) -> None:
        self._store.upsert(state)

    def list_active_by_stock(self, stock_code: str) -> list[WatchState]:
        return self._store.find(lambda s: s.stock_code == stock_code and s.ended_at is None)

    def list_all(self) -> list[WatchState]:
        return self._store.list_all()
