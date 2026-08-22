"""InvestmentThesisBaseline.versionの原子的な採番(実装プラン2節)。

batch_tracker.pyと同じくDynamoDBのUpdateItem(ADD式)による原子カウンタを使う。
CollectionStoreの汎用インターフェース(upsert/get)ではADD式による原子加算を
表現できないため、DynamoDB環境ではこのモジュールが直接boto3を使う。ローカル
(非Lambda)環境は単一プロセスのCLI/バッチ実行が前提のため、build_collection_store
経由の単純な読み取り→+1で代用する(真の原子性は不要という既存方針を踏襲)。

採番後にbaseline本体の保存が失敗しても欠番を許容する(連番の連続性よりも
重複防止を優先する)。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.infrastructure.collection_store import (
    build_collection_store,
    resolve_table_name,
    running_on_lambda,
)

_TABLE_FILE_NAME = "investment_thesis_baseline_sequences_v2.json"
# M3: V2切替。保存形式(トップレベル属性)は変更なし。


class _BaselineSequenceCounter(Entity):
    holding_id: str
    current_version: int
    updated_at: dt.datetime


def allocate_next_baseline_version(holding_id: str, store_dir: Path | None = None) -> int:
    """holding_idごとに1から始まる連番を原子的に採番する(重複しないことのみ保証)。"""
    if running_on_lambda():
        return _allocate_next_baseline_version_dynamodb(holding_id)
    return _allocate_next_baseline_version_local(holding_id, store_dir)


def _allocate_next_baseline_version_local(holding_id: str, store_dir: Path | None) -> int:
    store = build_collection_store(
        _BaselineSequenceCounter, _TABLE_FILE_NAME, "holding_id", store_dir
    )
    current = store.get(holding_id)
    next_version = (current.current_version if current is not None else 0) + 1
    store.upsert(
        _BaselineSequenceCounter(
            holding_id=holding_id,
            current_version=next_version,
            updated_at=dt.datetime.now(dt.UTC),
        )
    )
    return next_version


def _allocate_next_baseline_version_dynamodb(holding_id: str) -> int:
    import boto3

    table: Any = boto3.resource("dynamodb").Table(resolve_table_name(_TABLE_FILE_NAME))
    response = table.update_item(
        Key={"holding_id": holding_id},
        UpdateExpression="ADD current_version :inc SET updated_at = :now",
        ExpressionAttributeValues={
            ":inc": 1,
            ":now": dt.datetime.now(dt.UTC).isoformat(),
        },
        ReturnValues="UPDATED_NEW",
    )
    return int(response["Attributes"]["current_version"])
