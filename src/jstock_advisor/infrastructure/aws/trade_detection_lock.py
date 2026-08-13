"""TradeDetectionRunLock: 売買イベント検知処理の実行順序非依存化・冪等性担保
(BUY候補裾野拡大機能2026-08)。

BUY候補Lambda・保有銘柄Lambdaはどちらが先に起動するか保証されない
(infra/template.yamlで両方とも同時刻cronのため)。両ハンドラの入口で
`TradeCooldownService.detect_and_apply()`が呼ばれても、当日分の検知処理が
1回だけ実行されるよう、PROCESSING/COMPLETEDの2状態をDynamoDBの条件付き
更新(batch_tracker.pyのtry_acquire_dispatch_leaseと同じパターン)で管理する。

「ロックを取得できなかった=処理済み」とは扱わない(取得できなかった側は
呼び出し元がPROCESSING/COMPLETEDを確認し、bounded retry・stale lock回復を
行う。trade_cooldown_service.py参照)。
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any

import boto3
from botocore.exceptions import ClientError

from jstock_advisor.infrastructure.collection_store import resolve_table_name, running_on_lambda

_TABLE_FILE_NAME = "trade_detection_run_locks.json"
_TTL_HOURS = 24  # 検知処理自体は当日中に完結するため、翌日以降は不要になる一時データ

_TRANSACTION_CONDITION_FAILURE_CODES = (
    "TransactionCanceledException",
    "ConditionalCheckFailedException",
    "TransactionConflictException",
)


class RunLockStatus(StrEnum):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"


def _table() -> Any:
    return boto3.resource("dynamodb").Table(resolve_table_name(_TABLE_FILE_NAME))


def try_acquire(business_date: str, now: dt.datetime, lease_seconds: int) -> bool:
    """先行Lambda(または、stale lock回復を試みる後続Lambda)がロックを獲得する。

    項目が未作成、またはPROCESSINGのままlease_expires_atを過ぎている場合のみ
    成功する。ローカル(非Lambda)環境では常にTrueを返す(単一プロセスのため
    排他不要。呼び出し側は毎回検知処理を実行してよい)。
    """
    if not running_on_lambda():
        return True
    now_iso = now.isoformat()
    lease_expires_at = (now + dt.timedelta(seconds=lease_seconds)).isoformat()
    ttl = int((now + dt.timedelta(hours=_TTL_HOURS)).timestamp())
    try:
        _table().update_item(
            Key={"business_date": business_date},
            UpdateExpression=(
                "SET #status = :processing, leased_at = :now, "
                "lease_expires_at = :expires, #ttl = :ttl"
            ),
            ConditionExpression=(
                "attribute_not_exists(#status) OR "
                "(#status = :processing AND lease_expires_at < :now)"
            ),
            ExpressionAttributeNames={"#status": "status", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":processing": RunLockStatus.PROCESSING.value,
                ":now": now_iso,
                ":expires": lease_expires_at,
                ":ttl": ttl,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


def mark_completed(business_date: str, leased_at_iso: str) -> None:
    """検知処理完了後、PROCESSING→COMPLETEDへ遷移する。

    自分が取得したリース(leased_at一致)を条件とし、リース失効後に別の
    呼び出しが横取りしていた場合は上書きしない。ローカル環境では何もしない。
    """
    if not running_on_lambda():
        return
    try:
        _table().update_item(
            Key={"business_date": business_date},
            UpdateExpression="SET #status = :completed",
            ConditionExpression="leased_at = :leased_at",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":completed": RunLockStatus.COMPLETED.value,
                ":leased_at": leased_at_iso,
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return
        raise


def get_status(business_date: str) -> tuple[str | None, str | None]:
    """(status, lease_expires_at_iso)を返す。項目が存在しなければ(None, None)。"""
    if not running_on_lambda():
        return None, None
    item = _table().get_item(Key={"business_date": business_date}).get("Item")
    if item is None:
        return None, None
    return item.get("status"), item.get("lease_expires_at")
