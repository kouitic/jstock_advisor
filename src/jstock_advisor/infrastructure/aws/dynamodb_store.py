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

import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table


class DynamoDbCollectionStore[T: BaseModel]:
    def __init__(
        self, model_type: type[T], table_name: str, id_field: str, ttl_seconds: int | None = None
    ) -> None:
        """ttl_secondsを指定すると、保存する各アイテムへDynamoDB Native TTL用の
        ttl属性(現在時刻+ttl_seconds、UNIX秒)を付与する(通知検証モード機能
        2026-08追加。使い捨てテーブル向けの任意機能で、未指定(既定)なら
        既存の全リポジトリと同様ttl属性は付与されない)。
        """
        self._model_type = model_type
        self._id_field = id_field
        self._ttl_seconds = ttl_seconds
        resource = boto3.resource("dynamodb")
        self._table: Table = resource.Table(table_name)

    def _to_item(self, model: T) -> dict[str, Any]:
        item_id = str(getattr(model, self._id_field))
        item: dict[str, Any] = {self._id_field: item_id, "data": model.model_dump_json()}
        if self._ttl_seconds is not None:
            item["ttl"] = int(time.time()) + self._ttl_seconds
        return item

    def _from_item(self, item: dict[str, Any]) -> T:
        return self._model_type.model_validate_json(item["data"])

    def list_all(self) -> list[T]:
        items: list[T] = []
        scan_kwargs: dict[str, Any] = {}
        while True:
            response = self._table.scan(**scan_kwargs)
            items.extend(self._from_item(raw) for raw in response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key
        return items

    def get(self, item_id: str) -> T | None:
        response = self._table.get_item(Key={self._id_field: item_id})
        item = response.get("Item")
        return self._from_item(item) if item is not None else None

    def get_consistent(self, item_id: str) -> T | None:
        """get()のstrongly consistent read版(ConsistentRead=True)。

        insert_if_absent()の競合後にレコード内容を比較する等、結果整合性読み取り
        による一時的なNoneを避けたい限定用途でのみ使うこと(通常のget()はコスト・
        挙動を変えないため結果整合性読み取りのまま維持する)。
        """
        response = self._table.get_item(Key={self._id_field: item_id}, ConsistentRead=True)
        item = response.get("Item")
        return self._from_item(item) if item is not None else None

    def upsert(self, item: T) -> None:
        self._table.put_item(Item=self._to_item(item))

    def upsert_many(self, new_items: Iterable[T]) -> None:
        with self._table.batch_writer() as batch:
            for item in new_items:
                batch.put_item(Item=self._to_item(item))

    def delete(self, item_id: str) -> bool:
        response = self._table.delete_item(Key={self._id_field: item_id}, ReturnValues="ALL_OLD")
        return "Attributes" in response

    def find(self, predicate: Callable[[T], bool]) -> list[T]:
        return [item for item in self.list_all() if predicate(item)]

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
