"""Issue #65 F-E10(2): 通知リトライ予算の加算を条件付きにする。

`record_notification_failed()` の `ADD notification_failure_count :one` は
**無条件**だった。2 つの finalizer が同時に走ると予算が倍速で減り、本来の
半分の試行回数で `COMPLETED_WITH_NOTIFICATION_FAILURE` へ到達しえた
（= **通知が届かないまま打ち切られる**）。

★ 前後の遷移は既に条件付きで、**この 1 つだけが非対称**だった。
    mark_notification_pending   `#status = :write_completed`
    try_retry_notification      `#status = :failed`
    record_notification_failed  **条件なし**

★ 別 write へ分けない理由は `try_acquire_completion_finalize()`（Issue #57 B2）
  と同じ。同関数のコメントが**本 finding を名指し**して
  「同型の欠陥を再生産しないため」と書いている。

★ 条件不成立でも**例外を送出しない**。送出すると「通知に失敗しただけで
  finalize 全体も落ちる」という別の欠陥になる。

★ 実在の銘柄コード・銘柄名は使用しない。
"""

from __future__ import annotations

import datetime as dt
import logging

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.infrastructure.aws.batch_tracker import (
    WatchlistBatchStatus,
    get_watchlist_batch,
    record_notification_failed,
    try_retry_notification,
)

_NOW = dt.datetime(2026, 9, 10, 7, 0, tzinfo=dt.UTC)
_TABLE = "jstock-batch_runs"


@pytest.fixture
def dynamo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-northeast-1")
        client.create_table(
            TableName=_TABLE,
            KeySchema=[{"AttributeName": "batch_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "batch_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def _seed(batch_id: str, status: WatchlistBatchStatus, failure_count: int | None = None) -> None:
    item: dict = {"batch_id": {"S": batch_id}, "status": {"S": status.value}}
    if failure_count is not None:
        item["notification_failure_count"] = {"N": str(failure_count)}
    boto3.client("dynamodb", region_name="ap-northeast-1").put_item(
        TableName=_TABLE, Item=item
    )


# --- 条件成立: 従来どおり ---------------------------------------------------------------


def test_pending_records_the_failure_and_increments(dynamo) -> None:
    """条件成立（NOTIFICATION_PENDING）なら従来どおり遷移し +1 されること。"""
    _seed("b-1", WatchlistBatchStatus.NOTIFICATION_PENDING)

    assert record_notification_failed("b-1", _NOW, "LINE push failed") == 1

    item = get_watchlist_batch("b-1")
    assert item is not None
    assert item["status"] == WatchlistBatchStatus.NOTIFICATION_FAILED.value
    assert int(item["notification_failure_count"]) == 1
    assert item["last_notification_error"] == "LINE push failed"


def test_repeated_attempts_increment_one_by_one(dynamo) -> None:
    """★ 通常経路（1 つの finalizer が順に失敗を記録する）が不変であること。

    PENDING へ戻してから記録する、という既存の往復を 3 回繰り返す。
    """
    _seed("b-2", WatchlistBatchStatus.NOTIFICATION_PENDING)

    counts = []
    for _ in range(3):
        counts.append(record_notification_failed("b-2", _NOW, "boom"))
        # try_retry_notification が FAILED -> PENDING へ戻す（既存の遷移）。
        assert try_retry_notification("b-2", _NOW) is True

    assert counts == [1, 2, 3]


# --- ★ 条件不成立: 予算を消費しない ------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        WatchlistBatchStatus.NOTIFICATION_FAILED,
        WatchlistBatchStatus.COMPLETED,
        WatchlistBatchStatus.NOTIFICATION_SENT,
        WatchlistBatchStatus.WATCHLIST_WRITE_COMPLETED,
    ],
)
def test_non_pending_does_not_consume_the_budget(
    dynamo, status: WatchlistBatchStatus
) -> None:
    """★ 本 finding の中心。PENDING でなければ**カウンタが増えない**こと。

    2 つ目の finalizer が予算を減らしてしまうと、本来の半分の試行回数で
    打ち切られ、通知が届かないまま終わる。
    """
    _seed("b-3", status, failure_count=1)

    assert record_notification_failed("b-3", _NOW, "boom") == 1

    item = get_watchlist_batch("b-3")
    assert item is not None
    assert int(item["notification_failure_count"]) == 1  # ★ 増えていない
    assert item["status"] == status.value  # ★ 状態も書き換えない


def test_two_concurrent_finalizers_consume_only_one_attempt(dynamo) -> None:
    """★ 2 つの finalizer を模して 2 回呼んでも **予算が 1 しか減らない**こと。

    = 本 finding の主張（予算が倍速で減る）の直接検証。
    """
    _seed("b-4", WatchlistBatchStatus.NOTIFICATION_PENDING)

    first = record_notification_failed("b-4", _NOW, "boom")
    second = record_notification_failed("b-4", _NOW, "boom")

    assert first == 1
    assert second == 1  # ★ 2 つ目は加算せず現在値を返す
    item = get_watchlist_batch("b-4")
    assert item is not None
    assert int(item["notification_failure_count"]) == 1


def test_condition_failure_does_not_raise(dynamo) -> None:
    """★ 条件不成立でも例外を送出しないこと。

    送出すると「通知に失敗しただけで finalize 全体も落ちる」という別の欠陥に
    なる（呼び出し側は戻り値だけで予算を判定しており、例外を前提にしていない）。
    """
    _seed("b-5", WatchlistBatchStatus.COMPLETED)

    # 例外が出れば pytest がここで失敗する。
    assert record_notification_failed("b-5", _NOW, "boom") == 0


def test_condition_failure_without_the_attribute_returns_zero(dynamo) -> None:
    """★ 属性が無い場合は **0**（まだ 1 度も失敗していない）を返すこと。

    「測れなかった」と「0 件」を混同しない、という #65 F-F8 で守った区別と同じ。
    """
    _seed("b-6", WatchlistBatchStatus.COMPLETED)  # failure_count 属性なし

    assert record_notification_failed("b-6", _NOW, "boom") == 0


def test_condition_failure_is_warned_not_silently_accepted(
    dynamo, caplog: pytest.LogCaptureFixture
) -> None:
    """失敗の可視性: 記録できなかったことを WARNING で残すこと。"""
    _seed("b-7", WatchlistBatchStatus.COMPLETED, failure_count=2)

    with caplog.at_level(logging.WARNING):
        assert record_notification_failed("b-7", _NOW, "boom") == 2

    assert any(
        "notification failure not recorded" in r.getMessage() for r in caplog.records
    )


def test_the_normal_path_does_not_warn(dynamo, caplog: pytest.LogCaptureFixture) -> None:
    """★ 通常時は WARNING を出さないこと（ノイズを増やさない）。"""
    _seed("b-8", WatchlistBatchStatus.NOTIFICATION_PENDING)

    with caplog.at_level(logging.WARNING):
        record_notification_failed("b-8", _NOW, "boom")

    assert "notification failure not recorded" not in caplog.text


# --- 構造（ADD と条件が同一 UpdateItem であること） ---------------------------------------


def test_the_add_and_the_condition_live_in_the_same_update_item() -> None:
    """★ 加算と条件を**別 write へ分けていない**こと。

    分けると「加算したのに遷移していない / 遷移したのに加算されていない」
    中間状態が生まれ、予算管理が破綻する（`try_acquire_completion_finalize()`
    = Issue #57 B2 と同じ理由。同関数のコメントが本 finding を名指ししている）。
    """
    from pathlib import Path

    source = Path("src/jstock_advisor/infrastructure/aws/batch_tracker.py").read_text(
        encoding="utf-8"
    )
    block = source.split("def record_notification_failed(", 1)[1]
    block = block.split("\ndef ", 1)[0]
    add_at = block.index("ADD notification_failure_count :one")
    condition_at = block.index('ConditionExpression="#status = :pending"')
    update_item_at = block.index("update_item(")
    # 同じ update_item(...) 呼び出しの中に両方があること。
    assert update_item_at < add_at
    assert update_item_at < condition_at
    # ★ 2 回目の update_item が無いこと（= 別 write へ分けていない）。
    assert block.count("update_item(") == 1
