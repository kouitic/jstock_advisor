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

    def get_active_with_raw(
        self, stock_code: str, watch_type: WatchType
    ) -> tuple[WatchState, str] | None:
        """`get_active()`と同じものを、CASに使う生JSONと組で返す(Issue #71 F-C13)。

        `replace_if_raw_matches()`へ渡す`expected_raw_data`は、`get_raw_data()`で
        取得した値をそのまま使う必要がある(取得したモデルを`model_dump_json()`
        し直すとバイト単位の一致が保証されない。CollectionStoreのdocstring)。
        そのため「読んだ値」と「その時点の生JSON」を**同じ呼び出しで**返す。
        """
        watch_id = build_watch_id(stock_code, watch_type)
        state = self._store.get(watch_id)
        if state is None or state.ended_at is not None:
            return None
        raw = self._store.get_raw_data(watch_id)
        if raw is None:
            return None
        return state, raw

    def get_with_raw(self, watch_id: str) -> tuple[WatchState, str] | None:
        """終了済みも含めて取得する(CAS失敗後の再読込用。Issue #71 F-C13)。

        `get_active_with_raw()`と違い`ended_at`で絞り込まない。CASが失敗した
        理由が「別実行が終了させたから」なのかを**判別するため**であり、
        終了済みを弾いてしまうと判別できなくなる。
        """
        state = self._store.get(watch_id)
        if state is None:
            return None
        raw = self._store.get_raw_data(watch_id)
        if raw is None:
            return None
        return state, raw

    def replace_if_raw_matches(self, expected_raw_data: str, state: WatchState) -> bool:
        """生JSONが一致する場合のみ置き換える(CAS。Issue #71 F-C13)。

        既存の`upsert()`は残す(新規作成とテスト用。呼び出し側の移行は段階的)。
        """
        return self._store.replace_if_raw_matches(state.watch_id, expected_raw_data, state)

    def upsert(self, state: WatchState) -> None:
        self._store.upsert(state)

    def list_active_by_stock(self, stock_code: str) -> list[WatchState]:
        return self._store.find(lambda s: s.stock_code == stock_code and s.ended_at is None)

    def list_all(self) -> list[WatchState]:
        return self._store.list_all()
