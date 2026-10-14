"""Issue #605 最小 CLI: 記録を GetItem だけで読む reader の契約テスト。

固定するもの:
  - ★ allowlist の proxy: get_item だけを通し、put_item・update_item・delete_item・scan・
    query・batch_*・transact_* など get_item 以外は ReadOnlyViolationError ★
  - 実際の DynamoDB の型(moto)で、AuditLog の項目 `{audit_id, data(JSON)}` から
    `output_values` を読む
  - 項目なし = NOT_FOUND / 項目はあるが記録として読めない形 = UNREADABLE(例外にしない)
  - 失敗(権限なし・表なし・接続・その他)は固定の種別だけ(値・表名・audit_id・例外の文面を持たない)
  - ConsistentRead で 1 回だけ引く / 書込の経路が無い(AST)
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws

from jstock_advisor.infrastructure.aws.allocation_shadow_reader import (
    ALLOWED_OPERATIONS,
    DynamoRecordSource,
    GetItemOnlyClient,
    ReadFailure,
    ReadFailureKind,
    ReadOnlyViolationError,
    ReadStatus,
    decode_item,
)

_REPO = Path(__file__).resolve().parents[2]
_READER_PATH = (
    _REPO / "src" / "jstock_advisor" / "infrastructure" / "aws" / "allocation_shadow_reader.py"
)
_TABLE = "jstock-audit_log"
_SENTINEL = "sentinel-audit-id-zz"


def _entry_json(output: Any) -> str:
    return json.dumps(
        {
            "audit_id": "x",
            "decision_type": "allocation_shadow_items",
            "timestamp": "2026-10-14T00:00:00+00:00",
            "input_values": {},
            "calculation_formulas": {},
            "output_values": output,
            "data_sources": [],
            "rule_version": "r",
        }
    )


def _make_table() -> Any:
    client = boto3.client("dynamodb", region_name="ap-northeast-1")
    client.create_table(
        TableName=_TABLE,
        KeySchema=[{"AttributeName": "audit_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "audit_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return client


def _put(client: Any, audit_id: str, data: str) -> None:
    client.put_item(TableName=_TABLE, Item={"audit_id": {"S": audit_id}, "data": {"S": data}})


# --- allowlist -----------------------------------------------------------------------------------


def test_only_get_item_is_allowed() -> None:
    assert frozenset({"get_item"}) == ALLOWED_OPERATIONS


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        def call(**_kwargs: Any) -> dict[str, Any]:
            self.calls.append(name)
            return {}

        return call


@pytest.mark.parametrize(
    "name",
    [
        "put_item",
        "update_item",
        "delete_item",
        "scan",
        "query",
        "batch_get_item",
        "batch_write_item",
        "transact_write_items",
        "transact_get_items",
        "execute_statement",
        "create_table",
        "delete_table",
        "update_table",
        "describe_table",
        "list_tables",
        "import_table",
        "restore_table_from_backup",
    ],
)
def test_every_other_operation_is_refused_without_reaching_the_client(name: str) -> None:
    inner = _Recorder()
    client = GetItemOnlyClient(inner)

    with pytest.raises(ReadOnlyViolationError):
        getattr(client, name)(TableName=_TABLE)

    assert inner.calls == []


def test_get_item_is_passed_through() -> None:
    inner = _Recorder()

    GetItemOnlyClient(inner).get_item(TableName=_TABLE)

    assert inner.calls == ["get_item"]


# --- 実際の DynamoDB の型(moto)--------------------------------------------------------------------


@mock_aws
def test_a_found_record_is_the_output_values_dictionary() -> None:
    client = _make_table()
    record = {"schema_version": 1, "record_kind": "allocation_shadow_index", "n": ["a", {"b": 1}]}
    _put(client, "k1", _entry_json(record))

    result = DynamoRecordSource(GetItemOnlyClient(client), _TABLE).get("k1")

    assert result.status is ReadStatus.FOUND
    assert result.record == record


@mock_aws
def test_an_absent_item_is_not_found() -> None:
    client = _make_table()

    result = DynamoRecordSource(GetItemOnlyClient(client), _TABLE).get("none")

    assert result.status is ReadStatus.NOT_FOUND
    assert result.record is None


@mock_aws
@pytest.mark.parametrize(
    "data",
    [
        "not json",
        "[]",
        "null",
        '"text"',
        json.dumps({"output_values": "x"}),
        json.dumps({"output_values": None}),
        json.dumps({"output_values": [1]}),
        json.dumps({"no_output": {}}),
        "",
    ],
)
def test_an_item_that_is_not_a_record_is_unreadable_not_an_exception(data: str) -> None:
    client = _make_table()
    _put(client, "k", data)

    result = DynamoRecordSource(GetItemOnlyClient(client), _TABLE).get("k")

    assert result.status is ReadStatus.UNREADABLE
    assert result.record is None


@mock_aws
def test_an_item_without_the_data_attribute_is_unreadable() -> None:
    client = _make_table()
    client.put_item(TableName=_TABLE, Item={"audit_id": {"S": "k"}, "other": {"S": "x"}})

    assert DynamoRecordSource(GetItemOnlyClient(client), _TABLE).get("k").status is (
        ReadStatus.UNREADABLE
    )


@mock_aws
def test_the_read_is_a_single_consistent_get_item() -> None:
    client = _make_table()
    _put(client, "k", _entry_json({}))
    calls: list[dict[str, Any]] = []
    real = client.get_item

    def spy(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return real(**kwargs)

    client.get_item = spy

    DynamoRecordSource(GetItemOnlyClient(client), _TABLE).get("k")

    assert len(calls) == 1
    assert calls[0]["ConsistentRead"] is True
    assert calls[0]["Key"] == {"audit_id": {"S": "k"}}
    assert calls[0]["TableName"] == _TABLE


# --- decode_item(純粋)----------------------------------------------------------------


@pytest.mark.parametrize("item", [None, {}])
def test_an_empty_response_is_not_found(item: Any) -> None:
    assert decode_item(item).status is ReadStatus.NOT_FOUND


@pytest.mark.parametrize(
    "item",
    [
        {"data": "x"},
        {"data": {"N": "1"}},
        {"data": {"S": 5}},
        {"data": None},
        {"data": []},
        {"x": 1},
    ],
)
def test_a_malformed_item_is_unreadable(item: Any) -> None:
    assert decode_item(item).status is ReadStatus.UNREADABLE


# --- 失敗は固定の種別だけ -------------------------------------------------------------------------


class _Failing:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    def get_item(self, **_kwargs: Any) -> Any:
        raise self._error


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": _SENTINEL}}, "GetItem")


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (_client_error("AccessDeniedException"), ReadFailureKind.ACCESS_DENIED),
        (_client_error("AccessDenied"), ReadFailureKind.ACCESS_DENIED),
        (_client_error("UnauthorizedOperation"), ReadFailureKind.ACCESS_DENIED),
        (_client_error("ResourceNotFoundException"), ReadFailureKind.NOT_FOUND_TABLE),
        (_client_error("ThrottlingException"), ReadFailureKind.ERROR),
        (_client_error(""), ReadFailureKind.ERROR),
        (EndpointConnectionError(endpoint_url=_SENTINEL), ReadFailureKind.CONNECTION),
        (RuntimeError(_SENTINEL), ReadFailureKind.ERROR),
        (ValueError(_SENTINEL), ReadFailureKind.ERROR),
    ],
)
def test_a_failure_is_a_fixed_kind_that_leaks_nothing(
    error: BaseException, kind: ReadFailureKind
) -> None:
    source = DynamoRecordSource(GetItemOnlyClient(_Failing(error)), _TABLE)

    with pytest.raises(ReadFailure) as caught:
        source.get(_SENTINEL)

    assert caught.value.kind is kind
    text = str(caught.value) + repr(caught.value)
    assert _SENTINEL not in text
    assert _TABLE not in text
    assert caught.value.__cause__ is None  # 元の例外(文面を含みうる)を連鎖させない
    assert caught.value.__suppress_context__ is True


def test_a_non_dictionary_response_is_not_an_exception() -> None:
    class Odd:
        def get_item(self, **_kwargs: Any) -> Any:
            return "odd"

    source = DynamoRecordSource(GetItemOnlyClient(Odd()), _TABLE)

    assert source.get("k").status is ReadStatus.NOT_FOUND


# --- 構造(書込の経路が無い)-----------------------------------------------------------------------

_WRITE_NAMES = {
    "put_item",
    "update_item",
    "delete_item",
    "batch_write_item",
    "transact_write_items",
    "scan",
    "query",
    "create_table",
    "delete_table",
    "update_table",
    "invoke",
    "publish",
    "send_message",
    "put_object",
    "write",
    "write_text",
    "write_bytes",
    "unlink",
}


def test_the_reader_has_no_write_scan_or_invoke_call_and_reads_no_environment() -> None:
    tree = ast.parse(_READER_PATH.read_text(encoding="utf-8"))
    called = {
        getattr(node.func, "attr", getattr(node.func, "id", ""))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not called & _WRITE_NAMES
    assert not {"getenv", "environ"} & called
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "environ" not in names
    assert "get_item" in called


def test_the_reader_names_only_the_get_item_operation() -> None:
    source = _READER_PATH.read_text(encoding="utf-8")
    for forbidden in ("put_item(", "update_item(", "delete_item(", ".scan(", ".query("):
        assert forbidden not in source


def test_the_reader_does_not_import_the_collection_store_that_can_write() -> None:
    tree = ast.parse(_READER_PATH.read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not [m for m in imported if "dynamodb_store" in m or "collection_store" in m]
    assert not [m for m in imported if "repository" in m]
