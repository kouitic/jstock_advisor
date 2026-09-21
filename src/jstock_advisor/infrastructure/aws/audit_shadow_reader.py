"""shadow監査記録を読むためのDynamoDB read-only reader(Issue #458 / #160 PR-4)。

`AuditLogTable`を**Scanだけ**で読み、`decision_type="judgment_safety_shadow"`の記録と、表・scanの
メトリクス(件数・サイズ・消費した読み取りユニット等)を返す。

## read-onlyの保証(operations_manual 18節。名前だけでread-onlyと判断しない)

* boto3のclientは**allowlistのproxy**(`ReadOnlyDynamoClient`)で包む。許可するのは`scan`と
  `describe_table`だけで、それ以外の属性アクセスは例外にする(write系の呼び出しを構造的に防ぐ)。
* 書き込み系のメソッドを持つ`DynamoDbCollectionStore`は使わない(read名の関数から推移的に
  write・副作用へ到達しうるため)。項目のdecodeは`{"audit_id", "data"(JSON文字列)}`の形式を
  自前で読む。
* `ReturnConsumedCapacity="TOTAL"`は読み取りのパラメータであり、書き込みではない。
"""

from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

from jstock_advisor.domain.entities.audit import AuditLogEntry
from jstock_advisor.domain.jst import to_jst

SHADOW_DECISION_TYPE: Final = "judgment_safety_shadow"
DEFAULT_TABLE_NAME: Final = "jstock-audit_log"
ALLOWED_OPERATIONS: Final[frozenset[str]] = frozenset({"scan", "describe_table"})


class ReadOnlyViolationError(RuntimeError):
    """allowlist外の操作(write系など)を呼ぼうとした。"""


class ReadOnlyDynamoClient:
    """`scan` / `describe_table`だけを通すclientのproxy。それ以外は例外。"""

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        # __getattr__はインスタンス属性に無い名前のときだけ呼ばれる。allowlistだけを通す。
        if name not in ALLOWED_OPERATIONS:
            raise ReadOnlyViolationError(
                f"read-only readerは{sorted(ALLOWED_OPERATIONS)}のみ許可する(要求: {name})"
            )
        return getattr(self._client, name)


def build_read_only_client() -> ReadOnlyDynamoClient:
    """呼び出し元の資格情報(AWS_PROFILE等)でread-onlyのclientを作る。boto3は遅延importする。"""
    import boto3  # noqa: PLC0415 - CLIの実行時にだけ必要

    return ReadOnlyDynamoClient(boto3.client("dynamodb"))


@dataclass(frozen=True)
class TableMetrics:
    """DescribeTableの値(★概算。DynamoDBがおよそ6時間ごとに更新する)。"""

    item_count: int
    table_size_bytes: int


@dataclass(frozen=True)
class ScanMetrics:
    """Scanの応答から集計した実測値。"""

    read_pages: int
    scanned_count: int
    count: int
    total_rru: float
    elapsed_time_sec: float
    item_bytes: int


@dataclass(frozen=True)
class ScanResult:
    shadow_entries: list[AuditLogEntry]
    unparsed: int
    #: 走査した全項目の`decision_type`別件数(増加の内訳)。
    decision_type_counts: dict[str, int]
    #: 走査した全項目のJST暦日別の件数(表全体の日別の増加量。`timestamp`を読めない項目は数えない)。
    all_records_by_jst_date: dict[str, int]
    shadow_item_bytes: int
    scan_metrics: ScanMetrics


def describe_table_metrics(client: ReadOnlyDynamoClient, table_name: str) -> TableMetrics:
    table = client.describe_table(TableName=table_name)["Table"]
    return TableMetrics(
        item_count=int(table.get("ItemCount", 0)),
        table_size_bytes=int(table.get("TableSizeBytes", 0)),
    )


def _string_attribute(item: dict[str, Any], name: str) -> str | None:
    value = item.get(name)
    if isinstance(value, dict):
        raw = value.get("S")
        return raw if isinstance(raw, str) else None
    return value if isinstance(value, str) else None


def _jst_date_of(timestamp: object) -> str | None:
    """`timestamp`(ISO 8601)のJST暦日。読めない・タイムゾーン無しはNone(推測しない)。"""
    if not isinstance(timestamp, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return to_jst(parsed).date().isoformat()


def iter_scan_pages(client: ReadOnlyDynamoClient, table_name: str) -> Iterator[dict[str, Any]]:
    """Scanをページ単位で返す(`LastEvaluatedKey`が無くなるまで)。読み取りのみ。"""
    start_key: dict[str, Any] | None = None
    while True:
        kwargs: dict[str, Any] = {"TableName": table_name, "ReturnConsumedCapacity": "TOTAL"}
        if start_key is not None:
            kwargs["ExclusiveStartKey"] = start_key
        response = client.scan(**kwargs)
        yield response
        start_key = response.get("LastEvaluatedKey")
        if not start_key:
            return


def scan_shadow_records(client: ReadOnlyDynamoClient, table_name: str) -> ScanResult:
    """表を全件Scanし、shadow記録と各種メトリクスを返す。壊れた項目は`unparsed`として数える(LENIENT)。"""
    started = time.monotonic()
    pages = 0
    scanned = 0
    count = 0
    rru = 0.0
    item_bytes = 0
    shadow_bytes = 0
    unparsed = 0
    entries: list[AuditLogEntry] = []
    type_counts: dict[str, int] = {}
    by_jst_date: dict[str, int] = {}
    for page in iter_scan_pages(client, table_name):
        pages += 1
        scanned += int(page.get("ScannedCount", 0))
        count += int(page.get("Count", 0))
        capacity = page.get("ConsumedCapacity") or {}
        rru += float(capacity.get("CapacityUnits", 0.0))
        for item in page.get("Items", []):
            data = _string_attribute(item, "data")
            if data is None:
                unparsed += 1
                continue
            item_bytes += len(data)
            # 全件は軽量に読む(decision_typeとtimestampだけ)。shadowだけをモデルとして検証する。
            try:
                light = json.loads(data)
                decision_type = str(light["decision_type"])
            except (ValueError, KeyError, TypeError):
                unparsed += 1
                continue
            type_counts[decision_type] = type_counts.get(decision_type, 0) + 1
            jst_date = _jst_date_of(light.get("timestamp"))
            if jst_date is not None:
                by_jst_date[jst_date] = by_jst_date.get(jst_date, 0) + 1
            if decision_type != SHADOW_DECISION_TYPE:
                continue
            try:
                entries.append(AuditLogEntry.model_validate_json(data))
            except ValueError:
                unparsed += 1
                continue
            shadow_bytes += len(data)
    metrics = ScanMetrics(
        read_pages=pages,
        scanned_count=scanned,
        count=count,
        total_rru=rru,
        elapsed_time_sec=time.monotonic() - started,
        item_bytes=item_bytes,
    )
    return ScanResult(
        shadow_entries=entries,
        unparsed=unparsed,
        decision_type_counts=type_counts,
        all_records_by_jst_date=by_jst_date,
        shadow_item_bytes=shadow_bytes,
        scan_metrics=metrics,
    )
