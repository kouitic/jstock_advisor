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
    def insert_if_absent(self, item: T) -> bool:
        """既存の項目がなければ追加してTrue、既に存在すればFalse(冪等な新規追加専用)。

        DynamoDB実装は条件付き書き込みで原子的にこれを保証する。ウォッチリスト
        自動追加機能のように、永続データへの重複追加を確実に防ぎたい場合に使う。
        """
        ...
    def get_consistent(self, item_id: str) -> T | None:
        """get()のstrongly consistent read版。

        DynamoDB実装はConsistentRead=Trueで読む(結果整合性読み取りによる
        一時的なNoneを避ける)。JSON実装はget()と同じ(ローカルJSONは常に
        最新を読むため区別不要)。insert_if_absent()の競合後にレコード内容を
        比較する等、強い整合性が必要な限定的な用途でのみ使うこと(通常のget()を
        一律これへ置き換えない。DynamoDBの読み取りコスト増加・挙動変更を避けるため)。
        """
        ...


def running_on_lambda() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def resolve_table_name(file_name: str) -> str:
    prefix = os.environ.get(_TABLE_PREFIX_ENV, _DEFAULT_TABLE_PREFIX)
    base = file_name.removesuffix(".json")
    return f"{prefix}-{base}"


# --- 候補ユニバース本格対応(2026-08)向け、S3/ローカルファイルの二重化ヘルパー ---
# running_on_lambda()/resolve_table_name()と同じ「実行環境に応じてバックエンドを
# 選択する」パターンを、DynamoDBではなくS3(候補ユニバースキャッシュ)へ適用する。
# 実際のオブジェクト読み書き・staging/current/archiveレイアウトの構築は
# services/candidate_universe_downloader.py側の責務とし、このモジュールは
# 環境変数解決とローカルキャッシュディレクトリの決定のみを担う。

_CANDIDATE_UNIVERSE_BUCKET_ENV = "CANDIDATE_UNIVERSE_CACHE_BUCKET"


def resolve_candidate_universe_bucket() -> str:
    """候補ユニバースキャッシュ用S3バケット名(Lambda環境でのみ呼ぶこと)。"""
    bucket = os.environ.get(_CANDIDATE_UNIVERSE_BUCKET_ENV)
    if not bucket:
        raise RuntimeError(f"{_CANDIDATE_UNIVERSE_BUCKET_ENV}環境変数が設定されていません")
    return bucket


def resolve_candidate_universe_local_cache_dir() -> Path:
    """ローカル(非Lambda)環境での候補ユニバースキャッシュ保存先。

    ローカルCLIは常にこのディレクトリのみを読み書きし、本番S3へは(環境変数トリック
    等があっても)一切アクセスしない(6節: 誤操作防止のため意図的な設計)。
    """
    return Path(__file__).resolve().parents[3] / "data" / "cache" / "candidate_universe"


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
