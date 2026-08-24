from __future__ import annotations

import time

import boto3
import pytest
from moto import mock_aws
from pydantic import BaseModel

from jstock_advisor.infrastructure.aws.dynamodb_store import DynamoDbCollectionStore

_TABLE_NAME = "test-items"
_REGION = "ap-northeast-1"


class _Item(BaseModel):
    item_id: str
    name: str
    value: int


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_TABLE_NAME,
            KeySchema=[{"AttributeName": "item_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "item_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield DynamoDbCollectionStore(_Item, _TABLE_NAME, "item_id")


@pytest.fixture
def store_with_ttl(monkeypatch: pytest.MonkeyPatch):
    """通知検証モード機能(2026-08追加)。ttl_seconds指定時のみttl属性が付与される
    ことを確認するための、既存storeフィクスチャと同一テーブルを使うTTL付き版。"""
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_TABLE_NAME,
            KeySchema=[{"AttributeName": "item_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "item_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield DynamoDbCollectionStore(_Item, _TABLE_NAME, "item_id", ttl_seconds=7200)


def test_upsert_and_get_round_trips(store: DynamoDbCollectionStore[_Item]) -> None:
    item = _Item(item_id="1", name="foo", value=10)
    store.upsert(item)
    fetched = store.get("1")
    assert fetched == item


def test_get_returns_none_for_missing_item(store: DynamoDbCollectionStore[_Item]) -> None:
    assert store.get("does-not-exist") is None


def test_upsert_overwrites_existing_item(store: DynamoDbCollectionStore[_Item]) -> None:
    store.upsert(_Item(item_id="1", name="foo", value=10))
    store.upsert(_Item(item_id="1", name="bar", value=20))
    fetched = store.get("1")
    assert fetched is not None
    assert fetched.name == "bar"
    assert fetched.value == 20


def test_list_all_returns_every_item(store: DynamoDbCollectionStore[_Item]) -> None:
    store.upsert(_Item(item_id="1", name="a", value=1))
    store.upsert(_Item(item_id="2", name="b", value=2))
    items = store.list_all()
    assert {item.item_id for item in items} == {"1", "2"}


def test_delete_removes_item_and_reports_existence(store: DynamoDbCollectionStore[_Item]) -> None:
    store.upsert(_Item(item_id="1", name="a", value=1))
    assert store.delete("1") is True
    assert store.get("1") is None
    assert store.delete("1") is False


def test_find_filters_with_predicate(store: DynamoDbCollectionStore[_Item]) -> None:
    store.upsert(_Item(item_id="1", name="a", value=1))
    store.upsert(_Item(item_id="2", name="b", value=2))
    store.upsert(_Item(item_id="3", name="c", value=3))
    found = store.find(lambda i: i.value >= 2)
    assert {item.item_id for item in found} == {"2", "3"}


def test_upsert_many_writes_all_items(store: DynamoDbCollectionStore[_Item]) -> None:
    store.upsert_many(
        [
            _Item(item_id="1", name="a", value=1),
            _Item(item_id="2", name="b", value=2),
        ]
    )
    assert len(store.list_all()) == 2


def test_insert_if_absent_adds_new_item_and_returns_true(
    store: DynamoDbCollectionStore[_Item],
) -> None:
    added = store.insert_if_absent(_Item(item_id="1", name="foo", value=10))
    assert added is True
    assert store.get("1") == _Item(item_id="1", name="foo", value=10)


def test_insert_if_absent_does_not_overwrite_existing_item(
    store: DynamoDbCollectionStore[_Item],
) -> None:
    store.upsert(_Item(item_id="1", name="original", value=10))

    added = store.insert_if_absent(_Item(item_id="1", name="attempted-overwrite", value=99))

    assert added is False
    fetched = store.get("1")
    assert fetched is not None
    assert fetched.name == "original"
    assert fetched.value == 10


def test_get_consistent_round_trips(store: DynamoDbCollectionStore[_Item]) -> None:
    store.upsert(_Item(item_id="1", name="foo", value=10))
    assert store.get_consistent("1") == _Item(item_id="1", name="foo", value=10)


def test_get_consistent_returns_none_for_missing_item(
    store: DynamoDbCollectionStore[_Item],
) -> None:
    assert store.get_consistent("does-not-exist") is None


def test_get_consistent_uses_consistent_read(
    store: DynamoDbCollectionStore[_Item], monkeypatch: pytest.MonkeyPatch
) -> None:
    """コードレビュー対応: get_consistent()はConsistentRead=TrueでGetItemを呼ぶこと
    (通常のget()は結果整合性読み取りのまま変更せず、こちらだけ強い整合性で読む)。"""
    calls: list[dict] = []
    original_get_item = store._table.get_item

    def _spy_get_item(**kwargs):
        calls.append(kwargs)
        return original_get_item(**kwargs)

    monkeypatch.setattr(store._table, "get_item", _spy_get_item)

    store.get_consistent("does-not-exist")

    assert len(calls) == 1
    assert calls[0].get("ConsistentRead") is True


def test_upsert_without_ttl_seconds_omits_ttl_attribute(
    store: DynamoDbCollectionStore[_Item],
) -> None:
    """既存の全リポジトリ呼び出し(ttl_seconds未指定)はttl属性が付与されないまま
    (通知検証モード機能2026-08追加、NORMAL挙動の無変更確認)。"""
    store.upsert(_Item(item_id="1", name="foo", value=10))
    raw_item = store._table.get_item(Key={"item_id": "1"})["Item"]
    assert "ttl" not in raw_item


def test_upsert_with_ttl_seconds_sets_ttl_attribute(
    store_with_ttl: DynamoDbCollectionStore[_Item],
) -> None:
    """通知検証モード機能(2026-08追加): ttl_seconds指定時のみttl属性(UNIX秒)を
    付与する(ValidationRecommendationsTable等の使い捨てテーブル向け)。"""
    before = int(time.time())
    store_with_ttl.upsert(_Item(item_id="1", name="foo", value=10))
    raw_item = store_with_ttl._table.get_item(Key={"item_id": "1"})["Item"]
    assert "ttl" in raw_item
    assert int(raw_item["ttl"]) >= before + 7200 - 5
    assert int(raw_item["ttl"]) <= before + 7200 + 30


@pytest.fixture
def store_with_gsi(monkeypatch: pytest.MonkeyPatch):
    """LINE UI第二弾「対象確認」機能(2026-08)向け、GSI経由のQuery検証用。
    汎用のcategory属性でGSIを作る(batch_id等、特定機能の属性名にテストを
    結び付けないため)。"""
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_TABLE_NAME,
            KeySchema=[{"AttributeName": "item_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "item_id", "AttributeType": "S"},
                {"AttributeName": "category", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "category-index",
                    "KeySchema": [{"AttributeName": "category", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield DynamoDbCollectionStore(_Item, _TABLE_NAME, "item_id")


def test_upsert_with_index_attributes_adds_top_level_attribute(
    store_with_gsi: DynamoDbCollectionStore[_Item],
) -> None:
    store_with_gsi.upsert_with_index_attributes(
        _Item(item_id="1", name="a", value=1), {"category": "buy_candidate"}
    )
    raw_item = store_with_gsi._table.get_item(Key={"item_id": "1"})["Item"]
    assert raw_item["category"] == "buy_candidate"
    # dataとしても引き続き正しく読み戻せる(索引属性がモデルの通常シリアライズを
    # 壊さないこと)。
    assert store_with_gsi.get("1") == _Item(item_id="1", name="a", value=1)


def test_query_by_index_returns_only_matching_items(
    store_with_gsi: DynamoDbCollectionStore[_Item],
) -> None:
    store_with_gsi.upsert_with_index_attributes(
        _Item(item_id="1", name="a", value=1), {"category": "buy_candidate"}
    )
    store_with_gsi.upsert_with_index_attributes(
        _Item(item_id="2", name="b", value=2), {"category": "buy_candidate"}
    )
    store_with_gsi.upsert_with_index_attributes(
        _Item(item_id="3", name="c", value=3), {"category": "near_buy"}
    )

    results = store_with_gsi.query_by_index("category-index", "category", "buy_candidate")

    assert {item.item_id for item in results} == {"1", "2"}


def test_query_by_index_returns_empty_list_when_no_match(
    store_with_gsi: DynamoDbCollectionStore[_Item],
) -> None:
    store_with_gsi.upsert_with_index_attributes(
        _Item(item_id="1", name="a", value=1), {"category": "buy_candidate"}
    )
    assert store_with_gsi.query_by_index("category-index", "category", "no_such_value") == []


def test_upsert_without_index_attributes_is_unaffected(
    store: DynamoDbCollectionStore[_Item],
) -> None:
    """既存のupsert()呼び出しは今回の変更で一切挙動が変わらない(索引属性を
    要求しない通常テーブルでも引き続き使えること)。"""
    store.upsert(_Item(item_id="1", name="foo", value=10))
    assert store.get("1") == _Item(item_id="1", name="foo", value=10)


def test_get_does_not_use_consistent_read(
    store: DynamoDbCollectionStore[_Item], monkeypatch: pytest.MonkeyPatch
) -> None:
    """通常のget()は今回の修正で変更しない(ConsistentReadを指定しないまま)。"""
    calls: list[dict] = []
    original_get_item = store._table.get_item

    def _spy_get_item(**kwargs):
        calls.append(kwargs)
        return original_get_item(**kwargs)

    monkeypatch.setattr(store._table, "get_item", _spy_get_item)

    store.get("does-not-exist")

    assert len(calls) == 1
    assert "ConsistentRead" not in calls[0]
