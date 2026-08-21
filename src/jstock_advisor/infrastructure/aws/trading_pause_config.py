"""TradingPauseConfigの永続化(保有銘柄オーナー機能移行の書込停止フラグ)。

HoldingDecisionRuntimeConfig/InvestmentThesisBaselinePointerと同じ「再デプロイ
不要でCLIから切り替える」という考え方のみを踏襲する。ただしそれらの既存実装は
create(CollectionStore経由・dataブロブ)とupdate(生boto3・トップレベル属性の
ConditionExpression/UpdateExpression)とで永続化形式が一致しておらず、update側が
参照するトップレベル属性がcreate側では書き込まれないため、初回更新が常に
ConditionalCheckFailedExceptionになる可能性がある既知の不整合を抱えている
(保有銘柄オーナー機能プランv3・v4で確認済み)。

本モジュールはその不整合を再現しない。DynamoDB版のcreate/get/updateを
すべてトップレベル属性のみで一貫させ、data属性は一切使わない。ローカル
(非Lambda)環境はJsonCollectionStoreをそのまま使う(モデル全体を1つの
JSONオブジェクトとして保存するため、この種の不整合が構造的に存在しない)。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from jstock_advisor.domain.entities.trading_pause import TradingPauseConfig
from jstock_advisor.infrastructure.collection_store import (
    CollectionStore,
    build_collection_store,
    resolve_table_name,
    running_on_lambda,
)

_TABLE_FILE_NAME = "trading_pause_config.json"
_CONFIG_ID = "trading_pause"


class TradingPauseConflictError(Exception):
    """他の更新が先に行われたため、または初回作成が既に行われているため失敗した。"""


def _local_store(store_dir: Path | None = None) -> CollectionStore[TradingPauseConfig]:
    return build_collection_store(TradingPauseConfig, _TABLE_FILE_NAME, "config_id", store_dir)


def _to_dynamo_item(config: TradingPauseConfig) -> dict[str, Any]:
    return {
        "config_id": config.config_id,
        "config_version": config.config_version,
        "pause_buy_sell": config.pause_buy_sell,
        "updated_at": config.updated_at.isoformat(),
        "updated_by": config.updated_by,
        "change_reason": config.change_reason,
    }


def _from_dynamo_item(item: dict[str, Any]) -> TradingPauseConfig:
    return TradingPauseConfig(
        config_id=str(item["config_id"]),
        config_version=int(item["config_version"]),
        pause_buy_sell=bool(item["pause_buy_sell"]),
        updated_at=dt.datetime.fromisoformat(item["updated_at"]),
        updated_by=str(item["updated_by"]),
        change_reason=str(item["change_reason"]),
    )


def get(store_dir: Path | None = None) -> TradingPauseConfig | None:
    if running_on_lambda():
        return _get_dynamodb()
    return _local_store(store_dir).get(_CONFIG_ID)


def _get_dynamodb() -> TradingPauseConfig | None:
    import boto3

    table: Any = boto3.resource("dynamodb").Table(resolve_table_name(_TABLE_FILE_NAME))
    response = table.get_item(Key={"config_id": _CONFIG_ID})
    item = response.get("Item")
    return _from_dynamo_item(item) if item is not None else None


def init(
    pause_buy_sell: bool,
    updated_by: str,
    change_reason: str,
    now: dt.datetime | None = None,
    store_dir: Path | None = None,
) -> TradingPauseConfig | None:
    """初回作成。既に存在する場合はNoneを返す(既存値は変更しない)。"""
    config = TradingPauseConfig(
        config_version=1,
        pause_buy_sell=pause_buy_sell,
        updated_at=now or dt.datetime.now(dt.UTC),
        updated_by=updated_by,
        change_reason=change_reason,
    )
    if running_on_lambda():
        created = _init_dynamodb(config)
    else:
        created = _local_store(store_dir).insert_if_absent(config)
    return config if created else None


def _init_dynamodb(config: TradingPauseConfig) -> bool:
    import boto3
    from botocore.exceptions import ClientError

    table: Any = boto3.resource("dynamodb").Table(resolve_table_name(_TABLE_FILE_NAME))
    try:
        table.put_item(
            Item=_to_dynamo_item(config),
            ConditionExpression="attribute_not_exists(config_id)",
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def update(
    expected_config_version: int,
    pause_buy_sell: bool,
    updated_by: str,
    change_reason: str,
    now: dt.datetime | None = None,
    store_dir: Path | None = None,
) -> TradingPauseConfig:
    """expected_config_versionが現在値と一致する場合のみ更新する(楽観ロック)。

    一致しない場合はTradingPauseConflictErrorを送出する(自動リトライしない、
    呼び出し側=CLIが最新値を再取得して人間へ再確認を促す)。
    """
    if running_on_lambda():
        return _update_dynamodb(
            expected_config_version, pause_buy_sell, updated_by, change_reason, now
        )
    return _update_local(
        expected_config_version, pause_buy_sell, updated_by, change_reason, now, store_dir
    )


def _update_local(
    expected_config_version: int,
    pause_buy_sell: bool,
    updated_by: str,
    change_reason: str,
    now: dt.datetime | None,
    store_dir: Path | None,
) -> TradingPauseConfig:
    store = _local_store(store_dir)
    current = store.get(_CONFIG_ID)
    if current is None or current.config_version != expected_config_version:
        raise TradingPauseConflictError(
            f"config_versionが期待値(expected={expected_config_version})と一致しません"
            f"(現在={current.config_version if current else None})"
        )
    updated = current.model_copy(
        update={
            "config_version": expected_config_version + 1,
            "pause_buy_sell": pause_buy_sell,
            "updated_at": now or dt.datetime.now(dt.UTC),
            "updated_by": updated_by,
            "change_reason": change_reason,
        }
    )
    store.upsert(updated)
    return updated


def _update_dynamodb(
    expected_config_version: int,
    pause_buy_sell: bool,
    updated_by: str,
    change_reason: str,
    now: dt.datetime | None,
) -> TradingPauseConfig:
    import boto3
    from botocore.exceptions import ClientError

    table: Any = boto3.resource("dynamodb").Table(resolve_table_name(_TABLE_FILE_NAME))
    now_value = now or dt.datetime.now(dt.UTC)
    try:
        response = table.update_item(
            Key={"config_id": _CONFIG_ID},
            ConditionExpression="config_version = :expected_version",
            UpdateExpression=(
                "SET pause_buy_sell = :pause, "
                "config_version = config_version + :one, "
                "updated_at = :now, updated_by = :who, change_reason = :reason"
            ),
            ExpressionAttributeValues={
                ":expected_version": expected_config_version,
                ":pause": pause_buy_sell,
                ":one": 1,
                ":now": now_value.isoformat(),
                ":who": updated_by,
                ":reason": change_reason,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise TradingPauseConflictError(
                f"config_versionが期待値(expected={expected_config_version})と一致しません(競合)"
            ) from e
        raise
    return _from_dynamo_item(response["Attributes"])
