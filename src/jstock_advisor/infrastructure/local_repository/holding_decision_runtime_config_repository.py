"""HoldingDecisionRuntimeConfigの永続化(実装プラン1節)。

mode/notification_enabled/financial_policy_overrideという再デプロイ不要で
切り替えたい運用パラメータを専用テーブルへ保存する。初回作成はPutItem+
attribute_not_exists、通常更新は条件付きUpdateItem(config_versionによる
楽観ロック)で行う。CollectionStoreの汎用upsert()は無条件書き込みであり
条件付き更新を表現できないため、DynamoDB環境の更新はこのモジュールが
直接boto3を使う(baseline_pointer.pyと同じ設計)。

DynamoDB上のアイテムはinsert_if_absent()(CollectionStore経由)が書き込む
PK + data(JSON文字列)スキーマのため、条件式・更新式はconfig_version等の
トップレベルのネイティブ属性ではなく、data属性全体の完全一致を条件とする
CASで表現する(2026-08修正、watchlist_rotation_state.pyと同じ不具合パターン
・同じ修正方針)。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from jstock_advisor.domain.entities.enums import FinancialPolicyOverride, RuntimeConfigMode
from jstock_advisor.domain.entities.holding_decision import HoldingDecisionRuntimeConfig
from jstock_advisor.infrastructure.collection_store import (
    CollectionStore,
    build_collection_store,
    resolve_table_name,
    running_on_lambda,
)

_TABLE_FILE_NAME = "holding_decision_runtime_config.json"
_CONFIG_ID = "holding_decision"


class RuntimeConfigConflictError(Exception):
    """他の更新が先に行われたため、または初回作成が既に行われているため失敗した。"""


def _store(store_dir: Path | None = None) -> CollectionStore[HoldingDecisionRuntimeConfig]:
    return build_collection_store(
        HoldingDecisionRuntimeConfig, _TABLE_FILE_NAME, "config_id", store_dir
    )


def get(store_dir: Path | None = None) -> HoldingDecisionRuntimeConfig | None:
    return _store(store_dir).get(_CONFIG_ID)


def init(
    mode: RuntimeConfigMode,
    notification_enabled: bool,
    financial_policy_override: FinancialPolicyOverride,
    updated_by: str,
    change_reason: str,
    now: dt.datetime | None = None,
    store_dir: Path | None = None,
) -> HoldingDecisionRuntimeConfig | None:
    """初回作成。既に存在する場合はNoneを返す(既存値は変更しない)。"""
    config = HoldingDecisionRuntimeConfig(
        config_id=_CONFIG_ID,
        config_version=1,
        mode=mode,
        notification_enabled=notification_enabled,
        financial_policy_override=financial_policy_override,
        updated_at=now or dt.datetime.now(dt.UTC),
        updated_by=updated_by,
        change_reason=change_reason,
    )
    created = _store(store_dir).insert_if_absent(config)
    return config if created else None


def update(
    expected_config_version: int,
    mode: RuntimeConfigMode,
    notification_enabled: bool,
    financial_policy_override: FinancialPolicyOverride,
    updated_by: str,
    change_reason: str,
    now: dt.datetime | None = None,
    store_dir: Path | None = None,
) -> HoldingDecisionRuntimeConfig:
    """expected_config_versionが現在値と一致する場合のみ更新する(楽観ロック)。

    一致しない場合はRuntimeConfigConflictErrorを送出する(自動リトライしない、
    呼び出し側=CLIが最新値を再取得して人間へ再確認を促す)。
    """
    if running_on_lambda():
        return _update_dynamodb(
            expected_config_version,
            mode,
            notification_enabled,
            financial_policy_override,
            updated_by,
            change_reason,
            now,
        )
    return _update_local(
        expected_config_version,
        mode,
        notification_enabled,
        financial_policy_override,
        updated_by,
        change_reason,
        now,
        store_dir,
    )


def _update_local(
    expected_config_version: int,
    mode: RuntimeConfigMode,
    notification_enabled: bool,
    financial_policy_override: FinancialPolicyOverride,
    updated_by: str,
    change_reason: str,
    now: dt.datetime | None,
    store_dir: Path | None,
) -> HoldingDecisionRuntimeConfig:
    store = _store(store_dir)
    current = store.get(_CONFIG_ID)
    if current is None or current.config_version != expected_config_version:
        raise RuntimeConfigConflictError(
            f"config_versionが期待値(expected={expected_config_version})と一致しません"
            f"(現在={current.config_version if current else None})"
        )
    updated = current.model_copy(
        update={
            "config_version": expected_config_version + 1,
            "mode": mode,
            "notification_enabled": notification_enabled,
            "financial_policy_override": financial_policy_override,
            "updated_at": now or dt.datetime.now(dt.UTC),
            "updated_by": updated_by,
            "change_reason": change_reason,
        }
    )
    store.upsert(updated)
    return updated


def _update_dynamodb(
    expected_config_version: int,
    mode: RuntimeConfigMode,
    notification_enabled: bool,
    financial_policy_override: FinancialPolicyOverride,
    updated_by: str,
    change_reason: str,
    now: dt.datetime | None,
) -> HoldingDecisionRuntimeConfig:
    """2026-08修正(本番検証で発覚): init()が書き込むアイテムはinsert_if_absent()
    経由でPK + data(JSON文字列)スキーマ(dynamodb_store.py:to_dynamo_item())で
    保存され、トップレベルにconfig_version等のネイティブ属性は存在しない。この
    ため以前の実装はConditionExpressionが常にConditionalCheckFailedException
    となり、update()が一度も成功していなかった(watchlist_rotation_state.pyの
    rotation commitと同じ不具合パターン)。data属性全体の完全一致を条件とする
    CASへ修正し、既存本番データをmigrationなしでそのまま利用できるようにする。
    """
    import boto3
    from botocore.exceptions import ClientError

    table: Any = boto3.resource("dynamodb").Table(resolve_table_name(_TABLE_FILE_NAME))
    now_value = now or dt.datetime.now(dt.UTC)

    response = table.get_item(Key={"config_id": _CONFIG_ID}, ConsistentRead=True)
    item = response.get("Item")
    if item is None:
        raise RuntimeConfigConflictError(
            f"config_versionが期待値(expected={expected_config_version})と一致しません"
            "(現在=None、未初期化)"
        )
    current_data = item["data"]
    current = HoldingDecisionRuntimeConfig.model_validate_json(current_data)
    if current.config_version != expected_config_version:
        raise RuntimeConfigConflictError(
            f"config_versionが期待値(expected={expected_config_version})と一致しません"
            f"(現在={current.config_version})"
        )

    updated = current.model_copy(
        update={
            "config_version": expected_config_version + 1,
            "mode": mode,
            "notification_enabled": notification_enabled,
            "financial_policy_override": financial_policy_override,
            "updated_at": now_value,
            "updated_by": updated_by,
            "change_reason": change_reason,
        }
    )

    try:
        table.update_item(
            Key={"config_id": _CONFIG_ID},
            ConditionExpression="#data = :expected_data",
            UpdateExpression="SET #data = :new_data",
            ExpressionAttributeNames={"#data": "data"},
            ExpressionAttributeValues={
                ":expected_data": current_data,
                ":new_data": updated.model_dump_json(),
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise RuntimeConfigConflictError(
                f"config_versionが期待値(expected={expected_config_version})と一致しません(競合)"
            ) from e
        raise
    return updated
