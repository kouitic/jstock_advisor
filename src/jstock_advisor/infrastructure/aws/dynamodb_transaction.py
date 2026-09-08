"""DynamoDB TransactWriteItemsによる原子コミットの共有プリミティブ。

LINEボタン起点会話型UIの確定操作(`conversation_commit.py`、2026-08)が最初の
利用者であり、Issue #61 Phase B2(保有CSVのoverwrite取込・保有削除の原子化)が
同じ仕組みを再利用するため、両者が共有する部分だけをここへ切り出した。

**本モジュールは既存の挙動を変更しない。** `conversation_commit.py` が持っていた
privateヘルパーをそのまま移設し、同モジュールからは本モジュールを参照するだけに
した(条件式・リトライ方針・業務競合の判定基準はいずれも従来と同一)。

## 楽観ロックの条件

既存アイテムの更新・削除には、計画構築時点で読み取った`data`属性の生JSON文字列と
現在の値が完全一致することを要求する`#data = :expected_data`を必須で付与する。
計画構築からTransactWriteItems実行までの間に対象アイテムが別経路で変更された場合、
トランザクション全体を失敗させ、古い計画のままリトライしない(安全側)。
新規追加アイテムは`attribute_not_exists(PK)`を条件とする。

## リトライ方針

一時的な`TransactionConflictException`のみ短いバックオフでリトライし、
`ConditionalCheckFailedException`等の本物の条件不成立はリトライしない。
`batch_tracker.py`側にも同じパターンの実装があるが、そちらは疎結合を保つための
意図的な複製である(実装プランv2の設計判断)ため、本モジュールへは統合しない。
"""

from __future__ import annotations

import random
import time
from typing import Any

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from jstock_advisor.infrastructure.aws.dynamodb_store import to_dynamo_item
from jstock_advisor.services.write_plan import ConditionalDelete, ConditionalPut

_CONDITION_FAILURE_TOP_LEVEL_CODES = frozenset(
    {"ConditionalCheckFailedException", "TransactionConflictException"}
)
# TransactionCanceledExceptionのCancellationReasons側コード。"None"は当該アイテム
# 自体は失敗要因ではないことを示すDynamoDBの規約値(実際の理由コードではない)。
_SAFE_CANCELLATION_REASON_CODES = frozenset({"None", "ConditionalCheckFailed"})
_RETRYABLE_TRANSACTION_CONFLICT_CODES = frozenset({"TransactionConflictException"})
_TRANSACTION_CONFLICT_RETRY_BASE_DELAY_SECONDS = 0.05

_serializer = TypeSerializer()

MAX_TRANSACT_ITEMS = 100
"""DynamoDB TransactWriteItemsの1リクエストあたりの上限(AWSの仕様値)。

供給側の上限であり、業務上許容する上限(supported-domain)とは別物である。
業務側の上限は呼び出し元が定義する(Issue #61 Phase B2では
`portfolio_service.MAX_LOTS_PER_HOLDING`)。
"""

_MAX_TRANSACTION_CONFLICT_RETRY_ATTEMPTS = 4


def serialize(value: Any) -> Any:
    """Python値をDynamoDBのAttributeValue表現へ変換する。"""
    return _serializer.serialize(value)


def is_retryable_transaction_conflict(error: ClientError) -> bool:
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


def transact_write_items_with_conflict_retry(
    client: Any, transact_items: list[dict[str, Any]]
) -> None:
    delay = _TRANSACTION_CONFLICT_RETRY_BASE_DELAY_SECONDS
    for attempt in range(1, _MAX_TRANSACTION_CONFLICT_RETRY_ATTEMPTS + 1):
        try:
            client.transact_write_items(TransactItems=transact_items)
            return
        except ClientError as e:
            is_last_attempt = attempt >= _MAX_TRANSACTION_CONFLICT_RETRY_ATTEMPTS
            if is_last_attempt or not is_retryable_transaction_conflict(e):
                raise
            time.sleep(delay + random.uniform(0, delay))
            delay *= 2


def conditional_put_transact_item(table_name: str, put: ConditionalPut) -> dict[str, Any]:
    """ConditionalPutをTransactWriteItemsのPut項目へ変換する。

    expected_data=Noneは新規追加(attribute_not_exists(PK))、
    それ以外は既存アイテムの楽観ロック更新(#data = :expected_data)。
    """
    item = to_dynamo_item(put.model, put.id_field)
    if put.expected_data is None:
        return {
            "Put": {
                "TableName": table_name,
                "Item": {k: serialize(v) for k, v in item.items()},
                "ConditionExpression": "attribute_not_exists(#pk)",
                "ExpressionAttributeNames": {"#pk": put.id_field},
            }
        }
    return {
        "Put": {
            "TableName": table_name,
            "Item": {k: serialize(v) for k, v in item.items()},
            "ConditionExpression": "#data = :expected_data",
            "ExpressionAttributeNames": {"#data": "data"},
            "ExpressionAttributeValues": {":expected_data": serialize(put.expected_data)},
        }
    }

def conditional_delete_transact_item(
    table_name: str, delete: ConditionalDelete
) -> dict[str, Any]:
    """ConditionalDeleteをTransactWriteItemsのDelete項目へ変換する。"""
    return {
        "Delete": {
            "TableName": table_name,
            "Key": {delete.id_field: serialize(delete.id_value)},
            "ConditionExpression": "#data = :expected_data",
            "ExpressionAttributeNames": {"#data": "data"},
            "ExpressionAttributeValues": {":expected_data": serialize(delete.expected_data)},
        }
    }


def unconditional_put_transact_item(table_name: str, item: dict[str, Any]) -> dict[str, Any]:
    """条件を付与しないPut項目(冪等なupsertにのみ使う)。"""
    return {
        "Put": {
            "TableName": table_name,
            "Item": {k: serialize(v) for k, v in item.items()},
        }
    }

def is_safe_business_conflict(error: ClientError) -> bool:
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


def commit(transact_items: list[dict[str, Any]]) -> bool:
    """成功時True。安全な業務競合(楽観ロック競合・条件不成立)時はFalse。

    スロットリング・内部障害・権限不足等の非業務エラーはFalseへ変換せず、
    そのまま例外を伝播する。
    """
    client = boto3.client("dynamodb")
    try:
        transact_write_items_with_conflict_retry(client, transact_items)
        return True
    except ClientError as e:
        if is_safe_business_conflict(e):
            return False
        raise
