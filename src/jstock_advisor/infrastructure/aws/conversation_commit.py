"""LINEボタン起点会話型UI(2026-08)の「登録する」確定操作を、DynamoDB
TransactWriteItemsによる単一の原子コミットとして実行する(実装プランv2 3節)。

1回のtransact_write_items呼び出しに以下をすべて含める:
  1. ConversationStateの消費(Delete、operation_id/state/期限を条件化。追加条件2)
  2. Transaction Put(transaction_id = operation_id、決定的ID化)
  3. PurchaseLotのPut/Update/Delete(BUY/SELLのみ、既存アイテムは楽観ロック。追加条件1)
  4. HoldingのPut/Delete(BUY/SELLのみ、既存アイテムは楽観ロック。追加条件1)
  または(WATCHの場合)WatchlistItemのPut(冪等なため楽観ロック無し)

DynamoDBが「全部成功 or 全部不成功」を保証するため、「Transactionのみ登録
済みでHoldingsが未更新」のような部分状態は構造的に発生しない。

既存アイテム(Holding/PurchaseLot)の更新・削除には、計画構築時点で読み取った
`data`属性の生JSON文字列と現在の値が完全一致することを要求する
ConditionExpression(#data = :expected_data)を必須で付与する(実装プラン
v2追加条件1: 計画構築からTransactWriteItems実行までの間に対象アイテムが
別経路で変更された場合、トランザクション全体を失敗させ、古い計画のまま
リトライしない安全側の設計)。新規追加アイテムはattribute_not_exists(PK)を
条件とする。

リトライ方針はbatch_tracker.py の`_transact_write_items_with_conflict_retry()`
と同じパターン(TransactionConflictExceptionのみ短いバックオフでリトライし、
ConditionalCheckFailedException等の本物の条件不成立はリトライしない)。
batch_tracker.py側のプライベート関数は再利用せず複製する(疎結合を保つ
ため。ロジック自体は小さい関数のため重複のコストは低いと判断。実装プラン
v2の設計判断)。
"""

from __future__ import annotations

import datetime as dt
import random
import time
from typing import Any

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from jstock_advisor.domain.entities.enums import ConversationAction
from jstock_advisor.domain.entities.transaction import Transaction
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.infrastructure.aws import conversation_state_store
from jstock_advisor.infrastructure.aws.dynamodb_store import to_dynamo_item
from jstock_advisor.infrastructure.collection_store import resolve_table_name
from jstock_advisor.services.write_plan import (
    ConditionalDelete,
    ConditionalPut,
    PurchaseWritePlan,
    SaleWritePlan,
)

_TRANSACTIONS_TABLE_FILE = "transactions.json"
_PURCHASE_LOTS_TABLE_FILE = "purchase_lots.json"
_HOLDINGS_TABLE_FILE = "holdings.json"
_WATCHLIST_TABLE_FILE = "watchlist.json"

_CONDITION_FAILURE_TOP_LEVEL_CODES = frozenset(
    {"ConditionalCheckFailedException", "TransactionConflictException"}
)
# TransactionCanceledExceptionのCancellationReasons側コード。"None"は当該アイテム
# 自体は失敗要因ではないことを示すDynamoDBの規約値(実際の理由コードではない)。
_SAFE_CANCELLATION_REASON_CODES = frozenset({"None", "ConditionalCheckFailed"})
_RETRYABLE_TRANSACTION_CONFLICT_CODES = frozenset({"TransactionConflictException"})
_MAX_TRANSACTION_CONFLICT_RETRY_ATTEMPTS = 4
_TRANSACTION_CONFLICT_RETRY_BASE_DELAY_SECONDS = 0.05

_serializer = TypeSerializer()


def _ser(value: Any) -> Any:
    return _serializer.serialize(value)


def _is_retryable_transaction_conflict(error: ClientError) -> bool:
    """batch_tracker.py の同名関数と同じ判定ロジック(3節docstring参照)。"""
    code = error.response["Error"]["Code"]
    if code in _RETRYABLE_TRANSACTION_CONFLICT_CODES:
        return True
    if code != "TransactionCanceledException":
        return False
    reasons = error.response.get("CancellationReasons") or []
    reason_codes = {reason.get("Code") for reason in reasons}
    if "ConditionalCheckFailed" in reason_codes:
        return False
    return "TransactionConflict" in reason_codes


def _transact_write_items_with_conflict_retry(
    client: Any, transact_items: list[dict[str, Any]]
) -> None:
    delay = _TRANSACTION_CONFLICT_RETRY_BASE_DELAY_SECONDS
    for attempt in range(1, _MAX_TRANSACTION_CONFLICT_RETRY_ATTEMPTS + 1):
        try:
            client.transact_write_items(TransactItems=transact_items)
            return
        except ClientError as e:
            is_last_attempt = attempt >= _MAX_TRANSACTION_CONFLICT_RETRY_ATTEMPTS
            if is_last_attempt or not _is_retryable_transaction_conflict(e):
                raise
            time.sleep(delay + random.uniform(0, delay))
            delay *= 2


def _conditional_put_transact_item(table_name: str, put: ConditionalPut) -> dict[str, Any]:
    item = to_dynamo_item(put.model, put.id_field)
    if put.expected_data is None:
        return {
            "Put": {
                "TableName": table_name,
                "Item": {k: _ser(v) for k, v in item.items()},
                "ConditionExpression": "attribute_not_exists(#pk)",
                "ExpressionAttributeNames": {"#pk": put.id_field},
            }
        }
    return {
        "Put": {
            "TableName": table_name,
            "Item": {k: _ser(v) for k, v in item.items()},
            "ConditionExpression": "#data = :expected_data",
            "ExpressionAttributeNames": {"#data": "data"},
            "ExpressionAttributeValues": {":expected_data": _ser(put.expected_data)},
        }
    }


def _unconditional_put_transact_item(table_name: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "Put": {
            "TableName": table_name,
            "Item": {k: _ser(v) for k, v in item.items()},
        }
    }


def _conditional_delete_transact_item(table_name: str, delete: ConditionalDelete) -> dict[str, Any]:
    return {
        "Delete": {
            "TableName": table_name,
            "Key": {delete.id_field: _ser(delete.id_value)},
            "ConditionExpression": "#data = :expected_data",
            "ExpressionAttributeNames": {"#data": "data"},
            "ExpressionAttributeValues": {":expected_data": _ser(delete.expected_data)},
        }
    }


def _is_safe_business_conflict(error: ClientError) -> bool:
    """安全に「もう一度操作してください」へ変換してよい業務競合か判定する
    (コードレビュー2026-08-17 指摘3)。

    ConditionalCheckFailedException/TransactionConflictException(トップ
    レベルのエラーコード)はそのままTrue。TransactionCanceledExceptionは
    CancellationReasonsの中身を見て、すべての理由が"None"(そのアイテム自体
    は失敗要因ではない)または"ConditionalCheckFailed"(楽観ロック競合・
    ConversationState条件不成立)である場合のみTrueとする。スロットリング・
    内部障害・権限不足・ValidationError等、それ以外の理由が1つでも含まれる
    場合は業務競合として握りつぶさず、呼び出し元へ例外を伝播させる。
    """
    code = error.response["Error"]["Code"]
    if code in _CONDITION_FAILURE_TOP_LEVEL_CODES:
        return True
    if code != "TransactionCanceledException":
        return False
    reasons = error.response.get("CancellationReasons") or []
    reason_codes = {reason.get("Code") for reason in reasons}
    return bool(reason_codes) and reason_codes <= _SAFE_CANCELLATION_REASON_CODES


def _commit(transact_items: list[dict[str, Any]]) -> bool:
    """成功時True。安全な業務競合(operation_id/state/期限不一致・楽観ロック
    競合・二重押下)時はFalseを返す(呼び出し側が「もう一度操作してください」
    等の安全側の案内を返す)。スロットリング・内部障害・権限不足等の非業務
    エラーはFalseへ変換せず、そのまま例外を伝播する(指摘3)。
    """
    client = boto3.client("dynamodb")
    try:
        _transact_write_items_with_conflict_retry(client, transact_items)
        return True
    except ClientError as e:
        if _is_safe_business_conflict(e):
            return False
        raise


def commit_buy(
    user_id: str,
    expected_operation_id: str,
    plan: PurchaseWritePlan,
    transaction: Transaction,
    now: dt.datetime,
) -> bool:
    items = [
        conversation_state_store.build_confirm_delete_transact_item(
            user_id, ConversationAction.BUY, expected_operation_id, now
        ),
        _conditional_put_transact_item(
            resolve_table_name(_TRANSACTIONS_TABLE_FILE),
            ConditionalPut(model=transaction, id_field="transaction_id", expected_data=None),
        ),
        _conditional_put_transact_item(resolve_table_name(_PURCHASE_LOTS_TABLE_FILE), plan.lot_put),
        _conditional_put_transact_item(resolve_table_name(_HOLDINGS_TABLE_FILE), plan.holding_put),
    ]
    return _commit(items)


def commit_sell(
    user_id: str,
    expected_operation_id: str,
    plan: SaleWritePlan,
    transaction: Transaction,
    now: dt.datetime,
) -> bool:
    items = [
        conversation_state_store.build_confirm_delete_transact_item(
            user_id, ConversationAction.SELL, expected_operation_id, now
        ),
        _conditional_put_transact_item(
            resolve_table_name(_TRANSACTIONS_TABLE_FILE),
            ConditionalPut(model=transaction, id_field="transaction_id", expected_data=None),
        ),
    ]
    lots_table = resolve_table_name(_PURCHASE_LOTS_TABLE_FILE)
    for lot_delete in plan.lot_deletes:
        items.append(_conditional_delete_transact_item(lots_table, lot_delete))
    for lot_put in plan.lot_puts:
        items.append(_conditional_put_transact_item(lots_table, lot_put))

    holdings_table = resolve_table_name(_HOLDINGS_TABLE_FILE)
    if plan.holding_put is not None:
        items.append(_conditional_put_transact_item(holdings_table, plan.holding_put))
    if plan.holding_delete is not None:
        items.append(_conditional_delete_transact_item(holdings_table, plan.holding_delete))

    return _commit(items)


def commit_watch(
    user_id: str,
    expected_operation_id: str,
    watchlist_item: WatchlistItem,
    now: dt.datetime,
) -> bool:
    """WatchlistService.add_item()と同じくupsertで自然に冪等なため、
    WatchlistItem自体には楽観ロック条件を付与しない(実装プランv2 3節)。
    ConversationStateのclaim消費とウォッチリスト登録を同一トランザクション
    にすることで、片方だけ成立する状態を避ける点のみが目的。
    """
    items = [
        conversation_state_store.build_confirm_delete_transact_item(
            user_id, ConversationAction.WATCH, expected_operation_id, now
        ),
        _unconditional_put_transact_item(
            resolve_table_name(_WATCHLIST_TABLE_FILE),
            to_dynamo_item(watchlist_item, "stock_code"),
        ),
    ]
    return _commit(items)
