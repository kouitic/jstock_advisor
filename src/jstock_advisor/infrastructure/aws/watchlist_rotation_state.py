"""ウォッチリスト新規候補選定の永続ラウンドロビン方式(2026-08)における、
単一行の永続カーソル(`RotationState`、`rotation_id="default"`固定)の
読み書き。

`InvestmentThesisBaselinePointer`([baseline_pointer.py](baseline_pointer.py))と
同じ楽観ロック技法(`pointer_version`条件付き更新)を踏襲する。`CollectionStore`の
汎用`upsert()`は無条件書き込みであり条件付き更新を表現できないため、DynamoDB
環境の更新(`try_commit_rotation_advance`)はこのモジュールが直接boto3を使う。

rotation commitは、`watchlist_batch_finalizer._finish_batch()`が
`mark_watchlist_batch_completed()`を呼んだ直後(=業務処理[ランキング・
ウォッチリスト追加・通知]が確定した時点)にのみ行う。銘柄個別の評価失敗
(poison stock)は`completed>=total`の成立を妨げないため、rotation commit自体を
妨げない(計画Part A-5/A-6参照)。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from jstock_advisor.domain.entities.watchlist import RotationState
from jstock_advisor.infrastructure.collection_store import (
    CollectionStore,
    build_collection_store,
    resolve_table_name,
    running_on_lambda,
)

_TABLE_FILE_NAME = "watchlist_screening_rotation_state.json"
DEFAULT_ROTATION_ID = "default"


def _store(store_dir: Path | None = None) -> CollectionStore[RotationState]:
    return build_collection_store(RotationState, _TABLE_FILE_NAME, "rotation_id", store_dir)


def get_rotation_state(
    rotation_id: str = DEFAULT_ROTATION_ID, store_dir: Path | None = None
) -> RotationState | None:
    return _store(store_dir).get(rotation_id)


def get_rotation_state_consistent(
    rotation_id: str = DEFAULT_ROTATION_ID, store_dir: Path | None = None
) -> RotationState | None:
    """rotation commit失敗時の監査ログ用(observed_versionの記録、本番検証
    2026-08対応)。結果整合性読み取りによる一時的な不一致を避けるため強い
    整合性で読む(通常のget_rotation_state()を一律これへ置き換えない)。
    """
    return _store(store_dir).get_consistent(rotation_id)


def create_rotation_state_if_absent(
    now: dt.datetime,
    rotation_id: str = DEFAULT_ROTATION_ID,
    store_dir: Path | None = None,
) -> RotationState:
    """初回実行時のみ呼ぶ。既に存在する場合は現在値をそのまま返す(冪等)。

    cursor未設定(先頭から開始)・pointer_version=1・cycle_number=1で作成する。
    """
    state = RotationState(
        rotation_id=rotation_id,
        pointer_version=1,
        cycle_number=1,
        last_started_at=now,
    )
    created = _store(store_dir).insert_if_absent(state)
    if created:
        return state
    existing = get_rotation_state(rotation_id, store_dir)
    assert existing is not None
    return existing


def try_commit_rotation_advance(
    expected_version: int,
    new_last_market_segment: str | None,
    new_last_stock_code: str | None,
    wrapped: bool,
    selected_count: int,
    now: dt.datetime,
    rotation_id: str = DEFAULT_ROTATION_ID,
    store_dir: Path | None = None,
) -> bool:
    """cursorを条件付きで前進させる。

    `expected_version`が現在の`pointer_version`と一致する場合のみ更新し、
    `pointer_version`を+1する。一致しない場合(=別のバッチが先にcommitした)は
    Falseを返す(呼び出し側は再試行しない。次回実行が最新のcursorから継続する)。

    `wrapped=True`の場合、`cycle_number`を+1し`cycle_progress_selected_count`を
    `selected_count`へリセットし`last_started_at`を新サイクルの開始時刻として
    更新する。`wrapped=False`の場合は`cycle_progress_selected_count`へ加算する
    のみ(表示専用の概算値、判定ロジックはcursor比較のみに依存する)。
    """
    if running_on_lambda():
        return _commit_dynamodb(
            expected_version,
            new_last_market_segment,
            new_last_stock_code,
            wrapped,
            selected_count,
            now,
            rotation_id,
        )
    return _commit_local(
        expected_version,
        new_last_market_segment,
        new_last_stock_code,
        wrapped,
        selected_count,
        now,
        rotation_id,
        store_dir,
    )


def _commit_local(
    expected_version: int,
    new_last_market_segment: str | None,
    new_last_stock_code: str | None,
    wrapped: bool,
    selected_count: int,
    now: dt.datetime,
    rotation_id: str,
    store_dir: Path | None,
) -> bool:
    store = _store(store_dir)
    current = store.get(rotation_id)
    if current is None or current.pointer_version != expected_version:
        return False
    updates: dict[str, Any] = {
        "pointer_version": expected_version + 1,
        "last_market_segment": new_last_market_segment,
        "last_stock_code": new_last_stock_code,
        "last_completed_at": now,
    }
    if wrapped:
        updates["cycle_number"] = current.cycle_number + 1
        updates["cycle_progress_selected_count"] = selected_count
        updates["last_started_at"] = now
    else:
        updates["cycle_progress_selected_count"] = (
            current.cycle_progress_selected_count + selected_count
        )
    store.upsert(current.model_copy(update=updates))
    return True


def _commit_dynamodb(
    expected_version: int,
    new_last_market_segment: str | None,
    new_last_stock_code: str | None,
    wrapped: bool,
    selected_count: int,
    now: dt.datetime,
    rotation_id: str,
) -> bool:
    """2026-08修正(本番検証で発覚): 以前はpointer_version等をトップレベルの
    ネイティブ属性として直接update_itemしていたが、このテーブルの実際の
    アイテムはDynamoDbCollectionStore(get_rotation_state()/
    create_rotation_state_if_absent()が使う)が書き込む「PK + data(JSON文字列)」
    スキーマ(dynamodb_store.py:_to_item())であり、トップレベルにpointer_version
    属性が存在しない。このためConditionExpressionが常にConditionalCheckFailed
    Exceptionとなり、rotation commitが恒久的に失敗していた(既存本番state・
    移行不要)。

    修正後は同じ「PK + data」スキーマに合わせ、data属性全体の完全一致を
    条件とする条件付き更新でCASを実現する(2クライアントが同時に同じ
    data文字列を読んでいた場合のみ、片方だけが成功する)。
    """
    import boto3
    from botocore.exceptions import ClientError

    table: Any = boto3.resource("dynamodb").Table(resolve_table_name(_TABLE_FILE_NAME))
    response = table.get_item(Key={"rotation_id": rotation_id}, ConsistentRead=True)
    item = response.get("Item")
    if item is None:
        return False

    current_data = item["data"]
    current = RotationState.model_validate_json(current_data)
    if current.pointer_version != expected_version:
        return False

    updates: dict[str, Any] = {
        "pointer_version": expected_version + 1,
        "last_market_segment": new_last_market_segment,
        "last_stock_code": new_last_stock_code,
        "last_completed_at": now,
    }
    if wrapped:
        updates["cycle_number"] = current.cycle_number + 1
        updates["cycle_progress_selected_count"] = selected_count
        updates["last_started_at"] = now
    else:
        updates["cycle_progress_selected_count"] = (
            current.cycle_progress_selected_count + selected_count
        )
    new_state = current.model_copy(update=updates)

    try:
        table.update_item(
            Key={"rotation_id": rotation_id},
            ConditionExpression="#data = :expected_data",
            UpdateExpression="SET #data = :new_data",
            ExpressionAttributeNames={"#data": "data"},
            ExpressionAttributeValues={
                ":expected_data": current_data,
                ":new_data": new_state.model_dump_json(),
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
