"""rotation windowの二重dispatch防止lease(本番検証2026-08対応)の単体テスト。

本番で約50秒差の2回のDispatcher起動が同一rotation windowを二重にSQSへ
dispatchした事象を受け、trade_detection_lock.pyと同じ条件付き更新パターンで
実装したwatchlist_rotation_dispatch_lease.pyを検証する。`AWS_LAMBDA_FUNCTION_
NAME`を設定してrunning_on_lambda()==Trueの経路(実際のDynamoDB条件付き更新)を
駆動し、未設定時(ローカルCLI相当)は常に取得成功/no-opとなることも確認する。
"""

from __future__ import annotations

import datetime as dt

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.infrastructure.aws.watchlist_rotation_dispatch_lease import (
    get_rotation_dispatch_lease_status,
    release_rotation_dispatch_lease,
    try_acquire_rotation_dispatch_lease,
)

_REGION = "ap-northeast-1"
_TABLE_NAME = "jstock-watchlist_rotation_dispatch_lease"
_ROTATION_ID = "default"
_NOW = dt.datetime(2026, 8, 15, 12, 10, tzinfo=dt.UTC)


@pytest.fixture
def dynamo_lambda_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "watchlist-dispatcher")
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_TABLE_NAME,
            KeySchema=[{"AttributeName": "rotation_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "rotation_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def test_local_environment_always_returns_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """AWS_LAMBDA_FUNCTION_NAME未設定(ローカルCLI相当)では常にTrue(単一プロセス
    のため排他不要)。release/get_statusも何もしない。"""
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    assert try_acquire_rotation_dispatch_lease(_ROTATION_ID, "batch-1", _NOW, 3600) is True
    assert try_acquire_rotation_dispatch_lease(_ROTATION_ID, "batch-2", _NOW, 3600) is True
    release_rotation_dispatch_lease(_ROTATION_ID, "batch-1")
    assert get_rotation_dispatch_lease_status(_ROTATION_ID) == (None, None, None)


def test_scenario_a_second_dispatcher_blocked_while_first_holds_lease(
    dynamo_lambda_env: object,
) -> None:
    """A: Dispatcher Aがlease取得直後、Dispatcher Bが直後に起動 → Bは取得できない。"""
    acquired_a = try_acquire_rotation_dispatch_lease(_ROTATION_ID, "batch-a", _NOW, 3600)
    assert acquired_a is True

    later = _NOW + dt.timedelta(seconds=50)
    acquired_b = try_acquire_rotation_dispatch_lease(_ROTATION_ID, "batch-b", later, 3600)
    assert acquired_b is False

    active_batch_id, _started, _expires = get_rotation_dispatch_lease_status(_ROTATION_ID)
    assert active_batch_id == "batch-a"


def test_scenario_b_lease_released_then_next_dispatcher_can_acquire(
    dynamo_lambda_env: object,
) -> None:
    """B: A完了後lease解放 → 次回Bは新しいleaseを取得できる。"""
    try_acquire_rotation_dispatch_lease(_ROTATION_ID, "batch-a", _NOW, 3600)
    release_rotation_dispatch_lease(_ROTATION_ID, "batch-a")

    later = _NOW + dt.timedelta(minutes=5)
    acquired_b = try_acquire_rotation_dispatch_lease(_ROTATION_ID, "batch-b", later, 3600)
    assert acquired_b is True

    active_batch_id, _started, _expires = get_rotation_dispatch_lease_status(_ROTATION_ID)
    assert active_batch_id == "batch-b"


def test_scenario_c_stale_lease_recovered_after_expiry(dynamo_lambda_env: object) -> None:
    """C: Aが異常終了(release未呼び出し)→ lease期限切れ後にBが取得可能
    (Lambda異常終了で永久ロックしないための必須要件)。"""
    lease_seconds = 3600
    try_acquire_rotation_dispatch_lease(_ROTATION_ID, "batch-a", _NOW, lease_seconds)

    still_within_lease = _NOW + dt.timedelta(seconds=lease_seconds - 10)
    blocked = try_acquire_rotation_dispatch_lease(_ROTATION_ID, "batch-b", still_within_lease, 3600)
    assert blocked is False

    after_expiry = _NOW + dt.timedelta(seconds=lease_seconds + 10)
    acquired_b = try_acquire_rotation_dispatch_lease(_ROTATION_ID, "batch-b", after_expiry, 3600)
    assert acquired_b is True

    active_batch_id, _started, _expires = get_rotation_dispatch_lease_status(_ROTATION_ID)
    assert active_batch_id == "batch-b"


def test_release_only_own_lease_does_not_steal_newer_owner(dynamo_lambda_env: object) -> None:
    """release()はin_progress_batch_id一致時のみ解放する。stale-recoveryで
    batch-bが既に新しいleaseを取得した後、batch-a(元の保持者)からの遅延した
    release呼び出しがbatch-bのleaseを誤って奪わないことを確認する。"""
    lease_seconds = 3600
    try_acquire_rotation_dispatch_lease(_ROTATION_ID, "batch-a", _NOW, lease_seconds)
    after_expiry = _NOW + dt.timedelta(seconds=lease_seconds + 10)
    try_acquire_rotation_dispatch_lease(_ROTATION_ID, "batch-b", after_expiry, lease_seconds)

    # batch-aが遅れてrelease()を呼んでも、既にbatch-bが保持しているleaseは無事。
    release_rotation_dispatch_lease(_ROTATION_ID, "batch-a")

    active_batch_id, _started, _expires = get_rotation_dispatch_lease_status(_ROTATION_ID)
    assert active_batch_id == "batch-b"


def test_release_when_never_acquired_is_a_safe_no_op(dynamo_lambda_env: object) -> None:
    """未取得状態でのrelease()は例外を投げず何もしない
    (rotation.enabled=falseバッチ等、そもそもleaseを取得していないバッチからの
    呼び出しに対する安全策)。"""
    release_rotation_dispatch_lease(_ROTATION_ID, "never-acquired-batch")
    assert get_rotation_dispatch_lease_status(_ROTATION_ID) == (None, None, None)


def test_get_status_returns_none_tuple_when_absent(dynamo_lambda_env: object) -> None:
    assert get_rotation_dispatch_lease_status(_ROTATION_ID) == (None, None, None)
