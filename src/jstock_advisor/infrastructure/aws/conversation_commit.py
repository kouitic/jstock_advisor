"""LINEボタン起点会話型UI(2026-08)の「登録する」確定操作を、DynamoDB
TransactWriteItemsによる単一の原子コミットとして実行する(実装プランv2 3節)。

1回のtransact_write_items呼び出しに以下をすべて含める:
  0. (BUY/SELLのみ)TradingPauseConfig.pause_buy_sellのConditionCheck(保有銘柄
     オーナー機能移行のデータ移行中、サービス層でのpause確認とこのトランザクション
     実行の間に運用者がpauseへ切り替えた場合のTOCTOU競合を排除する)
  1. ConversationStateの消費(Delete、operation_id/state/期限を条件化。追加条件2)
  2. Transaction Put(transaction_id = operation_id、決定的ID化)
  3. PurchaseLotのPut/Update/Delete(BUY/SELLのみ、既存アイテムは楽観ロック。追加条件1)
  4. HoldingのPut/Delete(BUY/SELLのみ、既存アイテムは楽観ロック。追加条件1)
  または(WATCHの場合)WatchlistItemのPut(冪等なため楽観ロック無し。pauseの
  ConditionCheckも含めない。WATCHはHoldings/PurchaseLotsを更新しないため対象外)

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
from typing import Any

from boto3.dynamodb.types import TypeSerializer

from jstock_advisor.domain.entities.enums import ConversationAction
from jstock_advisor.domain.entities.transaction import Transaction
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.infrastructure.aws import conversation_state_store, dynamodb_transaction
from jstock_advisor.infrastructure.aws.dynamodb_store import to_dynamo_item
from jstock_advisor.infrastructure.collection_store import resolve_table_name
from jstock_advisor.services.write_plan import (
    ConditionalPut,
    PurchaseWritePlan,
    SaleWritePlan,
)

_TRANSACTIONS_TABLE_FILE = "transactions.json"
_PURCHASE_LOTS_TABLE_FILE = "purchase_lots.json"
_HOLDINGS_TABLE_FILE = "holdings_v2.json"  # M3: owner/holding_id対応後のV2テーブル
_WATCHLIST_TABLE_FILE = "watchlist.json"
_TRADING_PAUSE_CONFIG_TABLE_FILE = "trading_pause_config.json"
_TRADING_PAUSE_CONFIG_ID = "trading_pause"

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






def _trading_pause_condition_check_item() -> dict[str, Any]:
    """BUY/SELL確定と同一トランザクションでTradingPauseConfig.pause_buy_sell
    を検証する(コードレビュー対応: サービス層でのpause確認とTransactWriteItems
    実行の間に運用者がpauseへ切り替えた場合のTOCTOU競合を排除する)。

    未初期化(レコード自体が存在しない)場合はpause_buy_sell=False相当として
    許可する(TradingPauseService.is_buy_sell_paused()と同じ既定値)。
    TradingPauseConfigはトップレベル属性のみで保存されている
    (trading_pause_config.py参照、data属性を経由しない)ため、pause_buy_sell
    自体を直接ConditionExpressionで参照できる。
    """
    return {
        "ConditionCheck": {
            "TableName": resolve_table_name(_TRADING_PAUSE_CONFIG_TABLE_FILE),
            "Key": {"config_id": dynamodb_transaction.serialize(_TRADING_PAUSE_CONFIG_ID)},
            "ConditionExpression": (
                "attribute_not_exists(config_id) OR pause_buy_sell = :not_paused"
            ),
            "ExpressionAttributeValues": {":not_paused": dynamodb_transaction.serialize(False)},
        }
    }






def commit_buy(
    user_id: str,
    expected_operation_id: str,
    plan: PurchaseWritePlan,
    transaction: Transaction,
    now: dt.datetime,
) -> bool:
    items = [
        _trading_pause_condition_check_item(),
        conversation_state_store.build_confirm_delete_transact_item(
            user_id, ConversationAction.BUY, expected_operation_id, now
        ),
        dynamodb_transaction.conditional_put_transact_item(
            resolve_table_name(_TRANSACTIONS_TABLE_FILE),
            ConditionalPut(model=transaction, id_field="transaction_id", expected_data=None),
        ),
        dynamodb_transaction.conditional_put_transact_item(
            resolve_table_name(_PURCHASE_LOTS_TABLE_FILE), plan.lot_put
        ),
        dynamodb_transaction.conditional_put_transact_item(
            resolve_table_name(_HOLDINGS_TABLE_FILE), plan.holding_put
        ),
    ]
    return dynamodb_transaction.commit(items)


def commit_sell(
    user_id: str,
    expected_operation_id: str,
    plan: SaleWritePlan,
    transaction: Transaction,
    now: dt.datetime,
) -> bool:
    items = [
        _trading_pause_condition_check_item(),
        conversation_state_store.build_confirm_delete_transact_item(
            user_id, ConversationAction.SELL, expected_operation_id, now
        ),
        dynamodb_transaction.conditional_put_transact_item(
            resolve_table_name(_TRANSACTIONS_TABLE_FILE),
            ConditionalPut(model=transaction, id_field="transaction_id", expected_data=None),
        ),
    ]
    lots_table = resolve_table_name(_PURCHASE_LOTS_TABLE_FILE)
    for lot_delete in plan.lot_deletes:
        items.append(
            dynamodb_transaction.conditional_delete_transact_item(lots_table, lot_delete)
        )
    for lot_put in plan.lot_puts:
        items.append(dynamodb_transaction.conditional_put_transact_item(lots_table, lot_put))

    holdings_table = resolve_table_name(_HOLDINGS_TABLE_FILE)
    if plan.holding_put is not None:
        items.append(
            dynamodb_transaction.conditional_put_transact_item(holdings_table, plan.holding_put)
        )
    if plan.holding_delete is not None:
        items.append(
            dynamodb_transaction.conditional_delete_transact_item(
                holdings_table, plan.holding_delete
            )
        )

    return dynamodb_transaction.commit(items)


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
        dynamodb_transaction.unconditional_put_transact_item(
            resolve_table_name(_WATCHLIST_TABLE_FILE),
            to_dynamo_item(watchlist_item, "stock_code"),
        ),
    ]
    return dynamodb_transaction.commit(items)
