"""HoldingDecisionRuntimeConfigの永続化(実装プラン1節)。

mode/notification_enabled/financial_policy_overrideという再デプロイ不要で
切り替えたい運用パラメータを専用テーブルへ保存する。初回作成はPutItem+
attribute_not_exists、通常更新は条件付きUpdateItem(config_versionによる
楽観ロック)で行う。CollectionStoreの汎用upsert()は無条件書き込みであり
条件付き更新を表現できないため、DynamoDB環境の更新はこのモジュールが
直接boto3を使う(baseline_pointer.pyと同じ設計)。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

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
    import boto3
    from botocore.exceptions import ClientError

    table = boto3.resource("dynamodb").Table(resolve_table_name(_TABLE_FILE_NAME))
    now_value = now or dt.datetime.now(dt.UTC)
    try:
        response = table.update_item(
            Key={"config_id": _CONFIG_ID},
            ConditionExpression="config_version = :expected_version",
            UpdateExpression=(
                "SET #mode = :new_mode, notification_enabled = :notif, "
                "financial_policy_override = :policy_override, "
                "config_version = config_version + :one, "
                "updated_at = :now, updated_by = :who, change_reason = :reason"
            ),
            ExpressionAttributeNames={"#mode": "mode"},
            ExpressionAttributeValues={
                ":expected_version": expected_config_version,
                ":new_mode": mode.value,
                ":notif": notification_enabled,
                ":policy_override": financial_policy_override.value,
                ":one": 1,
                ":now": now_value.isoformat(),
                ":who": updated_by,
                ":reason": change_reason,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise RuntimeConfigConflictError(
                f"config_versionが期待値(expected={expected_config_version})と一致しません(競合)"
            ) from e
        raise
    attrs = response["Attributes"]
    return HoldingDecisionRuntimeConfig(
        config_id=_CONFIG_ID,
        config_version=int(attrs["config_version"]),
        mode=RuntimeConfigMode(attrs["mode"]),
        notification_enabled=bool(attrs["notification_enabled"]),
        financial_policy_override=FinancialPolicyOverride(attrs["financial_policy_override"]),
        updated_at=now_value,
        updated_by=updated_by,
        change_reason=change_reason,
    )
