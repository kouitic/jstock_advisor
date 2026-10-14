"""資金入替の観測のための DynamoDB read-only reader(Issue #604 P-b2。#128 R2。dormant)。

BatchRuns の保有バッチの行・保有ごとの評価記録(GSI の範囲指定 Query)・Recommendation(BatchGetItem)を
読む。設計: #604 P-b 設計 rev6(…6169320796)+ rev8(…6169691749)・設計レビュー PASS(…6169650442)。

## read-only の保証(operations_manual 18節。名前だけで read-only と判断しない)

* boto3 の client は ★ allowlist の proxy(`RotationReadOnlyClient`)★ で包む。許可するのは
  `get_item`・`batch_get_item`・`query` だけ。★ `scan` も書込も `describe_table` も許可しない ★
  (それ以外の属性アクセスは `ReadOnlyViolationError`)。保有の列挙(Scan)の adapter は P-b4
  (USER の IAM 承認の後)で、別の proxy にする。★ Scan を呼べるコードをここに置かない ★
* 書込メソッドを持つ `DynamoDbCollectionStore`・`CollectionStore`・repository は使わない
  (read 名の関数から推移的に write へ到達しうるため)。項目の decode は
  `{主キー, "data"(JSON 文字列)}` の形を自前で読む。
* client・表名・sleep・resolver は ★ 引数で受ける ★(環境変数・設定・時計を読まない)。boto3 は
  `build_rotation_read_client()` の中でだけ遅延 import する。

## 範囲指定 Query の境界(★ UTC の isoformat ★。rev6 F1)

保有バッチの `evaluated_at` は `datetime.now(UTC).isoformat()` の
★ `+00:00` の文字列 ★ で GSI の RANGE に入る。窓(JST の aware)を
★ UTC の `isoformat()` へ直して ★ BETWEEN へ渡す(JST の文字列だと辞書順の比較が壊れる)。
マイクロ秒の有無は `+` < `.` で辞書順が時刻順と一致する。
★ 読んだ行の `evaluated_at` が `+00:00`(または `Z`)で終わらない、または記録の本体の offset が
0 でない行は、1 件の読取の失敗として
数える ★(rev8 SHOULD-1。推測で解釈しない)。

## 失敗の扱い

どの reader も例外を送出せず、失敗は戻り値(`None` / `RecordRead(FAILED)` / `failed` の id)で返す
(値・識別子・例外の文面を持たない)。呼び出し側(`rotation_observation_inputs`)が 1 件ずつ隔離する。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from typing import Any, Final

from jstock_advisor.domain.entities.holding_evaluation_record import HoldingEvaluationRecord
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.rotation_observation import BatchFacts
from jstock_advisor.domain.signals.rotation_observation_reads import (
    RecommendationsRead,
    RecordRead,
    RecordStatus,
)
from jstock_advisor.domain.signals.rotation_source_eligibility import (
    EligibilityWindow,
    RecommendationFacts,
    facts_from_record,
    recommendation_facts,
)
from jstock_advisor.infrastructure.aws.audit_shadow_reader import ReadOnlyViolationError

ALLOWED_OPERATIONS: Final[frozenset[str]] = frozenset({"get_item", "batch_get_item", "query"})

#: 1 保有の範囲指定 Query のページ数の上限(通常は 1 件・1 ページ。超えたら『読めない』)
MAX_QUERY_PAGES: Final = 3
#: BatchGetItem の 1 リクエストの上限(AWS 仕様)
BATCH_GET_CHUNK: Final = 100
#: UnprocessedKeys の再送の上限(`dynamodb_store` と同じ方針)
MAX_BATCH_RETRIES: Final = 5
BATCH_BASE_DELAY_SECONDS: Final = 0.5
BATCH_MAX_DELAY_SECONDS: Final = 5.0

HOLDING_ID_INDEX_NAME: Final = "holding_id-index"
_UTC_SUFFIXES: Final = ("+00:00", "Z")


class RotationReadOnlyClient:
    """`get_item` / `batch_get_item` / `query` だけを通す client の proxy。それ以外は例外。"""

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        # __getattr__ はインスタンス属性に無い名前のときだけ呼ばれる。allowlist だけを通す。
        if name not in ALLOWED_OPERATIONS:
            raise ReadOnlyViolationError(
                f"read-only reader は{sorted(ALLOWED_OPERATIONS)}のみ許可する(要求: {name})"
            )
        return getattr(self._client, name)


def build_rotation_read_client() -> RotationReadOnlyClient:
    """呼び出し元の資格情報で read-only の client を作る。boto3 は遅延 import する。"""
    import boto3  # noqa: PLC0415 - 配線(P-b4)の実行時にだけ必要

    return RotationReadOnlyClient(boto3.client("dynamodb"))


# --- 小さな補助(型付き属性の読み) ------------------------------------------------------------


def _string(item: Any, name: str) -> str | None:
    value = item.get(name) if isinstance(item, dict) else None
    if isinstance(value, dict):
        raw = value.get("S")
        return raw if isinstance(raw, str) else None
    return None


def _aware(text: str | None) -> dt.datetime | None:
    if text is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _utc_iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat()


# --- BatchRuns(保有バッチの開始・完了確認の時刻) -----------------------------------------------


def read_batch_facts(
    client: RotationReadOnlyClient, table_name: str, batch_id: str
) -> BatchFacts | None:
    """BatchRuns の行(GetItem)から開始時刻・完了確認の時刻を読む。

    `None` = 読めない(行なし〔TTL・手動起動を含む〕・通信の失敗・形が違う)。`BatchFacts` の
    各時刻は欠落なら None(完了確認の無い行は『まだ終わっていない』)。
    値が読めない形(時刻でない文字列・
    offset なし)は、推測せず `None`(読めない)にする。
    """
    try:
        response = client.get_item(TableName=table_name, Key={"batch_id": {"S": batch_id}})
    except Exception:  # noqa: BLE001 - 失敗は『読めない』として返す(値・例外の文面を持たない)
        return None
    item = response.get("Item") if isinstance(response, dict) else None
    if not item:
        return None
    started_text = _string(item, "started_at")
    completed_text = _string(item, "completion_finalize_completed_at")
    started = _aware(started_text)
    completed = _aware(completed_text)
    if (started_text is not None and started is None) or (
        completed_text is not None and completed is None
    ):
        return None
    return BatchFacts(started, completed)


# --- 保有ごとの評価記録(GSI の範囲指定 Query) ---------------------------------------------------


class HoldingRecordReader:
    """`HoldingRecordPort` の実装。窓(UTC の isoformat)の内側の最新の記録 1 件を読む。"""

    def __init__(
        self,
        client: RotationReadOnlyClient,
        table_name: str,
        index_name: str = HOLDING_ID_INDEX_NAME,
    ) -> None:
        self._client = client
        self._table_name = table_name
        self._index_name = index_name

    def latest_in_window(self, holding_id: str, window: EligibilityWindow) -> RecordRead:
        bounds = {
            ":h": {"S": holding_id},
            ":s": {"S": _utc_iso(window.start)},
            ":e": {"S": _utc_iso(window.end)},
        }
        try:
            items = self._query_all(bounds)
        except Exception:  # noqa: BLE001 - 失敗は 1 件の読取の失敗(値・識別子を持たない)
            return RecordRead(RecordStatus.FAILED)
        if items is None:
            return RecordRead(RecordStatus.FAILED)
        if not items:
            return RecordRead(RecordStatus.NOT_FOUND)
        decoded: list[HoldingEvaluationRecord] = []
        for item in items:
            record = self._decode(item, window)
            if record is None:
                return RecordRead(RecordStatus.FAILED)
            decoded.append(record)
        latest = max(decoded, key=lambda record: record.evaluated_at)
        return RecordRead(RecordStatus.FOUND, facts_from_record(latest))

    def _query_all(self, values: dict[str, Any]) -> list[Any] | None:
        items: list[Any] = []
        start_key: Any = None
        for _ in range(MAX_QUERY_PAGES):
            kwargs: dict[str, Any] = {
                "TableName": self._table_name,
                "IndexName": self._index_name,
                "KeyConditionExpression": "holding_id = :h AND evaluated_at BETWEEN :s AND :e",
                "ExpressionAttributeValues": values,
            }
            if start_key is not None:
                kwargs["ExclusiveStartKey"] = start_key
            response = self._client.query(**kwargs)
            items.extend(response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return items
        return None  # ページの上限を超えた = 読み切れていない

    @staticmethod
    def _decode(item: Any, window: EligibilityWindow) -> HoldingEvaluationRecord | None:
        stamp = _string(item, "evaluated_at")
        data = _string(item, "data")
        if stamp is None or data is None or not stamp.endswith(_UTC_SUFFIXES):
            return None  # UTC で書かれていない行は、文字列の比較を信じない
        try:
            record = HoldingEvaluationRecord.model_validate_json(data)
        except ValueError:
            return None
        offset = record.evaluated_at.utcoffset()
        if offset != dt.timedelta(0) or not window.contains(record.evaluated_at):
            return None
        return record


# --- Recommendation(BatchGetItem) --------------------------------------------------------------


class RecommendationReader:
    """`RecommendationPort` の実装。id を 100 件ずつ引き、事実にする。

    attention_origin は注入された resolver で再計算する。
    """

    def __init__(
        self,
        client: RotationReadOnlyClient,
        table_name: str,
        resolve_attention_origin: Callable[[Recommendation], str | None],
        sleep: Callable[[float], None],
    ) -> None:
        self._client = client
        self._table_name = table_name
        self._resolve = resolve_attention_origin
        self._sleep = sleep

    def get_many(self, ids: Sequence[str]) -> RecommendationsRead:
        unique = list(dict.fromkeys(ids))
        found: dict[str, RecommendationFacts] = {}
        failed: set[str] = set()
        for start in range(0, len(unique), BATCH_GET_CHUNK):
            self._read_chunk(unique[start : start + BATCH_GET_CHUNK], found, failed)
        return RecommendationsRead(found, frozenset(failed - set(found)))

    def _read_chunk(
        self, chunk: list[str], found: dict[str, RecommendationFacts], failed: set[str]
    ) -> None:
        pending = [{"recommendation_id": {"S": item_id}} for item_id in chunk]
        attempt = 0
        while pending:
            try:
                response = self._client.batch_get_item(
                    RequestItems={self._table_name: {"Keys": pending}}
                )
            except Exception:  # noqa: BLE001 - その chunk の残りを『読めなかった』にする
                failed.update(self._ids_of(pending))
                return
            for item in response.get("Responses", {}).get(self._table_name, []):
                self._decode(item, found, failed)
            pending = response.get("UnprocessedKeys", {}).get(self._table_name, {}).get("Keys", [])
            if not pending:
                return
            attempt += 1
            if attempt > MAX_BATCH_RETRIES:
                failed.update(self._ids_of(pending))  # 『存在しない』と混同しない
                return
            self._sleep(min(BATCH_MAX_DELAY_SECONDS, BATCH_BASE_DELAY_SECONDS * 2 ** (attempt - 1)))

    @staticmethod
    def _ids_of(keys: list[dict[str, Any]]) -> set[str]:
        ids: set[str] = set()
        for key in keys:
            text = _string(key, "recommendation_id")
            if text is not None:
                ids.add(text)
        return ids

    def _decode(self, item: Any, found: dict[str, RecommendationFacts], failed: set[str]) -> None:
        item_id = _string(item, "recommendation_id")
        data = _string(item, "data")
        if item_id is None:
            return
        try:
            if data is None:
                raise ValueError("no data")
            recommendation = Recommendation.model_validate_json(data)
            found[item_id] = recommendation_facts(recommendation, self._resolve)
        except Exception:  # noqa: BLE001 - その id だけ『読めなかった』(値・例外の文面を持たない)
            failed.add(item_id)
