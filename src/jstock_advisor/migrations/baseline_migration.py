"""InvestmentThesisBaselineSequence/Pointerのholding_id移行(M2)。

- Sequence(_BaselineSequenceCounter相当): DynamoDB版はトップレベル属性のみ
  (dataブロブを使わない、ADD式の原子加算のため)で保存されている。本モジュールは
  移行専用に読み書きの生boto3処理を持ち、production側の
  infrastructure/aws/baseline_sequence.pyの実装には依存しない(旧テーブル・
  新テーブルの物理名をこのモジュール内で明示的な定数として個別に持つ)。
  current_versionは移行の前後で一切変更しない(リセットしない、ADDで
  増やさない。読み取った値をそのままコピーする)。

- Pointer(InvestmentThesisBaselinePointer): 本番検証で発見・修正済み
  (baseline_pointer.py)により、create/get/updateのすべてが標準の
  CollectionStore形式(PK + dataブロブ)へ統一されているため、本モジュールは
  標準のbuild_collection_store()経由でそのまま読み書きできる
  (生boto3処理は不要)。

いずれも、旧テーブル・新テーブルの参照はこのモジュール内で明示的に完結し、
production側の`_TABLE_FILE_NAME`定数(M3のコード切替で新テーブルへ差し替え
られる、単一の値)には一切依存しない設計とする(旧を読み新へ書く、という
移行スクリプト自身の要件を、production側の値と独立に保証するため)。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from jstock_advisor.domain.entities.holding_decision import InvestmentThesisBaselinePointer
from jstock_advisor.domain.entities.owner import build_holding_id, normalize_and_validate_owner
from jstock_advisor.infrastructure.collection_store import (
    build_collection_store,
    resolve_table_name,
)
from jstock_advisor.migrations.legacy_shapes import LegacyBaselineSequenceCounterV1
from jstock_advisor.migrations.target import MigrationTarget

_SEQUENCE_LEGACY_FILE_NAME = "investment_thesis_baseline_sequences.json"
_SEQUENCE_V2_FILE_NAME = "investment_thesis_baseline_sequences_v2.json"
_POINTER_LEGACY_FILE_NAME = "investment_thesis_baseline_pointers.json"
_POINTER_V2_FILE_NAME = "investment_thesis_baseline_pointers_v2.json"


# --- Sequence(トップレベル属性のみ、生boto3) --------------------------------


def read_all_legacy_sequences(
    target: MigrationTarget, store_dir: Path | None
) -> list[LegacyBaselineSequenceCounterV1]:
    if target is MigrationTarget.LOCAL:
        store = build_collection_store(
            LegacyBaselineSequenceCounterV1, _SEQUENCE_LEGACY_FILE_NAME, "holding_id", store_dir
        )
        return store.list_all()
    return _scan_sequence_table_aws(_SEQUENCE_LEGACY_FILE_NAME)


def _scan_sequence_table_aws(file_name: str) -> list[LegacyBaselineSequenceCounterV1]:
    import boto3

    table: Any = boto3.resource("dynamodb").Table(resolve_table_name(file_name))
    items: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {}
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return [
        LegacyBaselineSequenceCounterV1(
            holding_id=str(item["holding_id"]),
            current_version=int(item["current_version"]),
            updated_at=dt.datetime.fromisoformat(str(item["updated_at"])),
        )
        for item in items
    ]


def write_sequence_v2(
    entry: LegacyBaselineSequenceCounterV1,
    new_holding_id: str,
    target: MigrationTarget,
    store_dir: Path | None,
) -> None:
    """current_versionをそのままコピーしたV2レコードを書き込む(ADDで増やさない)。"""
    updated = entry.model_copy(update={"holding_id": new_holding_id})
    if target is MigrationTarget.LOCAL:
        store = build_collection_store(
            LegacyBaselineSequenceCounterV1, _SEQUENCE_V2_FILE_NAME, "holding_id", store_dir
        )
        store.upsert(updated)
        return
    import boto3

    table: Any = boto3.resource("dynamodb").Table(resolve_table_name(_SEQUENCE_V2_FILE_NAME))
    table.put_item(
        Item={
            "holding_id": updated.holding_id,
            "current_version": updated.current_version,
            "updated_at": updated.updated_at.isoformat(),
        }
    )


def get_sequence_v2(
    holding_id: str, target: MigrationTarget, store_dir: Path | None
) -> LegacyBaselineSequenceCounterV1 | None:
    if target is MigrationTarget.LOCAL:
        store = build_collection_store(
            LegacyBaselineSequenceCounterV1, _SEQUENCE_V2_FILE_NAME, "holding_id", store_dir
        )
        return store.get(holding_id)
    import boto3

    table: Any = boto3.resource("dynamodb").Table(resolve_table_name(_SEQUENCE_V2_FILE_NAME))
    response = table.get_item(Key={"holding_id": holding_id})
    item = response.get("Item")
    if item is None:
        return None
    return LegacyBaselineSequenceCounterV1(
        holding_id=str(item["holding_id"]),
        current_version=int(item["current_version"]),
        updated_at=dt.datetime.fromisoformat(str(item["updated_at"])),
    )


# --- Pointer(標準CollectionStore、dataブロブ) -------------------------------


def read_all_legacy_pointers(
    target: MigrationTarget, store_dir: Path | None
) -> list[InvestmentThesisBaselinePointer]:
    store = build_collection_store(
        InvestmentThesisBaselinePointer, _POINTER_LEGACY_FILE_NAME, "holding_id", store_dir
    )
    return store.list_all()


def write_pointer_v2(
    pointer: InvestmentThesisBaselinePointer,
    new_holding_id: str,
    store_dir: Path | None,
) -> None:
    """active_baseline_id/active_baseline_version/pointer_version/updated_at/
    updated_byをすべてそのままコピーしたV2レコードを書き込む
    (pointer_versionを勝手に増やさない)。
    """
    updated = pointer.model_copy(update={"holding_id": new_holding_id})
    store = build_collection_store(
        InvestmentThesisBaselinePointer, _POINTER_V2_FILE_NAME, "holding_id", store_dir
    )
    store.upsert(updated)


def get_pointer_v2(
    holding_id: str, store_dir: Path | None
) -> InvestmentThesisBaselinePointer | None:
    store = build_collection_store(
        InvestmentThesisBaselinePointer, _POINTER_V2_FILE_NAME, "holding_id", store_dir
    )
    return store.get(holding_id)


def migrated_holding_id_for_stock_code(stock_code: str, owner: str) -> str:
    """旧holding_id(stock_codeの1:1エイリアス)から新holding_idを導出する。"""
    normalized_owner = normalize_and_validate_owner(owner)
    return build_holding_id(normalized_owner, stock_code)
