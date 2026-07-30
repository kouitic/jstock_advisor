"""Lambda銘柄単位ファンアウト(lambda_handlers/_fanout.py)の完了検知用カウンタ。

DynamoDBの原子的なADD操作(UpdateItem)で完了件数・区分別内訳をカウントし、
最後の1件を処理したワーカーが「自分が最後だった」と検知してサマリー通知を送信する
(Step Functions等の追加インフラを使わない軽量な集約方式)。

ローカル(非Lambda)環境では常にNoneを返す。_fanout.py自体がLambda上でのみ
非同期再帰呼び出しを行う設計であり、ローカルCLIはこの機構を使わないため。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import boto3

from jstock_advisor.domain.entities.evaluation_audit import SUMMARY_CATEGORIES
from jstock_advisor.infrastructure.collection_store import resolve_table_name, running_on_lambda

_TABLE_FILE_NAME = "batch_runs.json"  # resolve_table_nameの命名規則(jstock-batch_runs)に合わせる
_TTL_HOURS = 6  # 集計用の一時データのため、数時間で自動削除する

# 銘柄コード一覧を記録する区分(要求仕様§13: 処理失敗・データ不足は銘柄コードも表示する)
_CATEGORIES_WITH_STOCK_CODES = ("data_insufficient", "failed")


@dataclass(frozen=True)
class BatchProgress:
    total: int
    completed: int
    category_counts: dict[str, int]
    data_insufficient_stock_codes: list[str]
    failed_stock_codes: list[str]
    # 買い候補分析の優先度付け通知(2026-07仕様追加)向け。record_resultに
    # ranking_entryを渡した銘柄が、生の"score|stock_code|recommendation_id"
    # 文字列のまま集約される(順序はDynamoDBの文字列セットのため保証されない。
    # 呼び出し側でパース・ソートすること)。
    ranking_entries: list[str]

    @property
    def is_complete(self) -> bool:
        return self.completed >= self.total


def _table() -> Any:
    return boto3.resource("dynamodb").Table(resolve_table_name(_TABLE_FILE_NAME))


def start_batch(batch_id: str, total: int, now: dt.datetime) -> None:
    """ファンアウト開始時に呼ぶ。ローカル環境・対象0件の場合は何もしない。"""
    if total <= 0 or not running_on_lambda():
        return
    ttl = int((now + dt.timedelta(hours=_TTL_HOURS)).timestamp())
    item: dict[str, Any] = {"batch_id": batch_id, "total": total, "completed": 0, "ttl": ttl}
    for category in SUMMARY_CATEGORIES:
        item[category] = 0
    _table().put_item(Item=item)


def record_result(
    batch_id: str,
    category: str,
    stock_code: str | None = None,
    ranking_entry: str | None = None,
) -> BatchProgress | None:
    """1銘柄の処理完了を原子的に記録し、現在の進捗を返す(ローカル環境ではNone)。

    categoryは"sent"/"hold"/"review"/"data_insufficient"/"suppressed"/"failed"/
    "candidate_not_ranked"(domain/entities/evaluation_audit.pyのSUMMARY_CATEGORIES
    と同じ集合)。data_insufficient/failedの場合、stock_codeを渡すとDynamoDBの
    文字列セットへ原子的に追加し、バッチサマリーで銘柄コードを表示できるようにする。

    ranking_entryを渡すと、優先度付け通知(要求仕様2026-07追加)向けに、任意の
    文字列(呼び出し側でスコア等を含めてエンコードする)をDynamoDBの文字列セットへ
    原子的に追加する。バッチ完了検知後、呼び出し側がこの一覧をパース・ソートして
    上位N件のみ通知する用途を想定している。
    """
    if not running_on_lambda():
        return None
    if category not in SUMMARY_CATEGORIES:
        raise ValueError(f"unknown batch result category: {category}")

    # categoryは"hold"等、DynamoDBの予約語と衝突しうる文字列をそのまま属性名に使うため、
    # ExpressionAttributeNamesで必ずプレースホルダ経由にする(直書きするとUpdateItemが
    # ValidationException: reserved keywordで失敗する)。
    names: dict[str, str] = {"#category": category, "#completed": "completed"}
    update_expr = "ADD #category :one, #completed :one"
    values: dict[str, Any] = {":one": 1}
    if stock_code is not None and category in _CATEGORIES_WITH_STOCK_CODES:
        names["#category_codes"] = f"{category}_codes"
        update_expr += ", #category_codes :codes"
        values[":codes"] = {stock_code}
    if ranking_entry is not None:
        names["#ranking_entries"] = "ranking_entries"
        update_expr += ", #ranking_entries :ranking_entries"
        values[":ranking_entries"] = {ranking_entry}

    response = _table().update_item(
        Key={"batch_id": batch_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    item = response["Attributes"]
    return BatchProgress(
        total=int(item["total"]),
        completed=int(item["completed"]),
        category_counts={category: int(item.get(category, 0)) for category in SUMMARY_CATEGORIES},
        data_insufficient_stock_codes=sorted(item.get("data_insufficient_codes", set())),
        failed_stock_codes=sorted(item.get("failed_codes", set())),
        ranking_entries=sorted(item.get("ranking_entries", set())),
    )
