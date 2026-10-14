"""購入側 Shadow の監査記録を読むだけの DynamoDB reader(Issue #605 最小 CLI)。

`AuditLogTable` を **GetItem だけ** で読み、記録(`AuditLogEntry.output_values` の辞書)を返す。

## read-only の保証(operations_manual 18節。名前だけで read-only と判断しない)

* boto3 の client は **allowlist の proxy**(`GetItemOnlyClient`)で包む。許可するのは `get_item`
  だけで、それ以外の属性アクセスは例外にする(write 系・scan・query を『誤って呼ぶ事故を防ぐ層』。
  実装の内部属性〔`_client`〕への直接アクセスまでは防げない。実際の境界は IAM の読取専用 role)
* 書込系のメソッドを持つ `DynamoDbCollectionStore` は使わない。項目の decode は
  `{"audit_id", "data"(JSON 文字列)}` の形を自前で読む
* 失敗は固定の種別(`ReadFailure`)だけを送出する。値・表名・audit_id・例外の文面を持たない

## 読み方(LENIENT)

記録は辞書のまま返す。形が合わないもの(JSON でない・`output_values` が辞書でない)は
`UNREADABLE` として返し、例外にしない(CLI が『読めなかった』と表示する)。
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from botocore.exceptions import BotoCoreError, ClientError

from jstock_advisor.domain.notification.allocation_shadow_cli_view import (
    DayInput,
    RecordInput,
    RecordState,
    day_skip_key,
    index_key,
    items_key,
    refs_to_read,
)

ALLOWED_OPERATIONS: Final[frozenset[str]] = frozenset({"get_item"})
_ACCESS_DENIED: Final = frozenset(
    {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}
)


class ReadOnlyViolationError(RuntimeError):
    """allowlist 外の操作(write 系など)を呼ぼうとした。"""


class GetItemOnlyClient:
    """`get_item` だけを通す client の proxy。それ以外は例外。"""

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        # __getattr__ はインスタンス属性に無い名前のときだけ呼ばれる。allowlist だけを通す。
        if name not in ALLOWED_OPERATIONS:
            raise ReadOnlyViolationError(
                f"read-only reader は {sorted(ALLOWED_OPERATIONS)} のみ許可する(要求: {name})"
            )
        return getattr(self._client, name)


def build_client(*, region: str, profile: str | None) -> GetItemOnlyClient:
    """明示した region・profile で client を作る(環境変数に依存しない)。boto3 は遅延 import。"""
    import boto3  # noqa: PLC0415 - CLI の実行時にだけ必要

    session = boto3.Session(profile_name=profile, region_name=region)
    return GetItemOnlyClient(session.client("dynamodb"))


class ReadFailure(Exception):  # noqa: N818 - 固定の種別だけを持つ
    """読取の失敗(接続・権限・応答の異常)。値・表名・audit_id・例外の文面を持たない。"""

    def __init__(self, kind: ReadFailureKind) -> None:
        super().__init__(kind.value)
        self.kind = kind


class ReadFailureKind(StrEnum):
    ACCESS_DENIED = "ACCESS_DENIED"
    NOT_FOUND_TABLE = "TABLE_NOT_FOUND"
    CONNECTION = "CONNECTION"
    ERROR = "ERROR"


class ReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    UNREADABLE = "UNREADABLE"  # 項目はあるが、記録として読めない形


@dataclass(frozen=True)
class RecordRead:
    status: ReadStatus
    record: dict[str, Any] | None = None


class RecordSource(Protocol):
    """記録の読取元(DynamoDB / ローカル)。audit_id を 1 件引く。失敗は ReadFailure。"""

    def get(self, audit_id: str) -> RecordRead: ...


def decode_item(item: dict[str, Any] | None) -> RecordRead:
    """GetItem の応答の項目 → 記録。形が合わなければ UNREADABLE(例外にしない)。"""
    if not item:
        return RecordRead(ReadStatus.NOT_FOUND)
    data = item.get("data")
    text = data.get("S") if isinstance(data, dict) else None
    if not isinstance(text, str):
        return RecordRead(ReadStatus.UNREADABLE)
    try:
        entry = json.loads(text)
    except ValueError:
        return RecordRead(ReadStatus.UNREADABLE)
    output = entry.get("output_values") if isinstance(entry, dict) else None
    if not isinstance(output, dict):
        return RecordRead(ReadStatus.UNREADABLE)
    return RecordRead(ReadStatus.FOUND, output)


@dataclass(frozen=True)
class DynamoRecordSource:
    client: GetItemOnlyClient
    table_name: str

    def get(self, audit_id: str) -> RecordRead:
        try:
            response = self.client.get_item(
                TableName=self.table_name,
                Key={"audit_id": {"S": audit_id}},
                ConsistentRead=True,
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in _ACCESS_DENIED:
                raise ReadFailure(ReadFailureKind.ACCESS_DENIED) from None
            if code == "ResourceNotFoundException":
                raise ReadFailure(ReadFailureKind.NOT_FOUND_TABLE) from None
            raise ReadFailure(ReadFailureKind.ERROR) from None
        except BotoCoreError:
            raise ReadFailure(ReadFailureKind.CONNECTION) from None
        except Exception:  # noqa: BLE001 - 固定の種別へ畳む(値・文面を出さない)
            raise ReadFailure(ReadFailureKind.ERROR) from None
        item = response.get("Item") if isinstance(response, dict) else None
        return decode_item(item if isinstance(item, dict) else None)


def _input(read: RecordRead) -> RecordInput:
    return RecordInput(RecordState(read.status.value), read.record)


def read_day(source: RecordSource, business_date: dt.date) -> DayInput:
    """索引 1 + skip 1 + 索引が載せた符号ごとの項目つきの記録(最大 16)を GetItem で読む。

    ★ キーの順序(索引 → 日単位 skip → 項目つきの記録)の実装はここ 1 つだけ ★。
    CLI(`jstock allocation today`)と集計(#934)の両方がこれを呼ぶ(読み方が 2 つに割れない)。
    """
    day = business_date.isoformat()
    index = _input(source.get(index_key(day)))
    skip = _input(source.get(day_skip_key(day)))
    items = {ref: _input(source.get(items_key(day, ref))) for ref in refs_to_read(index)}
    return DayInput(day, index, skip, items)


@dataclass(frozen=True)
class DayRead:
    """1 日分の読取の結果。読めた日は `day`、読めなかった日は固定の種別 `failure`。"""

    business_date: dt.date
    day: DayInput | None = None
    failure: ReadFailureKind | None = None


def read_days(source: RecordSource, dates: Iterable[dt.date]) -> list[DayRead]:
    """日付のループだけ(1 日分は `read_day`)。1 日の失敗は他の日の読取を止めない(日単位の隔離)。

    失敗は固定の種別(`ReadFailureKind`)だけを残す(値・表名・例外の文面を持たない)。
    """
    results: list[DayRead] = []
    for business_date in dates:
        try:
            results.append(DayRead(business_date, day=read_day(source, business_date)))
        except ReadFailure as failure:
            results.append(DayRead(business_date, failure=failure.kind))
    return results
