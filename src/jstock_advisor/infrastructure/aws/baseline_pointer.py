"""InvestmentThesisBaselinePointerの楽観ロック更新(実装プラン2節)。

「現在有効なbaseline」を指す唯一の情報源。baseline本体は書き換えず、この
ポインタ1行(holding_idごと)だけを条件付き更新することで、新規ACTIVE化+
旧SUPERSEDED化という2操作の部分失敗による「ポインタ0件/複数件」を構造的に
排除する。1節のHoldingDecisionRuntimeConfig更新と同じ楽観ロック技法を使う。

CollectionStoreの汎用upsert()は無条件書き込みであり条件付き更新を表現できない
ため、DynamoDB環境の更新(update_pointer)はこのモジュールが直接boto3を使う。
初回作成(create_pointer)はCollectionStore.insert_if_absent()で足りる
(既にDynamoDB/ローカル双方で原子的に実装済み)。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from jstock_advisor.domain.entities.holding_decision import InvestmentThesisBaselinePointer
from jstock_advisor.infrastructure.collection_store import (
    CollectionStore,
    build_collection_store,
    resolve_table_name,
    running_on_lambda,
)

_TABLE_FILE_NAME = "investment_thesis_baseline_pointers.json"


class BaselinePointerConflictError(Exception):
    """他の更新が先に行われたためポインタ更新に失敗した(競合)。"""


def _store(store_dir: Path | None = None) -> CollectionStore[InvestmentThesisBaselinePointer]:
    return build_collection_store(
        InvestmentThesisBaselinePointer, _TABLE_FILE_NAME, "holding_id", store_dir
    )


def get_pointer(
    holding_id: str, store_dir: Path | None = None
) -> InvestmentThesisBaselinePointer | None:
    return _store(store_dir).get(holding_id)


def create_pointer(
    holding_id: str,
    baseline_id: str,
    baseline_version: int,
    updated_by: str | None = None,
    now: dt.datetime | None = None,
    store_dir: Path | None = None,
) -> InvestmentThesisBaselinePointer | None:
    """最初のbaseline活性化時にポインタを新規作成する。

    既にポインタが存在する場合はNoneを返す(insert_if_absent()の性質上、
    既存値は変更しない。呼び出し側はget_pointer()で既存値を取得し直すこと)。
    """
    pointer = InvestmentThesisBaselinePointer(
        holding_id=holding_id,
        active_baseline_id=baseline_id,
        active_baseline_version=baseline_version,
        pointer_version=1,
        updated_at=now or dt.datetime.now(dt.UTC),
        updated_by=updated_by,
    )
    created = _store(store_dir).insert_if_absent(pointer)
    return pointer if created else None


def update_pointer(
    holding_id: str,
    new_baseline_id: str,
    new_baseline_version: int,
    expected_pointer_version: int,
    updated_by: str | None = None,
    now: dt.datetime | None = None,
    store_dir: Path | None = None,
) -> InvestmentThesisBaselinePointer:
    """既存ポインタを条件付きで新しいbaselineへ差し替える。

    expected_pointer_versionが現在値と一致する場合のみ更新し、pointer_versionを
    +1する。一致しない場合はBaselinePointerConflictErrorを送出する(自動リトライは
    呼び出し側の責務、2節の活性化リトライ方針)。
    """
    if running_on_lambda():
        return _update_pointer_dynamodb(
            holding_id,
            new_baseline_id,
            new_baseline_version,
            expected_pointer_version,
            updated_by,
            now,
        )
    return _update_pointer_local(
        holding_id,
        new_baseline_id,
        new_baseline_version,
        expected_pointer_version,
        updated_by,
        now,
        store_dir,
    )


def _update_pointer_local(
    holding_id: str,
    new_baseline_id: str,
    new_baseline_version: int,
    expected_pointer_version: int,
    updated_by: str | None,
    now: dt.datetime | None,
    store_dir: Path | None,
) -> InvestmentThesisBaselinePointer:
    store = _store(store_dir)
    current = store.get(holding_id)
    if current is None or current.pointer_version != expected_pointer_version:
        raise BaselinePointerConflictError(
            f"holding_id={holding_id}: ポインタが期待したバージョン"
            f"(expected={expected_pointer_version})と一致しません"
            f"(現在={current.pointer_version if current else None})"
        )
    updated = current.model_copy(
        update={
            "active_baseline_id": new_baseline_id,
            "active_baseline_version": new_baseline_version,
            "pointer_version": expected_pointer_version + 1,
            "updated_at": now or dt.datetime.now(dt.UTC),
            "updated_by": updated_by,
        }
    )
    store.upsert(updated)
    return updated


def _update_pointer_dynamodb(
    holding_id: str,
    new_baseline_id: str,
    new_baseline_version: int,
    expected_pointer_version: int,
    updated_by: str | None,
    now: dt.datetime | None,
) -> InvestmentThesisBaselinePointer:
    import boto3
    from botocore.exceptions import ClientError

    table: Any = boto3.resource("dynamodb").Table(resolve_table_name(_TABLE_FILE_NAME))
    now_value = now or dt.datetime.now(dt.UTC)
    try:
        response = table.update_item(
            Key={"holding_id": holding_id},
            ConditionExpression="pointer_version = :expected_version",
            UpdateExpression=(
                "SET active_baseline_id = :baseline_id, "
                "active_baseline_version = :baseline_version, "
                "pointer_version = pointer_version + :one, "
                "updated_at = :now, updated_by = :who"
            ),
            ExpressionAttributeValues={
                ":expected_version": expected_pointer_version,
                ":baseline_id": new_baseline_id,
                ":baseline_version": new_baseline_version,
                ":one": 1,
                ":now": now_value.isoformat(),
                ":who": updated_by,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise BaselinePointerConflictError(
                f"holding_id={holding_id}: ポインタが期待したバージョン"
                f"(expected={expected_pointer_version})と一致しません(競合)"
            ) from e
        raise
    return InvestmentThesisBaselinePointer(
        holding_id=holding_id,
        active_baseline_id=response["Attributes"]["active_baseline_id"],
        active_baseline_version=int(response["Attributes"]["active_baseline_version"]),
        pointer_version=int(response["Attributes"]["pointer_version"]),
        updated_at=now_value,
        updated_by=updated_by,
    )
