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
