"""NEW_CANDIDATE_SCREENINGのrotation window二重dispatch防止用の軽量lease
(本番検証2026-08で発覚した二重起動対応)。

本番で約50秒差の2回のDispatcher起動が、同一rotation_id="default"の永続
カーソル(watchlist_rotation_state.py)を両方ともcursor未前進の状態で読み、
同一300銘柄windowを二重にSQSへdispatchした。rotation cursorのCAS
(pointer_version楽観ロック)は「cursorの二重前進」は防ぐが、「同じwindowの
二重評価」自体は防げないため、本モジュールで別責務として排他制御する。

trade_detection_lock.py(TradeDetectionRunLock)と同じ、単一行への条件付き
update_item(attribute_not_exists OR lease_expires_at超過)パターンを踏襲する。
rotation cursor自体のCASとは完全に別のテーブル・別の責務であり、本モジュールを
導入してもpointer_version CASは削除しない(両方を維持する)。

WATCHLIST_MAINTENANCEはこのleaseの対象外(呼び出し側が
job_type=="NEW_CANDIDATE_SCREENING"の場合のみtry_acquire()を呼ぶこと)。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import boto3
from botocore.exceptions import ClientError

from jstock_advisor.infrastructure.collection_store import resolve_table_name, running_on_lambda

_TABLE_FILE_NAME = "watchlist_rotation_dispatch_lease.json"

# leaseそのものの有効期限(lease_expires_at)とは無関係の、テーブル衛生用TTL余裕。
_TTL_BUFFER_SECONDS = 7 * 24 * 3600

_TRANSACTION_CONDITION_FAILURE_CODES = (
    "TransactionCanceledException",
    "ConditionalCheckFailedException",
    "TransactionConflictException",
)


def _table() -> Any:
    return boto3.resource("dynamodb").Table(resolve_table_name(_TABLE_FILE_NAME))


def try_acquire_rotation_dispatch_lease(
    rotation_id: str, batch_id: str, now: dt.datetime, lease_seconds: int
) -> bool:
    """rotation windowのdispatch leaseを取得する。

    項目が未作成、または既存leaseのlease_expires_atを過ぎている(stale、
    Lambda異常終了等で解放されなかった)場合のみ取得できる。取得成功時は
    in_progress_batch_id/lease_started_at/lease_expires_atを設定する。

    ローカル(非Lambda)環境では常にTrue(単一プロセスのCLI利用のため排他不要)。
    """
    if not running_on_lambda():
        return True
    now_iso = now.isoformat()
    lease_expires_at = (now + dt.timedelta(seconds=lease_seconds)).isoformat()
    ttl = int((now + dt.timedelta(seconds=lease_seconds + _TTL_BUFFER_SECONDS)).timestamp())
    try:
        _table().update_item(
            Key={"rotation_id": rotation_id},
            UpdateExpression=(
                "SET in_progress_batch_id = :batch_id, lease_started_at = :now, "
                "lease_expires_at = :expires, #ttl = :ttl"
            ),
            ConditionExpression=(
                "attribute_not_exists(in_progress_batch_id) OR lease_expires_at < :now"
            ),
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":batch_id": batch_id,
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


def release_rotation_dispatch_lease(rotation_id: str, batch_id: str) -> None:
    """自分が取得したlease(in_progress_batch_id一致)のみ解放する。

    既に失効・別batchが取得済み・そもそも未取得の場合は何もしない(誤って
    他batchが正当に保持しているleaseを奪わないため)。バッチが正常/異常
    いずれの終端状態に至った場合も呼んでよい安全な操作(idempotent)。
    ローカル環境では何もしない。
    """
    if not running_on_lambda():
        return
    try:
        _table().update_item(
            Key={"rotation_id": rotation_id},
            UpdateExpression="REMOVE in_progress_batch_id, lease_started_at, lease_expires_at",
            ConditionExpression="in_progress_batch_id = :batch_id",
            ExpressionAttributeValues={":batch_id": batch_id},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return
        raise


def get_rotation_dispatch_lease_status(
    rotation_id: str,
) -> tuple[str | None, str | None, str | None]:
    """(in_progress_batch_id, lease_started_at, lease_expires_at)を返す。

    項目が存在しない、またはリース未保持の場合は(None, None, None)。監査ログで
    「誰がactive batchか」を記録する専用(判定ロジックには使わない)。ローカル
    環境では常に(None, None, None)。
    """
    if not running_on_lambda():
        return None, None, None
    item = _table().get_item(Key={"rotation_id": rotation_id}).get("Item")
    if item is None:
        return None, None, None
    return (
        item.get("in_progress_batch_id"),
        item.get("lease_started_at"),
        item.get("lease_expires_at"),
    )
