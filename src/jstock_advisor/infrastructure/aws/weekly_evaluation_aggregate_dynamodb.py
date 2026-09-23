"""週次評価集計(WeeklyEvaluationAggregate)の DynamoDB 実装(Issue #537)。

契約と設計の背景は `infrastructure/weekly_evaluation_aggregate_store.py` を参照。

## テーブル(WeeklyEvaluationAggregateTable。HASH = review_week / RANGE = item_key)

```
集計行   review_week = "2026-W38"   item_key = "AGG#{recommendation_type}#{rule_version}"
         sample_count / conclusive_count / success_count / price_return_sum / price_return_count /
         excess_return_sum / excess_return_count / lc_{LABEL}(ラベル別件数。トップレベルの数値属性)/
         recommendation_type / rule_version / updated_at / schema_version
週の状態  review_week = "2026-W38"   item_key = "#STATE"
         mark_seq(評価を反映するたびに +1)/ recomputed_seq / rebuild_required / rebuild_reason
発見用    review_week = "#INDEX"      item_key = "PENDING" | "REBUILD"     weeks(String Set)
制御用    review_week = "#CONTROL"    item_key = "BACKFILL"                status / completed_at /
  week_count / row_count
```

★ 1 週の全集計行は Query 1 回(review_week = ?)で取れる。**Aggregate 全件の Scan は行わない。**
★ ラベル別件数をマップでなくトップレベルの属性(`lc_*`)にするのは、マップの要素への ADD が、
  マップが無い初回に失敗するため(ADD はトップレベルの数値属性を暗黙に作る)。
★ EvaluationResult の item は `{evaluation_id, data(JSON)}`。
  通常のリポジトリ(DynamoDbCollectionStore)
  と同じ形式で Transaction の Put へ入れる(`to_dynamo_item`)。
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.weekly_evaluation_aggregate import (
    AGGREGATE_SCHEMA_VERSION,
    WeeklyEvaluationAggregate,
    aggregate_item_key,
    delta_of,
)
from jstock_advisor.infrastructure.aws.dynamodb_store import to_dynamo_item
from jstock_advisor.infrastructure.aws.dynamodb_transaction import (
    serialize,
    transact_write_items_with_conflict_retry,
)
from jstock_advisor.infrastructure.collection_store import resolve_table_name
from jstock_advisor.infrastructure.weekly_evaluation_aggregate_store import (
    REBUILD_REASONS,
    TABLE_ENV,
    BackfillStatus,
    WeekState,
)

_HASH = "review_week"
_RANGE = "item_key"
_STATE_KEY = "#STATE"
_INDEX_WEEK = "#INDEX"
_PENDING_KEY = "PENDING"
_REBUILD_KEY = "REBUILD"
_CONTROL_WEEK = "#CONTROL"
_BACKFILL_KEY = "BACKFILL"
_AGG_PREFIX = "AGG#"
_LABEL_ATTR_PREFIX = "lc_"
_EVALUATION_TABLE_FILE = "evaluation_results.json"
_MAX_TRANSACT_ITEMS = 100

_deserializer = TypeDeserializer()


def _key(review_week: str, item_key: str) -> dict[str, Any]:
    return {_HASH: serialize(review_week), _RANGE: serialize(item_key)}


def _iso(now: dt.datetime) -> str:
    return now.isoformat()


def _is_first_item_conditional_failure(error: ClientError) -> bool:
    """Transaction の取消理由が「先頭項目(EvaluationResult の Put)の条件不成立」だけか。"""
    if error.response["Error"]["Code"] != "TransactionCanceledException":
        return False
    reasons = error.response.get("CancellationReasons") or []
    if not reasons or reasons[0].get("Code") != "ConditionalCheckFailed":
        return False
    return all(reason.get("Code") in (None, "None") for reason in reasons[1:])


def _is_conditional_failure(error: ClientError) -> bool:
    if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
        return True
    if error.response["Error"]["Code"] != "TransactionCanceledException":
        return False
    reasons = error.response.get("CancellationReasons") or []
    return any(reason.get("Code") == "ConditionalCheckFailed" for reason in reasons)


class DynamoWeeklyEvaluationAggregateStore:
    def __init__(self, aggregate_table: str, evaluation_table: str, client: Any) -> None:
        self._aggregate_table = aggregate_table
        self._evaluation_table = evaluation_table
        self._client = client

    @classmethod
    def from_environment(cls) -> DynamoWeeklyEvaluationAggregateStore:
        table = os.environ.get(TABLE_ENV)
        if not table:
            raise RuntimeError(f"{TABLE_ENV}環境変数が設定されていません")
        return cls(table, resolve_table_name(_EVALUATION_TABLE_FILE), boto3.client("dynamodb"))

    # --- 書き込み -------------------------------------------------------

    def commit_evaluation(
        self,
        evaluation: EvaluationResult,
        recommendation_type: RecommendationType,
        rule_version: str,
        now: dt.datetime,
    ) -> bool:
        delta = delta_of(evaluation)  # 非有限値はここで例外(Transaction を送らない = 保存しない)
        label_attr = f"{_LABEL_ATTR_PREFIX}{delta.label}"
        item = to_dynamo_item(evaluation, "evaluation_id")
        transact_items: list[dict[str, Any]] = [
            {
                # 1: claim。決定的な evaluation_id(#325)が既に有れば、Transaction
                # 全体が不成立になる。
                "Put": {
                    "TableName": self._evaluation_table,
                    "Item": {k: serialize(v) for k, v in item.items()},
                    "ConditionExpression": "attribute_not_exists(#pk)",
                    "ExpressionAttributeNames": {"#pk": "evaluation_id"},
                }
            },
            {
                # 2: 集計の加算(ADD はトップレベルの数値属性を暗黙に作る)
                "Update": {
                    "TableName": self._aggregate_table,
                    "Key": _key(
                        delta.review_week, aggregate_item_key(recommendation_type, rule_version)
                    ),
                    "UpdateExpression": (
                        "ADD sample_count :one, conclusive_count :cc, success_count :sc, "
                        "price_return_sum :pr, price_return_count :one, "
                        "excess_return_sum :er, excess_return_count :ec, #lc :one "
                        "SET updated_at = :now, recommendation_type = :rt, "
                        "rule_version = :rv, schema_version = :sv"
                    ),
                    "ExpressionAttributeNames": {"#lc": label_attr},
                    "ExpressionAttributeValues": {
                        ":one": serialize(1),
                        ":cc": serialize(delta.conclusive),
                        ":sc": serialize(delta.success),
                        ":pr": serialize(delta.price_return),
                        ":er": serialize(delta.excess_return),
                        ":ec": serialize(delta.excess_count),
                        ":now": serialize(_iso(now)),
                        ":rt": serialize(recommendation_type.value),
                        ":rv": serialize(rule_version),
                        ":sv": serialize(AGGREGATE_SCHEMA_VERSION),
                    },
                }
            },
            {
                # 3: 週の状態。mark_seq を進める = REVIEW_RECOMPUTE_PENDING(Metrics の再生成が要る)
                "Update": {
                    "TableName": self._aggregate_table,
                    "Key": _key(delta.review_week, _STATE_KEY),
                    "UpdateExpression": "ADD mark_seq :one SET last_marked_at = :now",
                    "ExpressionAttributeValues": {
                        ":one": serialize(1),
                        ":now": serialize(_iso(now)),
                    },
                }
            },
            {
                # 4: 再計算対象の週の一覧(Aggregate 全件を探索せずに、対象の週を特定するため)
                "Update": {
                    "TableName": self._aggregate_table,
                    "Key": _key(_INDEX_WEEK, _PENDING_KEY),
                    "UpdateExpression": "ADD weeks :w",
                    "ExpressionAttributeValues": {":w": {"SS": [delta.review_week]}},
                }
            },
        ]
        try:
            transact_write_items_with_conflict_retry(self._client, transact_items)
        except ClientError as e:
            if _is_first_item_conditional_failure(e):
                return False  # 既に保存済み。加算も marker も起きていない
            raise
        return True

    def finish_recompute(self, review_week: str, seen_mark_seq: int) -> bool:
        items = [
            {
                "Update": {
                    "TableName": self._aggregate_table,
                    "Key": _key(review_week, _STATE_KEY),
                    "UpdateExpression": "SET recomputed_seq = :seen",
                    "ConditionExpression": "mark_seq = :seen",
                    "ExpressionAttributeValues": {":seen": serialize(seen_mark_seq)},
                }
            },
            {
                "Update": {
                    "TableName": self._aggregate_table,
                    "Key": _key(_INDEX_WEEK, _PENDING_KEY),
                    "UpdateExpression": "DELETE weeks :w",
                    "ExpressionAttributeValues": {":w": {"SS": [review_week]}},
                }
            },
        ]
        try:
            transact_write_items_with_conflict_retry(self._client, items)
        except ClientError as e:
            if _is_conditional_failure(e):
                return False  # 読んだ後に新しい評価が届いた(mark_seq が進んだ)。marker を残す
            raise
        return True

    def mark_rebuild_required(self, review_week: str, reason: str, now: dt.datetime) -> None:
        if reason not in REBUILD_REASONS:
            raise ValueError(f"未知の rebuild 理由です: {reason}")
        items = [
            {
                "Update": {
                    "TableName": self._aggregate_table,
                    "Key": _key(review_week, _STATE_KEY),
                    "UpdateExpression": (
                        "SET rebuild_required = :t, rebuild_reason = :r, rebuild_marked_at = :now"
                    ),
                    "ExpressionAttributeValues": {
                        ":t": serialize(True),
                        ":r": serialize(reason),
                        ":now": serialize(_iso(now)),
                    },
                }
            },
            {
                "Update": {
                    "TableName": self._aggregate_table,
                    "Key": _key(_INDEX_WEEK, _REBUILD_KEY),
                    "UpdateExpression": "ADD weeks :w",
                    "ExpressionAttributeValues": {":w": {"SS": [review_week]}},
                }
            },
        ]
        transact_write_items_with_conflict_retry(self._client, items)

    def replace_week(
        self,
        review_week: str,
        rows: Iterable[WeeklyEvaluationAggregate],
        expected_mark_seq: int | None,
        now: dt.datetime,
        *,
        request_recompute: bool = True,
    ) -> bool:
        new_rows = list(rows)
        for row in new_rows:
            if row.review_week != review_week:
                raise ValueError("置き換える行の週が一致しません")
        new_keys = {row.item_key for row in new_rows}
        stale_keys = [
            r.item_key for r in self.query_week(review_week) if r.item_key not in new_keys
        ]
        state_values: dict[str, Any] = {
            ":f": serialize(False),
            ":now": serialize(_iso(now)),
        }
        if request_recompute:
            state_values[":one"] = serialize(1)
        if expected_mark_seq is None:
            condition = "attribute_not_exists(mark_seq)"
        else:
            condition = "mark_seq = :exp"
            state_values[":exp"] = serialize(expected_mark_seq)
        items: list[dict[str, Any]] = [
            {
                # 状態の更新(mark_seq を進める = Metrics の再生成を要求 / rebuild_required を解除)。
                # 作り直しの元にした raw を読んだ後に新しい評価が届いていれば、条件が不成立になる。
                "Update": {
                    "TableName": self._aggregate_table,
                    "Key": _key(review_week, _STATE_KEY),
                    "UpdateExpression": (
                        ("ADD mark_seq :one " if request_recompute else "")
                        + "SET rebuild_required = :f, last_rebuilt_at = :now REMOVE rebuild_reason"
                    ),
                    "ConditionExpression": condition,
                    "ExpressionAttributeValues": state_values,
                }
            },
            {
                "Update": {
                    "TableName": self._aggregate_table,
                    "Key": _key(_INDEX_WEEK, _REBUILD_KEY),
                    "UpdateExpression": "DELETE weeks :w",
                    "ExpressionAttributeValues": {":w": {"SS": [review_week]}},
                }
            },
        ]
        if request_recompute:
            items.append(
                {
                    "Update": {
                        "TableName": self._aggregate_table,
                        "Key": _key(_INDEX_WEEK, _PENDING_KEY),
                        "UpdateExpression": "ADD weeks :w",
                        "ExpressionAttributeValues": {":w": {"SS": [review_week]}},
                    }
                }
            )
        for row in new_rows:
            items.append(
                {
                    "Put": {
                        "TableName": self._aggregate_table,
                        "Item": self._row_item(row),
                    }
                }
            )
        for item_key in stale_keys:
            items.append(
                {"Delete": {"TableName": self._aggregate_table, "Key": _key(review_week, item_key)}}
            )
        if len(items) > _MAX_TRANSACT_ITEMS:
            raise ValueError(
                f"1 週の集計行が多すぎて 1 つの Transaction に収まりません(rows={len(new_rows)})"
            )
        try:
            transact_write_items_with_conflict_retry(self._client, items)
        except ClientError as e:
            if _is_conditional_failure(e):
                return False
            raise
        return True

    def set_backfill_complete(self, now: dt.datetime, week_count: int, row_count: int) -> None:
        self._client.update_item(
            TableName=self._aggregate_table,
            Key=_key(_CONTROL_WEEK, _BACKFILL_KEY),
            UpdateExpression=("SET #s = :c, completed_at = :now, week_count = :w, row_count = :r"),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":c": serialize("COMPLETE"),
                ":now": serialize(_iso(now)),
                ":w": serialize(week_count),
                ":r": serialize(row_count),
            },
        )

    # --- 読み取り(Query / GetItem のみ。Scan は行わない) ------------------

    def query_week(self, review_week: str) -> list[WeeklyEvaluationAggregate]:
        rows: list[WeeklyEvaluationAggregate] = []
        kwargs: dict[str, Any] = {
            "TableName": self._aggregate_table,
            "KeyConditionExpression": "#h = :w AND begins_with(#r, :p)",
            "ExpressionAttributeNames": {"#h": _HASH, "#r": _RANGE},
            "ExpressionAttributeValues": {
                ":w": serialize(review_week),
                ":p": serialize(_AGG_PREFIX),
            },
            "ConsistentRead": True,
        }
        while True:
            response = self._client.query(**kwargs)
            for raw in response.get("Items", []):
                rows.append(self._decode_row(raw))
            last = response.get("LastEvaluatedKey")
            if not last:
                return rows
            kwargs["ExclusiveStartKey"] = last

    def get_state(self, review_week: str) -> WeekState:
        raw = self._get(review_week, _STATE_KEY)
        if raw is None:
            return WeekState(review_week)
        return WeekState(
            review_week=review_week,
            mark_seq=int(raw.get("mark_seq", 0)),
            recomputed_seq=int(raw.get("recomputed_seq", 0)),
            rebuild_required=bool(raw.get("rebuild_required", False)),
            rebuild_reason=raw.get("rebuild_reason"),
        )

    def list_pending_weeks(self) -> list[str]:
        return self._week_set(_PENDING_KEY)

    def list_rebuild_weeks(self) -> list[str]:
        return self._week_set(_REBUILD_KEY)

    def get_backfill_status(self) -> BackfillStatus:
        raw = self._get(_CONTROL_WEEK, _BACKFILL_KEY)
        if raw is None or raw.get("status") != "COMPLETE":
            return BackfillStatus(complete=False)
        completed_at = raw.get("completed_at")
        return BackfillStatus(
            complete=True,
            completed_at=dt.datetime.fromisoformat(completed_at) if completed_at else None,
            week_count=int(raw.get("week_count", 0)),
            row_count=int(raw.get("row_count", 0)),
        )

    # --- 内部 ---------------------------------------------------------------

    def _get(self, review_week: str, item_key: str) -> dict[str, Any] | None:
        response = self._client.get_item(
            TableName=self._aggregate_table,
            Key=_key(review_week, item_key),
            ConsistentRead=True,
        )
        item = response.get("Item")
        if item is None:
            return None
        return {k: _deserializer.deserialize(v) for k, v in item.items()}

    def _week_set(self, item_key: str) -> list[str]:
        raw = self._get(_INDEX_WEEK, item_key)
        if raw is None:
            return []
        return sorted(raw.get("weeks", set()))

    @staticmethod
    def _row_item(row: WeeklyEvaluationAggregate) -> dict[str, Any]:
        item: dict[str, Any] = {
            _HASH: row.review_week,
            _RANGE: row.item_key,
            "recommendation_type": row.recommendation_type.value,
            "rule_version": row.rule_version,
            "sample_count": row.sample_count,
            "conclusive_count": row.conclusive_count,
            "success_count": row.success_count,
            "price_return_sum": row.price_return_sum,
            "price_return_count": row.price_return_count,
            "excess_return_sum": row.excess_return_sum,
            "excess_return_count": row.excess_return_count,
            "updated_at": row.updated_at.isoformat(),
            "schema_version": row.schema_version,
        }
        for label, count in row.label_counts.items():
            item[f"{_LABEL_ATTR_PREFIX}{label}"] = count
        return {k: serialize(v) for k, v in item.items()}

    @staticmethod
    def _decode_row(raw_item: dict[str, Any]) -> WeeklyEvaluationAggregate:
        raw = {k: _deserializer.deserialize(v) for k, v in raw_item.items()}
        label_counts = {
            k[len(_LABEL_ATTR_PREFIX) :]: int(v)
            for k, v in raw.items()
            if k.startswith(_LABEL_ATTR_PREFIX)
        }
        return WeeklyEvaluationAggregate(
            review_week=raw[_HASH],
            recommendation_type=RecommendationType(raw["recommendation_type"]),
            rule_version=raw["rule_version"],
            sample_count=int(raw.get("sample_count", 0)),
            conclusive_count=int(raw.get("conclusive_count", 0)),
            success_count=int(raw.get("success_count", 0)),
            price_return_sum=Decimal(raw.get("price_return_sum", 0)),
            price_return_count=int(raw.get("price_return_count", 0)),
            excess_return_sum=Decimal(raw.get("excess_return_sum", 0)),
            excess_return_count=int(raw.get("excess_return_count", 0)),
            label_counts=label_counts,
            updated_at=dt.datetime.fromisoformat(raw["updated_at"]),
            schema_version=int(raw.get("schema_version", AGGREGATE_SCHEMA_VERSION)),
        )
