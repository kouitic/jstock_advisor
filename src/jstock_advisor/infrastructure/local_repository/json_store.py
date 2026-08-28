"""pydanticモデルのコレクションをJSONファイルへ永続化する汎用ストア。

MVPではDynamoDBの代わりにローカルJSONファイルを使用する。CRUD操作のみを
公開することで、将来DynamoDB実装(infrastructure/aws)に差し替えても
呼び出し側(リポジトリクラス)のインターフェースが変わらないようにする。
単一ユーザー・単一プロセスでのCLI利用を前提とし、同時書き込みの排他制御は行わない。
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path

from pydantic import BaseModel

DEFAULT_STORE_DIR = Path(__file__).resolve().parents[4] / "data" / "local_store"


class JsonCollectionStore[T: BaseModel]:
    def __init__(
        self,
        model_type: type[T],
        file_name: str,
        id_field: str,
        store_dir: Path | None = None,
    ) -> None:
        self._model_type = model_type
        self._id_field = id_field
        self._path = (store_dir or DEFAULT_STORE_DIR) / file_name
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _read_all(self) -> dict[str, T]:
        if not self._path.exists():
            return {}
        with self._path.open(encoding="utf-8") as f:
            raw = json.load(f)
        return {str(item[self._id_field]): self._model_type.model_validate(item) for item in raw}

    def _write_all(self, items: dict[str, T]) -> None:
        payload = [json.loads(item.model_dump_json()) for item in items.values()]
        fd, tmp_path = tempfile.mkstemp(dir=self._path.parent, prefix=".tmp_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def list_all(self) -> list[T]:
        return list(self._read_all().values())

    def get(self, item_id: str) -> T | None:
        return self._read_all().get(item_id)

    def get_consistent(self, item_id: str) -> T | None:
        """ローカルJSONは常に最新のファイル内容を読むため、get()と同じでよい。"""
        return self.get(item_id)

    def get_raw_data(self, item_id: str) -> str | None:
        """ローカル実装はDynamoDBへ書き込まないため、get()相当の
        model_dump_json()を返す(楽観ロックの実利用はDynamoDB実装のみ)。"""
        item = self.get(item_id)
        return item.model_dump_json() if item is not None else None

    def upsert(self, item: T) -> None:
        items = self._read_all()
        item_id = str(getattr(item, self._id_field))
        items[item_id] = item
        self._write_all(items)

    def upsert_many(self, new_items: Iterable[T]) -> None:
        items = self._read_all()
        for item in new_items:
            items[str(getattr(item, self._id_field))] = item
        self._write_all(items)

    def delete(self, item_id: str) -> bool:
        items = self._read_all()
        if item_id not in items:
            return False
        del items[item_id]
        self._write_all(items)
        return True

    def find(self, predicate: Callable[[T], bool]) -> list[T]:
        return [item for item in self._read_all().values() if predicate(item)]

    def upsert_with_index_attributes(self, item: T, index_attributes: dict[str, str]) -> None:
        """ローカルJSONにGSI概念は無いため、index_attributesを無視して通常のupsertと同一。"""
        self.upsert(item)

    def query_by_index(self, index_name: str, key_name: str, key_value: str) -> list[T]:
        """ローカルJSONにGSI概念は無いため、index_nameは無視しfind()相当で絞り込む。"""
        return self.find(lambda item: str(getattr(item, key_name)) == key_value)

    def get_many(self, item_ids: Iterable[str]) -> dict[str, T]:
        """対象確認機能2026-08向け。既存の全件読み込みから該当IDだけを取り出す
        だけであり、追加I/Oは発生しない(DynamoDB実装のBatchGetItem相当の
        分割・リトライ・失敗概念はローカル実装には存在しない)。"""
        items = self._read_all()
        return {item_id: items[item_id] for item_id in dict.fromkeys(item_ids) if item_id in items}

    def insert_if_absent(self, item: T) -> bool:
        """既存があれば触らずFalse、無ければ追加してTrue。

        単一プロセスでのCLI利用を前提とし(モジュール冒頭のdocstring参照)、
        read→writeの間の排他制御は行わない(check-then-act)。
        """
        items = self._read_all()
        item_id = str(getattr(item, self._id_field))
        if item_id in items:
            return False
        items[item_id] = item
        self._write_all(items)
        return True

    def replace_if_raw_matches(self, item_id: str, expected_raw_data: str, item: T) -> bool:
        """現在値のmodel_dump_json()がexpected_raw_dataと一致する場合のみ置換
        (CAS。Issue #17)。単一プロセス前提のread-compare-write
        (insert_if_absent()と同じ前提)。"""
        items = self._read_all()
        current = items.get(item_id)
        if current is None or current.model_dump_json() != expected_raw_data:
            return False
        items[item_id] = item
        self._write_all(items)
        return True

    def delete_if_raw_matches(self, item_id: str, expected_raw_data: str) -> bool:
        """現在値のmodel_dump_json()がexpected_raw_dataと一致する場合のみ削除
        (条件付き削除。Issue #17)。単一プロセス前提のread-compare-write。"""
        items = self._read_all()
        current = items.get(item_id)
        if current is None or current.model_dump_json() != expected_raw_data:
            return False
        del items[item_id]
        self._write_all(items)
        return True
