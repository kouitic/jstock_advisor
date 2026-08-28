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
    def get_raw_data(self, item_id: str) -> str | None:
        """DynamoDB実装は`data`属性の生JSON文字列をそのまま返す(モデルを経由した
        再シリアライズを行わない)。LINEボタン起点会話型UIの楽観ロック
        (TransactWriteItemsのConditionExpression: #data = :expected_data)が、
        計画構築時点で実際にDynamoDBへ保存されているバイト列と完全一致する値を
        条件に使うために必要(再シリアライズ結果は、フィールド順序・Decimalの
        表現等が偶然一致しない限りバイト単位では一致するとは限らないため、
        `get()`で取得したモデルを`model_dump_json()`し直した値は使わない)。
        JSON実装ではDynamoDBへの書き込みを行わないため、get()相当の
        model_dump_json()を返す(実利用はDynamoDB実装のみ)。項目が無ければNone。
        """
        ...
    def upsert_with_index_attributes(self, item: T, index_attributes: dict[str, str]) -> None:
        """upsert()に加え、GSI等でのクエリ用にトップレベル属性も書き込む
        (LINE UI第二弾・対象確認機能2026-08追加)。

        DynamoDB実装のみがindex_attributesを実際にトップレベル属性として
        書き込む。JSON実装はindex_attributesを無視してupsert(item)と同じ
        動作(ローカルJSONにはGSIという概念が存在しないため)。batch_id等の
        特定の属性名をこのProtocol自体に持ち込まない、汎用の拡張ポイントとする。
        """
        ...

    def query_by_index(self, index_name: str, key_name: str, key_value: str) -> list[T]:
        """指定したGSI(index_name)のHASHキー(key_name)がkey_valueに一致する
        項目をQueryで取得する(LINE UI第二弾・対象確認機能2026-08追加)。

        DynamoDB実装のみが実際にGSIをQueryする(効率的な検索用)。ローカルJSON
        実装はindex_nameを無視し、find(lambda item: getattr(item, key_name) ==
        key_value)と同じ全件フィルタ結果を返す(ローカルにGSIという概念が
        存在しないため)。対象のGSIを持たないテーブルでこのメソッドを呼ぶと
        DynamoDB実装側で例外になるため、呼び出し側は対応するGSIを持つ
        リポジトリでのみ使用すること。
        """
        ...

    def replace_if_raw_matches(self, item_id: str, expected_raw_data: str, item: T) -> bool:
        """保存中の`data`生JSONがexpected_raw_dataと完全一致する場合のみitemで
        置き換えてTrue、一致しない・項目が無い場合は何もせずFalse(CAS。
        LINE通知dedup原子化 Issue #17で追加)。

        DynamoDB実装はConditionExpression「#data = :expected_data」の条件付き
        put_itemで原子的に行う(watchlist_rotation_state.py等で実績のある
        楽観ロックパターンの汎用化)。expected_raw_dataにはget_raw_data()で
        取得した値をそのまま渡すこと(get()したモデルのmodel_dump_json()し直しは
        バイト単位一致が保証されないため使わない)。ローカルJSON実装は
        単一プロセス前提のread-compare-write(insert_if_absent()と同じ前提)。
        """
        ...

    def delete_if_raw_matches(self, item_id: str, expected_raw_data: str) -> bool:
        """保存中の`data`生JSONがexpected_raw_dataと完全一致する場合のみ削除して
        True、一致しない・項目が無い場合は何もせずFalse(条件付き削除。
        LINE通知dedup原子化 Issue #17で追加。claim所有者だけがpush失敗時の
        補償deleteを行えるようにするために使う)。
        """
        ...

    def get_many(self, item_ids: Iterable[str]) -> dict[str, T]:
        """複数IDを一括取得する(対象確認機能2026-08、N+1回避)。

        戻り値は取得できたitem_idのみを含む辞書(get()と同じく、存在しない
        IDは戻り値に単純に含まれない。Noneで表現しない)。重複したitem_idは
        1件分として扱う。

        DynamoDB実装はBatchGetItem(最大100件/リクエスト)を使い、1件ずつ
        GetItemを呼ぶ実装へフォールバックしない。UnprocessedKeysが返った
        場合は`_batch_write_with_retry()`(batch_tracker.py)と同じ指数
        バックオフ+ジッターで再送し、規定回数の再送後もUnprocessedKeysが
        残る場合はRuntimeErrorを送出する(実際に存在しないIDとして静かに
        省略しない。取得失敗と「存在しない」を混同しないため)。

        ローカルJSON実装は既存の全件読み込みから該当IDだけを取り出す
        (追加I/Oなし、失敗の概念自体が無い)。
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
    ttl_seconds: int | None = None,
) -> CollectionStore[T]:
    """ttl_secondsはDynamoDBバックエンド向けの任意引数(通知検証モード機能2026-08追加、
    ValidationRecommendationsTable等の使い捨てテーブル専用)。ローカルJSON実装には
    TTL概念が無いため無視される。"""
    if running_on_lambda():
        from jstock_advisor.infrastructure.aws.dynamodb_store import DynamoDbCollectionStore

        return DynamoDbCollectionStore(
            model_type, resolve_table_name(file_name), id_field, ttl_seconds=ttl_seconds
        )
    return JsonCollectionStore(model_type, file_name, id_field, store_dir)
