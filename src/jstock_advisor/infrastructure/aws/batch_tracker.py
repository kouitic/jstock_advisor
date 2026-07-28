"""Lambda銘柄単位ファンアウト(lambda_handlers/_fanout.py)の完了検知用カウンタ。

DynamoDBの原子的なADD操作(UpdateItem)で完了件数をカウントし、最後の1件を
処理したワーカーが「自分が最後だった」と検知してサマリー通知を送信する
(Step Functions等の追加インフラを使わない軽量な集約方式)。

ローカル(非Lambda)環境では常にNoneを返す。_fanout.py自体がLambda上でのみ
非同期再帰呼び出しを行う設計であり、ローカルCLIはこの機構を使わないため。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import boto3

from jstock_advisor.infrastructure.collection_store import resolve_table_name, running_on_lambda

_TABLE_FILE_NAME = "batch_runs.json"  # resolve_table_nameの命名規則(jstock-batch_runs)に合わせる
_TTL_HOURS = 6  # 集計用の一時データのため、数時間で自動削除する


@dataclass(frozen=True)
class BatchProgress:
    total: int
    succeeded: int
    failed: int
    completed: int

    @property
    def is_complete(self) -> bool:
        return self.completed >= self.total


def _table() -> Any:
    return boto3.resource("dynamodb").Table(resolve_table_name(_TABLE_FILE_NAME))


def start_batch(batch_id: str, total: int, now: dt.datetime) -> None:
    """ファンアウト開始時に呼ぶ。ローカル環境・対象0件の場合は何もしない。"""
    if total <= 0 or not running_on_lambda():
        return
    ttl = int((now + dt.timedelta(hours=_TTL_HOURS)).timestamp())
    _table().put_item(
        Item={
            "batch_id": batch_id,
            "total": total,
            "succeeded": 0,
            "failed": 0,
            "completed": 0,
            "ttl": ttl,
        }
    )


def record_result(batch_id: str, succeeded: bool) -> BatchProgress | None:
    """1銘柄の処理完了を原子的に記録し、現在の進捗を返す(ローカル環境ではNone)。"""
    if not running_on_lambda():
        return None
    field = "succeeded" if succeeded else "failed"
    response = _table().update_item(
        Key={"batch_id": batch_id},
        UpdateExpression=f"ADD {field} :one, completed :one",
        ExpressionAttributeValues={":one": 1},
        ReturnValues="ALL_NEW",
    )
    item = response["Attributes"]
    return BatchProgress(
        total=int(item["total"]),
        succeeded=int(item["succeeded"]),
        failed=int(item["failed"]),
        completed=int(item["completed"]),
    )
