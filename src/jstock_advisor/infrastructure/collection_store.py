"""ローカルJSON/DynamoDBの両バックエンドが実装する共通インターフェースと、
実行環境に応じてどちらを使うか決定するファクトリ関数。

Lambda環境(AWS_LAMBDA_FUNCTION_NAME環境変数が設定されている)ではDynamoDB、
それ以外(ローカルCLI)ではJSONファイルを自動的に選択する。リポジトリ層の
コードはこのファクトリを呼ぶだけで、呼び出し側の変更なしにストレージを
差し替えられる(要求仕様: DynamoDB移行時にリポジトリ層のインターフェースが
変わらないようにする、という当初設計方針の実現)。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from jstock_advisor.infrastructure.local_repository.json_store import JsonCollectionStore

_TABLE_PREFIX_ENV = "DYNAMODB_TABLE_PREFIX"
_DEFAULT_TABLE_PREFIX = "jstock"


class CollectionStore[T: BaseModel](Protocol):
    def list_all(self) -> list[T]: ...
    def get(self, item_id: str) -> T | None: ...
    def upsert(self, item: T) -> None: ...
    def upsert_many(self, new_items: Iterable[T]) -> None: ...
    def delete(self, item_id: str) -> bool: ...
    def find(self, predicate: Callable[[T], bool]) -> list[T]: ...


def running_on_lambda() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def resolve_table_name(file_name: str) -> str:
    prefix = os.environ.get(_TABLE_PREFIX_ENV, _DEFAULT_TABLE_PREFIX)
    base = file_name.removesuffix(".json")
    return f"{prefix}-{base}"


def build_collection_store[T: BaseModel](
    model_type: type[T],
    file_name: str,
    id_field: str,
    store_dir: Path | None = None,
) -> CollectionStore[T]:
    if running_on_lambda():
        from jstock_advisor.infrastructure.aws.dynamodb_store import DynamoDbCollectionStore

        return DynamoDbCollectionStore(model_type, resolve_table_name(file_name), id_field)
    return JsonCollectionStore(model_type, file_name, id_field, store_dir)
