"""DynamoDBバックエンドの汎用コレクションストア。

ローカルのJsonCollectionStoreと同一のインターフェース(list_all/get/upsert/
upsert_many/delete/find)を提供し、Lambda環境ではリポジトリ層のコード変更
無しにストレージをDynamoDBへ差し替えられるようにする(infrastructure/
collection_store.pyのファクトリ経由で選択される)。

各アイテムは丸ごとJSON文字列としてdata属性に保存する。ローカルJSON実装と
同じシリアライズ経路(model_dump_json/model_validate_json)を使うことで、
Decimal等の型変換ロジックを一本化し挙動の差異を避ける。パーティションキーは
id_fieldの値をそのまま使う単純な設計とする(単一ユーザー運用規模のため、
アクセスパターンごとのGSI設計は行わずスキャンで十分とする)。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import TYPE_CHECKING, Any, cast

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel

from jstock_advisor.infrastructure.record_failure_policy import (
    ItemIdDisclosure,
    RecordFailureCollector,
    RecordFailurePolicy,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

# BatchGetItemのチャンク・リトライ仕様(対象確認機能2026-08、N+1回避)。
# batch_tracker.py::_batch_write_with_retry()(BatchWriteItemのUnprocessedItems
# 再送)と同じ指数バックオフ+ジッターのパラメータをそのまま踏襲する(この
# リポジトリ内でのバッチ系DynamoDB API呼び出しの再送方針を1本化するため)。
_BATCH_GET_MAX_KEYS_PER_REQUEST = 100  # BatchGetItemの1リクエストあたりの上限(AWS仕様)
_BATCH_GET_BASE_DELAY_SECONDS = 0.5
_BATCH_GET_MAX_DELAY_SECONDS = 5.0
_BATCH_GET_MAX_RETRIES = 5


def _chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def to_dynamo_item(
    model: BaseModel, id_field: str, ttl_seconds: int | None = None
) -> dict[str, Any]:
    """`{id_field: id, "data": model.model_dump_json()}`形式のPUT用アイテムを構築する。

    DynamoDbCollectionStore._to_item()と全く同じ形式(LINEボタン起点会話型UI・
    実装プランv2 3節)。conversation_commit.pyがTransactWriteItems用のPut/Update
    アイテムを組み立てる際、通常のリポジトリ経由で読み書きする場合と完全に
    同一のシリアライズ形式であることを保証するために公開する。
    """
    item_id = str(getattr(model, id_field))
    item: dict[str, Any] = {id_field: item_id, "data": model.model_dump_json()}
    if ttl_seconds is not None:
        item["ttl"] = int(time.time()) + ttl_seconds
    return item


class DynamoDbCollectionStore[T: BaseModel]:
    def __init__(
        self,
        model_type: type[T],
        table_name: str,
        id_field: str,
        ttl_seconds: int | None = None,
        failure_policy: RecordFailurePolicy = RecordFailurePolicy.STRICT,
        item_id_disclosure: ItemIdDisclosure = ItemIdDisclosure.HASH,
    ) -> None:
        """ttl_secondsを指定すると、保存する各アイテムへDynamoDB Native TTL用の
        ttl属性(現在時刻+ttl_seconds、UNIX秒)を付与する(通知検証モード機能
        2026-08追加。使い捨てテーブル向けの任意機能で、未指定(既定)なら
        既存の全リポジトリと同様ttl属性は付与されない)。
        """
        self._model_type = model_type
        self._id_field = id_field
        self._ttl_seconds = ttl_seconds
        self._table_name = table_name
        self._resource = boto3.resource("dynamodb")
        self._table: Table = self._resource.Table(table_name)
        self._failure_policy = failure_policy
        self._item_id_disclosure = item_id_disclosure

    def _to_item(self, model: T) -> dict[str, Any]:
        return to_dynamo_item(model, self._id_field, self._ttl_seconds)

    def _from_item(self, item: dict[str, Any]) -> T:
        return self._model_type.model_validate_json(item["data"])

    def _collector(self) -> RecordFailureCollector:
        return RecordFailureCollector(
            collection=self._table_name,
            policy=self._failure_policy,
            item_id_disclosure=self._item_id_disclosure,
        )

    def _decode_page(
        self, raw_items: Iterable[dict[str, Any]], collector: RecordFailureCollector
    ) -> Iterator[T]:
        """全件経路(Scan / Query / BatchGetItem)の 1 ページ分をデコードする。

        Issue #63 PR-2(A-U1b)。失敗の扱いを collector のポリシーへ委ねる。
        `STRICT` では最初の失敗で元の例外がそのまま送出されるため、
        **呼び出し側から見た挙動は現行と同じ**(打ち切り位置も変わらない)。

        `get()` / `get_consistent()` は要求した 1 件しか検証しないため、
        本経路ではなく `_decode_one()` を通す(1 件の不正が他の id の取得を
        妨げないという既存の性質を変えない)。
        """
        for raw in raw_items:
            item_id = str(raw.get(self._id_field, ""))
            try:
                yield self._from_item(raw)
            except Exception as error:  # noqa: BLE001 - ポリシーに委ねるため広く捕捉する
                collector.handle(item_id, error)

    def _decode_one(self, item_id: str, raw: dict[str, Any]) -> T | None:
        """主キー指定で取得した 1 件をデコードする(Issue #63 PR-3a)。

        `get()` / `get_consistent()` 用。全件経路(`_decode_page()`)と違い、
        検証するのは要求された 1 件だけであり、他の id の取得には影響しない。

            STRICT(既定)  元の例外をそのまま送出する。**現行と同一の挙動**
            LENIENT       失敗を記録して `None` を返す。呼び出し側から見ると
                          「その id が無い」= cache miss と同じであり、
                          provider から取り直して上書きすれば自然に解消する

        **なぜ主キー経路にも接続が要るか。** cache の Production 読み取りは
        5 経路とも主キー指定の `get()` である(`watchlist_data_cache` の
        `get_or_fetch`、EDINET の 3 repository)。PR-2 では `_decode_page()` を
        全件経路(Scan / Query / BatchGetItem)にしか入れていないため、
        `LENIENT` を宣言しても Lambda では効かず、壊れた 1 件が従来どおり
        例外を送出していた(ローカル JSON 実装は `_read_all()` を経由するため
        効いており、backend によって挙動が食い違っていた)。
        """
        collector = self._collector()
        try:
            return self._from_item(raw)
        except Exception as error:  # noqa: BLE001 - ポリシーに委ねるため広く捕捉する
            if self._failure_policy is RecordFailurePolicy.FAIL_SAFE_SUPPRESS:
                # 「判定不能」を伝える戻り値がこの signature には無い。
                # `None` を返すと呼び出し側は「レコードが無い」と読み、
                # FAIL_SAFE_SUPPRESS が防ごうとしている**誤った判断**
                # (notification_log なら重複送信)をそのまま起こす。
                # 伝えられない以上は fail-closed とし、元の例外を送出する。
                raise
            collector.handle(item_id, error)
            return None

    def list_all(self) -> list[T]:
        return list(self.iter_all())

    def iter_all(self) -> Iterator[T]:
        """Scanのページを1つずつ処理し、1件ずつyieldする(Issue #113)。

        `list_all()`と異なり全ページを`list`へ保持しないため、ピークメモリは
        「1ページ分(最大1MBの生データ + そのdeserialize結果)」に有界となる。
        呼び出し側がページ間で参照を保持しなければ、直前のページは次ページの
        取得前に解放される。

        `list_all()`はこのメソッドの`list()`化として実装しており、
        列挙順・件数・内容が両者で一致することが構造的に保証される。
        """
        collector = self._collector()
        scan_kwargs: dict[str, Any] = {}
        while True:
            response = self._table.scan(**scan_kwargs)
            yield from self._decode_page(response.get("Items", []), collector)
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key

    def get(self, item_id: str) -> T | None:
        response = self._table.get_item(Key={self._id_field: item_id})
        item = response.get("Item")
        return self._decode_one(item_id, item) if item is not None else None

    def get_raw_data(self, item_id: str) -> str | None:
        """`data`属性の生JSON文字列をそのまま返す(モデルを経由した再シリアライズを
        行わない)。楽観ロックのConditionExpression(#data = :expected_data)に
        使う値は、実際にDynamoDBへ保存されているバイト列と完全一致している
        必要があるため、_from_item()を経由しない。

        モデル検証を通らないため、`failure_policy`の対象外である
        (Issue #63 PR-3a。検証しない経路に「検証失敗の扱い」は存在しない)。"""
        response = self._table.get_item(Key={self._id_field: item_id})
        item = response.get("Item")
        return str(item["data"]) if item is not None else None

    def get_consistent(self, item_id: str) -> T | None:
        """get()のstrongly consistent read版(ConsistentRead=True)。

        insert_if_absent()の競合後にレコード内容を比較する等、結果整合性読み取り
        による一時的なNoneを避けたい限定用途でのみ使うこと(通常のget()はコスト・
        挙動を変えないため結果整合性読み取りのまま維持する)。
        """
        response = self._table.get_item(Key={self._id_field: item_id}, ConsistentRead=True)
        item = response.get("Item")
        return self._decode_one(item_id, item) if item is not None else None

    def upsert(self, item: T) -> None:
        self._table.put_item(Item=self._to_item(item))

    def apply_batch(self, delete_ids: Iterable[str], puts: Iterable[T]) -> None:
        """削除と追加/更新を順に適用する(Issue #61 Phase B2)。

        **本実装は原子的ではない。** DynamoDBで原子性が必要な経路は
        TransactWriteItems(holding_replacement_commit.py)を使うこと。
        本メソッドはローカルJSON実装とのインターフェース互換のために存在する。
        """
        for item_id in delete_ids:
            self.delete(str(item_id))
        for item in puts:
            self.upsert(item)

    def upsert_many(self, new_items: Iterable[T]) -> None:
        with self._table.batch_writer() as batch:
            for item in new_items:
                batch.put_item(Item=self._to_item(item))

    def delete(self, item_id: str) -> bool:
        response = self._table.delete_item(Key={self._id_field: item_id}, ReturnValues="ALL_OLD")
        return "Attributes" in response

    def find(self, predicate: Callable[[T], bool]) -> list[T]:
        return [item for item in self.list_all() if predicate(item)]

    def upsert_with_index_attributes(
        self, item: T, index_attributes: Mapping[str, str | int]
    ) -> None:
        dynamo_item = self._to_item(item)
        dynamo_item.update(index_attributes)
        self._table.put_item(Item=dynamo_item)

    def query_by_index(self, index_name: str, key_name: str, key_value: str) -> list[T]:
        from boto3.dynamodb.conditions import Key

        collector = self._collector()
        items: list[T] = []
        query_kwargs: dict[str, Any] = {
            "IndexName": index_name,
            "KeyConditionExpression": Key(key_name).eq(key_value),
        }
        while True:
            response = self._table.query(**query_kwargs)
            items.extend(self._decode_page(response.get("Items", []), collector))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key
        return items

    def insert_if_absent(self, item: T) -> bool:
        """条件付きput_item(attribute_not_exists)で原子的に新規追加のみを許可する。

        既に同一キーの項目が存在すればConditionalCheckFailedExceptionを捕捉し
        Falseを返す(既存項目は一切変更しない)。
        """
        try:
            self._table.put_item(
                Item=self._to_item(item),
                ConditionExpression="attribute_not_exists(#pk)",
                ExpressionAttributeNames={"#pk": self._id_field},
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def replace_if_raw_matches(self, item_id: str, expected_raw_data: str, item: T) -> bool:
        """`data`生JSONがexpected_raw_dataと完全一致する場合のみ原子的に置換する
        (CAS。Issue #17。watchlist_rotation_state.py等の#data = :expected_data
        楽観ロックパターンの汎用化)。条件不成立(値の不一致・項目の不存在)は
        ConditionalCheckFailedExceptionを捕捉しFalseを返す。それ以外の
        ClientError(スロットリング等の基盤エラー)はそのまま送出する。"""
        del item_id  # PKはitem側のid_field値から導出される(引数はProtocol整合用)
        try:
            self._table.put_item(
                Item=self._to_item(item),
                ConditionExpression="#data = :expected_data",
                ExpressionAttributeNames={"#data": "data"},
                ExpressionAttributeValues={":expected_data": expected_raw_data},
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def delete_if_raw_matches(self, item_id: str, expected_raw_data: str) -> bool:
        """`data`生JSONがexpected_raw_dataと完全一致する場合のみ原子的に削除する
        (条件付き削除。Issue #17)。条件不成立はFalse、その他のClientErrorは
        そのまま送出する(replace_if_raw_matches()と同じ規約)。"""
        try:
            self._table.delete_item(
                Key={self._id_field: item_id},
                ConditionExpression="#data = :expected_data",
                ExpressionAttributeNames={"#data": "data"},
                ExpressionAttributeValues={":expected_data": expected_raw_data},
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def get_many(self, item_ids: Iterable[str]) -> dict[str, T]:
        """BatchGetItem(最大100件/リクエスト)で複数IDを一括取得する
        (対象確認機能2026-08、N+1回避)。1件ずつGetItemを呼ぶ実装へは
        フォールバックしない。

        戻り値には実際に存在したIDのみを含める(get()と同じ意味、存在しない
        IDは単に含まれずNoneでも表現しない)。UnprocessedKeysが返った場合は
        モジュール冒頭の定数(batch_tracker.py::_batch_write_with_retry()と
        同じ指数バックオフ+ジッター)で再送し、規定回数を超えて残る場合は
        RuntimeErrorを送出する(取得できなかったことを「存在しない」と
        混同して静かに省略しない)。
        """
        collector = self._collector()
        unique_ids = list(dict.fromkeys(item_ids))
        result: dict[str, T] = {}
        for chunk in _chunked(unique_ids, _BATCH_GET_MAX_KEYS_PER_REQUEST):
            pending_keys: list[dict[str, Any]] = [{self._id_field: item_id} for item_id in chunk]
            attempt = 0
            while pending_keys:
                response = cast(
                    "dict[str, Any]",
                    self._resource.batch_get_item(
                        RequestItems={self._table_name: {"Keys": pending_keys}}
                    ),
                )
                responses: dict[str, Any] = response.get("Responses", {})
                for item in self._decode_page(responses.get(self._table_name, []), collector):
                    result[str(getattr(item, self._id_field))] = item
                unprocessed_keys: dict[str, Any] = response.get("UnprocessedKeys", {})
                pending_keys = unprocessed_keys.get(self._table_name, {}).get("Keys", [])
                if not pending_keys:
                    break
                attempt += 1
                if attempt > _BATCH_GET_MAX_RETRIES:
                    raise RuntimeError(
                        f"BatchGetItem: UnprocessedKeysが{_BATCH_GET_MAX_RETRIES}回の"
                        f"再送後も残っています table={self._table_name} "
                        f"remaining={len(pending_keys)}"
                    )
                delay = min(
                    _BATCH_GET_MAX_DELAY_SECONDS,
                    _BATCH_GET_BASE_DELAY_SECONDS * (2 ** (attempt - 1)),
                )
                delay *= 1 + random.uniform(-0.2, 0.2)
                time.sleep(max(0.0, delay))
        return result
