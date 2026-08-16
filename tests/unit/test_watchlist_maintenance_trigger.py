"""平日毎日起動化(2026-08)対応: WATCHLIST_MAINTENANCE後続起動トリガー
(`watchlist_batch_finalizer.maybe_trigger_maintenance`)のテスト。

DynamoDBの条件付き更新の意味論自体は test_watchlist_batch_tracker.py で
moto検証済みのため、ここでは`try_acquire_maintenance_trigger`/
`mark_maintenance_triggered`/`boto3.client("lambda").invoke`をモック化し、
呼び出し順序・ガード条件・invoke失敗時の挙動のみを検証する。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.services import watchlist_batch_finalizer as finalizer

_NOW = dt.datetime(2026, 8, 17, 6, 30, tzinfo=dt.UTC)


def _fake_config(*, auto_removal_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        watchlist_screening=SimpleNamespace(
            auto_removal=SimpleNamespace(enabled=auto_removal_enabled)
        )
    )


class _FakeLambdaClient:
    def __init__(self, *, raise_on_invoke: bool = False) -> None:
        self.raise_on_invoke = raise_on_invoke
        self.invoke_calls: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        self.invoke_calls.append(kwargs)
        if self.raise_on_invoke:
            raise RuntimeError("simulated invoke failure")
        return {"StatusCode": 202}


def _patch_lambda_running(monkeypatch: pytest.MonkeyPatch, *, on_lambda: bool = True) -> None:
    monkeypatch.setattr(finalizer, "running_on_lambda", lambda: on_lambda)
    monkeypatch.setenv("WATCHLIST_DISPATCHER_FUNCTION_NAME", "jstock-advisor-watchlist-dispatcher")


def test_triggers_maintenance_on_normal_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    """通常のNEW_CANDIDATE_SCREENING業務finalize完了で1回起動されること
    (テスト#4/#8: 一部candidateがNOT_FOUND/FAILED_REQUIRED等でもバッチ全体の
    finalizeが正常ならmaintenanceを起動できる=batch_item自体に個別candidateの
    結果は含まれないため、この関数のガード条件はjob_type/config有効性のみで
    あることを直接確認する)。"""
    _patch_lambda_running(monkeypatch)
    acquire_calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        finalizer,
        "try_acquire_maintenance_trigger",
        lambda *a, **kw: (acquire_calls.append(a) or True),
    )
    fake_client = _FakeLambdaClient()
    monkeypatch.setattr(finalizer.boto3, "client", lambda *_a, **_kw: fake_client)
    marked: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        finalizer, "mark_maintenance_triggered", lambda *a: marked.append(a)
    )

    batch_item = {"job_type": "NEW_CANDIDATE_SCREENING"}
    finalizer.maybe_trigger_maintenance("batch-1", batch_item, _NOW, _fake_config())

    assert len(acquire_calls) == 1
    assert acquire_calls[0][0] == "batch-1"
    assert acquire_calls[0][1] == "watchlist-maint-batch-1"
    assert len(fake_client.invoke_calls) == 1
    payload = fake_client.invoke_calls[0]
    assert payload["InvocationType"] == "Event"
    assert payload["FunctionName"] == "jstock-advisor-watchlist-dispatcher"
    assert marked == [("batch-1", _NOW)]


def test_invoke_payload_contains_trigger_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """テスト#9: triggered_by_batch_id/trigger_typeがchild batchへ正しく
    引き継がれること(invoke payload経由で伝播することを確認)。"""
    _patch_lambda_running(monkeypatch)
    monkeypatch.setattr(finalizer, "try_acquire_maintenance_trigger", lambda *a, **kw: True)
    fake_client = _FakeLambdaClient()
    monkeypatch.setattr(finalizer.boto3, "client", lambda *_a, **_kw: fake_client)
    monkeypatch.setattr(finalizer, "mark_maintenance_triggered", lambda *a: None)

    finalizer.maybe_trigger_maintenance(
        "batch-1", {"job_type": "NEW_CANDIDATE_SCREENING"}, _NOW, _fake_config()
    )

    import json

    payload = json.loads(fake_client.invoke_calls[0]["Payload"])
    assert payload["job_type"] == "WATCHLIST_MAINTENANCE"
    assert payload["batch_id"] == "watchlist-maint-batch-1"
    assert payload["triggered_by_batch_id"] == "batch-1"
    assert payload["trigger_type"] == "POST_NEW_CANDIDATE_SCREENING"


def test_skips_for_watchlist_maintenance_job_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """WATCHLIST_MAINTENANCE自身のfinalizeからは連鎖起動しないこと。"""

    def _fail_if_called(*_a: Any, **_kw: Any) -> bool:
        pytest.fail("try_acquire_maintenance_trigger should not be called for maintenance batches")

    monkeypatch.setattr(finalizer, "try_acquire_maintenance_trigger", _fail_if_called)

    finalizer.maybe_trigger_maintenance(
        "watchlist-maint-1", {"job_type": "WATCHLIST_MAINTENANCE"}, _NOW, _fake_config()
    )


def test_skips_when_auto_removal_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail_if_called(*_a: Any, **_kw: Any) -> bool:
        pytest.fail("try_acquire_maintenance_trigger should not be called when disabled")

    monkeypatch.setattr(finalizer, "try_acquire_maintenance_trigger", _fail_if_called)

    finalizer.maybe_trigger_maintenance(
        "batch-1",
        {"job_type": "NEW_CANDIDATE_SCREENING"},
        _NOW,
        _fake_config(auto_removal_enabled=False),
    )


def test_skips_when_lease_not_acquired(monkeypatch: pytest.MonkeyPatch) -> None:
    """テスト#5相当: 既にTRIGGERED、または他の主体がlease保持中の場合はinvokeしない
    (exactly-once)。"""
    _patch_lambda_running(monkeypatch)
    monkeypatch.setattr(finalizer, "try_acquire_maintenance_trigger", lambda *a, **kw: False)

    def _fail_if_called(*_a: Any, **_kw: Any) -> Any:
        pytest.fail("lambda client should not be constructed when lease is not acquired")

    monkeypatch.setattr(finalizer.boto3, "client", _fail_if_called)

    finalizer.maybe_trigger_maintenance(
        "batch-1", {"job_type": "NEW_CANDIDATE_SCREENING"}, _NOW, _fake_config()
    )


def test_invoke_failure_does_not_mark_triggered(monkeypatch: pytest.MonkeyPatch) -> None:
    """テスト#6: invoke失敗時に処理が消失せずretry可能な状態になること
    (mark_maintenance_triggeredが呼ばれないため、TRIGGERINGのまま残り
    Reconcilerが再試行できる)。"""
    _patch_lambda_running(monkeypatch)
    monkeypatch.setattr(finalizer, "try_acquire_maintenance_trigger", lambda *a, **kw: True)
    fake_client = _FakeLambdaClient(raise_on_invoke=True)
    monkeypatch.setattr(finalizer.boto3, "client", lambda *_a, **_kw: fake_client)
    marked: list[tuple[Any, ...]] = []
    monkeypatch.setattr(finalizer, "mark_maintenance_triggered", lambda *a: marked.append(a))

    finalizer.maybe_trigger_maintenance(
        "batch-1", {"job_type": "NEW_CANDIDATE_SCREENING"}, _NOW, _fake_config()
    )

    assert len(fake_client.invoke_calls) == 1
    assert marked == []


def test_local_cli_execution_does_not_invoke_real_lambda(monkeypatch: pytest.MonkeyPatch) -> None:
    """ローカルCLI実行時(running_on_lambda()==False)は実際のinvokeを行わない
    (rotation dispatch lease等と同じ方針)。leaseはTRIGGERINGのまま残り、
    無条件でTRIGGERED化しない。"""
    monkeypatch.setattr(finalizer, "running_on_lambda", lambda: False)
    monkeypatch.setattr(finalizer, "try_acquire_maintenance_trigger", lambda *a, **kw: True)

    def _fail_if_called(*_a: Any, **_kw: Any) -> Any:
        pytest.fail("lambda client should not be constructed for local CLI execution")

    monkeypatch.setattr(finalizer.boto3, "client", _fail_if_called)
    marked: list[tuple[Any, ...]] = []
    monkeypatch.setattr(finalizer, "mark_maintenance_triggered", lambda *a: marked.append(a))

    finalizer.maybe_trigger_maintenance(
        "batch-1", {"job_type": "NEW_CANDIDATE_SCREENING"}, _NOW, _fake_config()
    )

    assert marked == []


def test_finish_batch_calls_maybe_trigger_maintenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """テスト#3相当(部分): _finish_batch()(=業務finalize確定のchoke point)から
    maybe_trigger_maintenanceが必ず呼ばれること。DISPATCH_FAILED/TIMED_OUT/
    FINALIZE_FAILEDはそもそも_finish_batch()へ到達しないため
    (既存のrotation commit choke pointと同一)、この呼び出し1本で
    「異常系では起動しない」ことも構造的に保証される。"""
    monkeypatch.setattr(finalizer, "record_batch_audit", lambda **kw: None)
    monkeypatch.setattr(finalizer, "mark_batch_audit_recorded", lambda *a: None)
    monkeypatch.setattr(finalizer, "mark_watchlist_batch_completed", lambda *a, **kw: None)
    monkeypatch.setattr(finalizer, "_maybe_commit_rotation", lambda *a: None)
    triggered: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        finalizer, "maybe_trigger_maintenance", lambda *a: triggered.append(a)
    )

    finalizer._finish_batch(
        "batch-1",
        _NOW,
        _NOW,
        {"job_type": "NEW_CANDIDATE_SCREENING", "finalize_batch_audit_recorded": True},
        [],
        {},
        _fake_config(),
        "NORMAL",
        [],
        [],
        False,
        False,
    )

    assert len(triggered) == 1
    assert triggered[0][0] == "batch-1"
