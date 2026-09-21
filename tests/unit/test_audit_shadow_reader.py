"""Issue #458(#160 PR-4): shadow監査記録のDynamoDB read-only reader。

不変条件を固定する:
  * clientは`scan` / `describe_table`だけを通すallowlistのproxyで包まれる。write系の名前を1つずつ
    呼ぶと、すべて例外になる。
  * Scanはページ単位で`LastEvaluatedKey`が無くなるまで読み、`ConsumedCapacity`を合算する。
  * 壊れた項目は全体を止めず`unparsed`として数える(LENIENT。沈黙させない)。
  * `scan`に渡す引数は`TableName` / `ReturnConsumedCapacity` / `ExclusiveStartKey`のみ。

★ 銘柄コードは実在しない0000系のみ。Productionへは一切アクセスしない(fake clientのみ)。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from jstock_advisor.infrastructure.aws.audit_shadow_reader import (
    ALLOWED_OPERATIONS,
    ReadOnlyDynamoClient,
    ReadOnlyViolationError,
    describe_table_metrics,
    iter_scan_pages,
    scan_shadow_records,
)

_WRITE_OPERATIONS = (
    "put_item",
    "update_item",
    "delete_item",
    "batch_write_item",
    "transact_write_items",
    "create_table",
    "delete_table",
    "update_table",
    "execute_statement",
    "invoke",
    "publish",
    "put_object",
)


def _item(audit_id: str, decision_type: str, timestamp: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "audit_id": audit_id,
        "timestamp": timestamp,
        "stock_code": "0000",
        "decision_type": decision_type,
        "input_values": {"schema_version": 1, "engine": "BUY_CANDIDATES", "buy_action": "BUY"},
        "calculation_formulas": {},
        "output_values": {"findings": [], "not_evaluated": [], "strong": True},
        "data_sources": [],
        "rule_version": "v1",
    }
    body.update(extra)
    return {"audit_id": {"S": audit_id}, "data": {"S": json.dumps(body)}}


class _FakeClient:
    """`scan` / `describe_table`だけを持つfake。それ以外は呼ばれたら失敗する(fail stub)。"""

    def __init__(self, pages: list[dict[str, Any]], table: dict[str, Any] | None = None) -> None:
        self._pages = list(pages)
        self._table = table or {"ItemCount": 10, "TableSizeBytes": 2048}
        self.scan_calls: list[dict[str, Any]] = []
        self.describe_calls: list[dict[str, Any]] = []

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        self.scan_calls.append(kwargs)
        return self._pages.pop(0)

    def describe_table(self, **kwargs: Any) -> dict[str, Any]:
        self.describe_calls.append(kwargs)
        return {"Table": self._table}

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"read-only readerが許可外の操作を呼んだ: {name}")


def test_allowlist_contains_only_scan_and_describe_table() -> None:
    assert frozenset({"scan", "describe_table"}) == ALLOWED_OPERATIONS


@pytest.mark.parametrize("operation", _WRITE_OPERATIONS)
def test_proxy_rejects_every_write_operation(operation: str) -> None:
    proxy = ReadOnlyDynamoClient(_FakeClient([]))

    with pytest.raises(ReadOnlyViolationError):
        getattr(proxy, operation)


def test_proxy_passes_scan_and_describe_table_through() -> None:
    fake = _FakeClient([{"Items": []}])
    proxy = ReadOnlyDynamoClient(fake)

    proxy.scan(TableName="t")
    proxy.describe_table(TableName="t")

    assert fake.scan_calls == [{"TableName": "t"}]
    assert fake.describe_calls == [{"TableName": "t"}]


def test_scan_reads_all_pages_and_only_sends_read_parameters() -> None:
    fake = _FakeClient(
        [
            {
                "Items": [_item("1", "judgment_safety_shadow", "2026-09-24T00:00:00+00:00")],
                "ScannedCount": 1,
                "Count": 1,
                "ConsumedCapacity": {"CapacityUnits": 4.5},
                "LastEvaluatedKey": {"audit_id": {"S": "1"}},
            },
            {
                "Items": [_item("2", "buy_signal", "2026-09-24T01:00:00+00:00")],
                "ScannedCount": 1,
                "Count": 1,
                "ConsumedCapacity": {"CapacityUnits": 0.5},
            },
        ]
    )

    result = scan_shadow_records(ReadOnlyDynamoClient(fake), "jstock-audit_log")

    assert [c.keys() for c in fake.scan_calls] == [
        {"TableName", "ReturnConsumedCapacity"},
        {"TableName", "ReturnConsumedCapacity", "ExclusiveStartKey"},
    ]
    assert all(c["ReturnConsumedCapacity"] == "TOTAL" for c in fake.scan_calls)
    assert fake.scan_calls[1]["ExclusiveStartKey"] == {"audit_id": {"S": "1"}}
    m = result.scan_metrics
    assert (m.read_pages, m.scanned_count, m.count, m.total_rru) == (2, 2, 2, 5.0)
    assert [e.audit_id for e in result.shadow_entries] == ["1"]
    assert result.decision_type_counts == {"judgment_safety_shadow": 1, "buy_signal": 1}


def test_scanned_count_and_count_can_differ() -> None:
    fake = _FakeClient([{"Items": [], "ScannedCount": 7, "Count": 3}])

    result = scan_shadow_records(ReadOnlyDynamoClient(fake), "t")

    assert (result.scan_metrics.scanned_count, result.scan_metrics.count) == (7, 3)


def test_broken_items_are_counted_as_unparsed_without_stopping() -> None:
    good = _item("1", "judgment_safety_shadow", "2026-09-24T00:00:00+00:00")
    invalid_json = {"audit_id": {"S": "2"}, "data": {"S": "{not json"}}
    no_data = {"audit_id": {"S": "3"}}
    no_decision_type = {"audit_id": {"S": "4"}, "data": {"S": json.dumps({"x": 1})}}
    bad_shadow_model = {
        "audit_id": {"S": "5"},
        "data": {"S": json.dumps({"decision_type": "judgment_safety_shadow", "timestamp": "x"})},
    }
    fake = _FakeClient(
        [{"Items": [good, invalid_json, no_data, no_decision_type, bad_shadow_model]}]
    )

    result = scan_shadow_records(ReadOnlyDynamoClient(fake), "t")

    assert [e.audit_id for e in result.shadow_entries] == ["1"]
    assert result.unparsed == 4  # 壊れた1件で全体を止めず、沈黙もさせない


def test_all_records_by_jst_date_uses_jst_and_skips_unreadable_timestamps() -> None:
    fake = _FakeClient(
        [
            {
                "Items": [
                    _item("1", "buy_signal", "2026-09-24T14:59:00+00:00"),  # JST 9/24
                    _item("2", "buy_signal", "2026-09-24T15:00:00+00:00"),  # JST 9/25
                    _item("3", "buy_signal", "2026-09-24T15:00:00"),  # tz無し: 推測しない
                    _item("4", "buy_signal", "garbage"),
                ]
            }
        ]
    )

    result = scan_shadow_records(ReadOnlyDynamoClient(fake), "t")

    assert result.all_records_by_jst_date == {"2026-09-24": 1, "2026-09-25": 1}


def test_shadow_item_bytes_counts_only_shadow_items() -> None:
    shadow = _item("1", "judgment_safety_shadow", "2026-09-24T00:00:00+00:00")
    other = _item("2", "buy_signal", "2026-09-24T00:00:00+00:00")
    fake = _FakeClient([{"Items": [shadow, other]}])

    result = scan_shadow_records(ReadOnlyDynamoClient(fake), "t")

    assert result.shadow_item_bytes == len(shadow["data"]["S"])
    assert result.scan_metrics.item_bytes == len(shadow["data"]["S"]) + len(other["data"]["S"])


def test_iter_scan_pages_stops_when_no_last_evaluated_key() -> None:
    fake = _FakeClient([{"Items": []}])

    pages = list(iter_scan_pages(ReadOnlyDynamoClient(fake), "t"))

    assert len(pages) == 1
    assert len(fake.scan_calls) == 1


def test_describe_table_metrics_reads_item_count_and_size() -> None:
    fake = _FakeClient([], table={"ItemCount": 78_700, "TableSizeBytes": 153_000_000})

    metrics = describe_table_metrics(ReadOnlyDynamoClient(fake), "jstock-audit_log")

    assert (metrics.item_count, metrics.table_size_bytes) == (78_700, 153_000_000)
    assert fake.describe_calls == [{"TableName": "jstock-audit_log"}]
    assert fake.scan_calls == []  # describeはscanしない


def test_scan_with_fail_stub_client_never_touches_other_methods() -> None:
    """scan / describe_table以外の全メソッドが呼ばれたら失敗するclientで、読み取りを実行する。"""
    fake = _FakeClient(
        [{"Items": [_item("1", "judgment_safety_shadow", "2026-09-24T00:00:00+00:00")]}]
    )
    client = ReadOnlyDynamoClient(fake)

    describe_table_metrics(client, "t")
    scan_shadow_records(client, "t")  # 許可外の属性へ触れたらAssertionError
