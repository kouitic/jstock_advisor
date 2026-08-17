"""ConversationStateStore: LINEボタン起点の会話型UI(2026-08)における
一時的な対話状態(BUY/SELL/WATCHの入力待ち・確認待ち)の永続化。

infrastructure/aws/trade_detection_lock.pyと同型の、フラット属性・生boto3の
専用モジュールとして実装する(build_collection_store経由の汎用CRUD層は
使わない)。理由: DynamoDbCollectionStoreは各アイテムを`{id_field, data}`の
JSONブロブとして保存し、`ttl_seconds`で付与するttl属性もdata属性の外側に
物理的に保存されるため、`get()`で呼び出し元へ一切返らない(dynamodb_store.py
参照)。本モジュールはDynamoDB Native TTL(物理削除・掃除用途、期限到達後
最大48時間程度残存しうる)とは独立に、`ttl <= 現在時刻`をアプリケーション層
でも判定し、「会話状態あり/なし」の実際の判定に使う(実装プランv2 3節)。

「confirm(登録する)」ボタン押下時のConversationState消費は、このモジュールの
単独update_item/delete_itemでは行わない。TransactWriteItems(Transaction/
PurchaseLot/Holding等の原子コミット)に含める1本のDeleteアクションとして
conversation_commit.pyが実行するため、build_confirm_delete_transact_item()が
そのDeleteアクションの構築のみを担う(DynamoDBのTransactWriteItemsは同一
アイテムに対するConditionCheckとDeleteを同時に指定できないため、確認条件と
削除を1つのDeleteアクションへ統合する。実装プランv2追加条件2)。

ローカル(非Lambda)環境向けのフォールバックは持たない。この対話状態は
LINE Webhookハンドラ(Lambda専用)からのみ参照されるため、trade_detection_
lock.pyのようなrunning_on_lambda()分岐は不要(テストはtrade_detection_lockの
テストと同様、フェイクTable/フェイクResourceで検証する)。
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from jstock_advisor.domain.entities.enums import ConversationAction, ConversationStateName
from jstock_advisor.infrastructure.collection_store import resolve_table_name

_TABLE_FILE_NAME = "conversation_states.json"
# 対話1件が完結するまでの猶予(要求仕様: 10〜30分の範囲で選定)。
TTL_SECONDS = 20 * 60

_CONDITION_FAILURE_CODES = (
    "TransactionCanceledException",
    "ConditionalCheckFailedException",
    "TransactionConflictException",
)

_serializer = TypeSerializer()


def _ser(value: Any) -> Any:
    return _serializer.serialize(value)


@dataclass(frozen=True)
class ConversationState:
    user_id: str
    action: ConversationAction
    state: ConversationStateName
    operation_id: str
    stock_code: str | None
    shares: int | None
    price: Decimal | None
    created_at: dt.datetime
    updated_at: dt.datetime
    ttl: int


def _table() -> Any:
    return boto3.resource("dynamodb").Table(resolve_table_name(_TABLE_FILE_NAME))


def _table_name() -> str:
    return resolve_table_name(_TABLE_FILE_NAME)


def _from_item(item: dict[str, Any]) -> ConversationState:
    return ConversationState(
        user_id=item["user_id"],
        action=ConversationAction(item["action"]),
        state=ConversationStateName(item["state"]),
        operation_id=item["operation_id"],
        stock_code=item.get("stock_code"),
        shares=int(item["shares"]) if item.get("shares") is not None else None,
        price=item.get("price"),
        created_at=dt.datetime.fromisoformat(item["created_at"]),
        updated_at=dt.datetime.fromisoformat(item["updated_at"]),
        ttl=int(item["ttl"]),
    )


def get(user_id: str, now: dt.datetime) -> ConversationState | None:
    """物理的にレコードが残っていても、`ttl <= now`ならアプリ層で
    「会話状態なし」として扱う(DynamoDB Native TTLの物理削除タイミングに
    依存しない)。"""
    item = _table().get_item(Key={"user_id": user_id}).get("Item")
    if item is None:
        return None
    if int(item["ttl"]) <= int(now.timestamp()):
        return None
    return _from_item(item)


def start_or_replace(
    user_id: str, action: ConversationAction, now: dt.datetime
) -> ConversationState:
    """新規対話の開始(常に上書き、既存Pendingの有無に関わらず成功する)。"""
    operation_id = str(uuid.uuid4())
    now_iso = now.isoformat()
    ttl = int(now.timestamp()) + TTL_SECONDS
    item: dict[str, Any] = {
        "user_id": user_id,
        "action": action.value,
        "state": ConversationStateName.INPUT_WAITING.value,
        "operation_id": operation_id,
        "created_at": now_iso,
        "updated_at": now_iso,
        "ttl": ttl,
    }
    _table().put_item(Item=item)
    return _from_item(item)


def record_input(
    user_id: str,
    expected_action: ConversationAction,
    stock_code: str,
    now: dt.datetime,
    shares: int | None = None,
    price: Decimal | None = None,
) -> ConversationState | None:
    """INPUT_WAITING→CONFIRM_WAITING。actionが一致し、状態がINPUT_WAITING、
    かつ`ttl > now`の場合のみ成功する(期限切れは物理的に残っていても条件不成立
    として扱う)。成功時は新しいoperation_idを発行する。条件不成立時はNoneを返す。

    shares/priceはWATCH(銘柄コードのみ)ではNoneのまま渡す(該当属性を
    REMOVEし、以前の対話で残っていた値をクリアする)。
    """
    operation_id = str(uuid.uuid4())
    now_iso = now.isoformat()
    now_epoch = int(now.timestamp())
    ttl = now_epoch + TTL_SECONDS

    set_clauses = [
        "#state = :confirm_waiting",
        "#stock_code = :stock_code",
        "#operation_id = :new_op",
        "#updated_at = :now",
        "#ttl = :ttl",
    ]
    remove_clauses: list[str] = []
    values: dict[str, Any] = {
        ":expected_action": expected_action.value,
        ":input_waiting": ConversationStateName.INPUT_WAITING.value,
        ":confirm_waiting": ConversationStateName.CONFIRM_WAITING.value,
        ":stock_code": stock_code,
        ":new_op": operation_id,
        ":now": now_iso,
        ":now_epoch": now_epoch,
        ":ttl": ttl,
    }
    if shares is not None:
        set_clauses.append("#shares = :shares")
        values[":shares"] = shares
    else:
        remove_clauses.append("#shares")
    if price is not None:
        set_clauses.append("#price = :price")
        values[":price"] = price
    else:
        remove_clauses.append("#price")

    update_expression = "SET " + ", ".join(set_clauses)
    if remove_clauses:
        update_expression += " REMOVE " + ", ".join(remove_clauses)

    # DynamoDBはExpressionAttributeNamesに実際の式で参照されていないエントリが
    # 1つでもあるとValidationExceptionを送出するため、この呼び出しで実際に
    # 使う名前だけに絞る(#shares/#priceはSET/REMOVEいずれかに必ず含まれるが、
    # #created_atはこの関数では一切参照しないため含めない)。
    names = {
        "#action": "action",
        "#state": "state",
        "#stock_code": "stock_code",
        "#operation_id": "operation_id",
        "#updated_at": "updated_at",
        "#ttl": "ttl",
        "#shares": "shares",
        "#price": "price",
    }

    try:
        _table().update_item(
            Key={"user_id": user_id},
            UpdateExpression=update_expression,
            ConditionExpression=(
                "#action = :expected_action AND #state = :input_waiting AND #ttl > :now_epoch"
            ),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] in _CONDITION_FAILURE_CODES:
            return None
        raise
    return get(user_id, now)


def retry(
    user_id: str,
    expected_action: ConversationAction,
    expected_operation_id: str,
    now: dt.datetime,
) -> ConversationState | None:
    """CONFIRM_WAITING→INPUT_WAITING(入力し直し)。stock_code/shares/priceを
    クリアし、operation_idを再発行する。operation_id・state・期限のいずれかが
    一致しない場合は何も変更せずNoneを返す。"""
    new_operation_id = str(uuid.uuid4())
    now_iso = now.isoformat()
    now_epoch = int(now.timestamp())
    ttl = now_epoch + TTL_SECONDS
    try:
        _table().update_item(
            Key={"user_id": user_id},
            UpdateExpression=(
                "SET #state = :input_waiting, #operation_id = :new_op, "
                "#updated_at = :now, #ttl = :ttl "
                "REMOVE #stock_code, #shares, #price"
            ),
            ConditionExpression=(
                "#action = :expected_action AND #state = :confirm_waiting "
                "AND #operation_id = :expected_op AND #ttl > :now_epoch"
            ),
            ExpressionAttributeNames={
                "#action": "action",
                "#state": "state",
                "#operation_id": "operation_id",
                "#updated_at": "updated_at",
                "#ttl": "ttl",
                "#stock_code": "stock_code",
                "#shares": "shares",
                "#price": "price",
            },
            ExpressionAttributeValues={
                ":expected_action": expected_action.value,
                ":confirm_waiting": ConversationStateName.CONFIRM_WAITING.value,
                ":input_waiting": ConversationStateName.INPUT_WAITING.value,
                ":expected_op": expected_operation_id,
                ":new_op": new_operation_id,
                ":now": now_iso,
                ":now_epoch": now_epoch,
                ":ttl": ttl,
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] in _CONDITION_FAILURE_CODES:
            return None
        raise
    return get(user_id, now)


def discard_input(
    user_id: str,
    expected_action: ConversationAction,
    expected_operation_id: str,
    now: dt.datetime,
) -> bool:
    """INPUT_WAITING中の対話を破棄する(条件付き削除)。cancel()はCONFIRM_WAITING
    専用の破棄(ボタンの「キャンセル」)のため使えない。既にウォッチリスト
    登録済みの銘柄が入力された場合等、確認画面へ進めずにINPUT_WAITINGの
    まま対話を終了させたいケースで使う(コードレビュー2026-08-17再指摘)。"""
    now_epoch = int(now.timestamp())
    try:
        _table().delete_item(
            Key={"user_id": user_id},
            ConditionExpression=(
                "#action = :expected_action AND #state = :input_waiting "
                "AND #operation_id = :expected_op AND #ttl > :now_epoch"
            ),
            ExpressionAttributeNames={
                "#action": "action",
                "#state": "state",
                "#operation_id": "operation_id",
                "#ttl": "ttl",
            },
            ExpressionAttributeValues={
                ":expected_action": expected_action.value,
                ":input_waiting": ConversationStateName.INPUT_WAITING.value,
                ":expected_op": expected_operation_id,
                ":now_epoch": now_epoch,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _CONDITION_FAILURE_CODES:
            return False
        raise


def cancel(user_id: str, expected_operation_id: str, now: dt.datetime) -> bool:
    """CONFIRM_WAITING中の対話を破棄する(条件付き削除)。"""
    now_epoch = int(now.timestamp())
    try:
        _table().delete_item(
            Key={"user_id": user_id},
            ConditionExpression=(
                "#state = :confirm_waiting AND #operation_id = :expected_op AND #ttl > :now_epoch"
            ),
            ExpressionAttributeNames={
                "#state": "state",
                "#operation_id": "operation_id",
                "#ttl": "ttl",
            },
            ExpressionAttributeValues={
                ":confirm_waiting": ConversationStateName.CONFIRM_WAITING.value,
                ":expected_op": expected_operation_id,
                ":now_epoch": now_epoch,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _CONDITION_FAILURE_CODES:
            return False
        raise


def build_confirm_delete_transact_item(
    user_id: str,
    expected_action: ConversationAction,
    expected_operation_id: str,
    now: dt.datetime,
) -> dict[str, Any]:
    """confirm(登録する)実行時、TransactWriteItemsへ含める単一Deleteアクション
    (低レベルDynamoDB API形式)を構築する。

    ConditionCheckとDeleteは同一アイテムに同時指定できないため、確認条件
    (action・state・operation_id・期限)をDelete自体のConditionExpressionへ
    付与する(実装プランv2追加条件2)。呼び出し元(conversation_commit.py)は
    このアイテムを他のPut/Update/Deleteアイテムと同一のtransact_write_items()
    呼び出しへそのまま含めればよい。
    """
    now_epoch = int(now.timestamp())
    return {
        "Delete": {
            "TableName": _table_name(),
            "Key": {"user_id": _ser(user_id)},
            "ConditionExpression": (
                "#action = :expected_action AND #state = :confirm_waiting "
                "AND #operation_id = :expected_op AND #ttl > :now_epoch"
            ),
            "ExpressionAttributeNames": {
                "#action": "action",
                "#state": "state",
                "#operation_id": "operation_id",
                "#ttl": "ttl",
            },
            "ExpressionAttributeValues": {
                ":expected_action": _ser(expected_action.value),
                ":confirm_waiting": _ser(ConversationStateName.CONFIRM_WAITING.value),
                ":expected_op": _ser(expected_operation_id),
                ":now_epoch": _ser(now_epoch),
            },
        }
    }
