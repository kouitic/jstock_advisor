from __future__ import annotations

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
