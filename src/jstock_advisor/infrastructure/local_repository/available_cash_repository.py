"""AvailableCashの永続化(Issue #584、#128 A1)。

ownerを主キーとする1レコード/owner方式。初回作成は`insert_if_absent()`
(PutItem + attribute_not_exists相当)、更新は`replace_if_raw_matches()`
(生JSONの完全一致によるCAS。#193/watchlist_rotation_state.pyと同じ
`holding_decision_runtime_config_repository.py`の設計を踏襲。ただし
AvailableCashにはconfig_version相当のnative属性を持たせていないため、
呼び出し側が`get_raw()`で読んだ生JSONをそのまま`replace_if_raw_matches()`へ
渡す設計とし、DynamoDB専用の別実装〔_update_dynamodb相当〕を追加で作らない)。

A1ではtrade連携atomicityは実装しない。単独のレコードを安全に保存・取得できる
ことのみが必要(Issue #584本文どおり)。
"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.available_cash import AvailableCash
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store

_TABLE_FILE_NAME = "available_cash.json"


class AvailableCashRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[AvailableCash] = build_collection_store(
            AvailableCash, _TABLE_FILE_NAME, "owner", store_dir
        )

    def get(self, owner: str) -> AvailableCash | None:
        """レコードが無い場合はNone(UNKNOWN/NOT_INITIALIZED)を返す。

        0円のレコードが存在する場合はNoneではなくavailable_cash=0の
        AvailableCashを返す(未初期化と0円を区別する。Issue #584 T4)。
        """
        return self._store.get(owner)

    def get_raw(self, owner: str) -> str | None:
        """楽観的並行性制御(`replace_if_raw_matches()`)のための生JSON取得。"""
        return self._store.get_raw_data(owner)

    def initialize(self, record: AvailableCash) -> bool:
        """初回作成。既に存在する場合はFalseを返す(既存値は変更しない)。"""
        return self._store.insert_if_absent(record)

    def replace_if_raw_matches(
        self, owner: str, expected_raw_data: str, record: AvailableCash
    ) -> bool:
        """expected_raw_dataが現在値と一致する場合のみ更新する(楽観ロック)。

        一致しない場合はFalseを返す(自動リトライしない。呼び出し側が最新値を
        再取得して判断する。Issue #584 T11)。
        """
        return self._store.replace_if_raw_matches(owner, expected_raw_data, record)

    def list_all(self) -> list[AvailableCash]:
        return self._store.list_all()
