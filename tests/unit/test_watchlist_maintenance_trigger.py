"""平日毎日起動化(2026-08)対応・再レビュー修正(2026-08): WATCHLIST_MAINTENANCE
後続起動トリガー(`watchlist_batch_finalizer.maybe_trigger_maintenance`)の
テスト。

DynamoDBの条件付き更新の意味論自体は test_watchlist_batch_tracker.py で
moto検証済みのため、ここでは`try_acquire_maintenance_trigger`/
`mark_maintenance_triggered`/`boto3.client("lambda").invoke`をモック化し、
呼び出し順序・ガード条件(final_status含む)・invoke失敗時の挙動・戻り値
(MaintenanceTriggerOutcome)のみを検証する。

再レビュー修正(High): maybe_trigger_maintenance()は`_finish_batch()`へ
到達したかどうかではなく、明示的な`final_status`引数で起動可否を判定する
(COMPLETED/COMPLETED_WITH_NOTIFICATION_FAILUREのみ起動対象、ABORTEDを
含むそれ以外は起動しない)。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.infrastructure.aws.batch_tracker import (
    CandidateProgressRecord,
    WatchlistBatchStatus,
)
from jstock_advisor.services import watchlist_batch_finalizer as finalizer
from jstock_advisor.services.watchlist_batch_finalizer import MaintenanceTriggerOutcome

_NOW = dt.datetime(2026, 8, 17, 6, 30, tzinfo=dt.UTC)


def _fake_config(*, auto_removal_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        watchlist_screening=SimpleNamespace(
            auto_removal=SimpleNamespace(enabled=auto_removal_enabled)
        )
    )


def _record(stock_code: str, evaluation_result: str | None) -> CandidateProgressRecord:
    return CandidateProgressRecord(
        batch_id="batch-1",
        stock_code=stock_code,
        status="COMPLETED",
        dispatched=True,
        evaluation_result=evaluation_result,
        ranking_entry=None,
        lease_owner_id=None,
        attempt_count=1,
        total_processing_duration_ms=100,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        total_score=None,
        notification_detail=None,
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


# --- テストA: COMPLETED→1回起動 -------------------------------------------


def test_a_completed_triggers_maintenance_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """final_status=COMPLETEDの場合、1回だけ起動されTRIGGEREDを返すこと。"""
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
    monkeypatch.setattr(finalizer, "mark_maintenance_triggered", lambda *a: marked.append(a))

    batch_item = {"job_type": "NEW_CANDIDATE_SCREENING"}
    outcome = finalizer.maybe_trigger_maintenance(
        "batch-1", batch_item, _NOW, _fake_config(), WatchlistBatchStatus.COMPLETED
    )

    assert outcome is MaintenanceTriggerOutcome.TRIGGERED
    assert len(acquire_calls) == 1
    assert acquire_calls[0][0] == "batch-1"
    assert acquire_calls[0][1] == "watchlist-maint-batch-1"
    assert len(fake_client.invoke_calls) == 1
    payload = fake_client.invoke_calls[0]
    assert payload["InvocationType"] == "Event"
    assert payload["FunctionName"] == "jstock-advisor-watchlist-dispatcher"
    assert marked == [("batch-1", _NOW)]


# --- テストB: COMPLETED_WITH_NOTIFICATION_FAILURE→1回起動 -------------------


def test_b_completed_with_notification_failure_triggers_maintenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通知のみ失敗しウォッチリスト追加自体は正常確定したケースでも起動すること
    (評価・ranking・追加/スキップ・Audit・rotationは正常確定済みのため)。"""
    _patch_lambda_running(monkeypatch)
    monkeypatch.setattr(finalizer, "try_acquire_maintenance_trigger", lambda *a, **kw: True)
    fake_client = _FakeLambdaClient()
    monkeypatch.setattr(finalizer.boto3, "client", lambda *_a, **_kw: fake_client)
    monkeypatch.setattr(finalizer, "mark_maintenance_triggered", lambda *a: None)

    outcome = finalizer.maybe_trigger_maintenance(
        "batch-1",
        {"job_type": "NEW_CANDIDATE_SCREENING"},
        _NOW,
        _fake_config(),
        WatchlistBatchStatus.COMPLETED_WITH_NOTIFICATION_FAILURE,
    )

    assert outcome is MaintenanceTriggerOutcome.TRIGGERED
    assert len(fake_client.invoke_calls) == 1


# --- テストC: ABORTED→起動しない(High修正の本体) ---------------------------


def test_c_aborted_does_not_trigger_maintenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """429率・スコア項目欠損率等の閾値超過によるABORTEDでは、データ品質が
    信頼できないため後続のmaintenance(自動削除判定)を起動しないこと。"""
    _patch_lambda_running(monkeypatch)

    def _fail_if_called(*_a: Any, **_kw: Any) -> bool:
        pytest.fail("try_acquire_maintenance_trigger should not be called for ABORTED")

    monkeypatch.setattr(finalizer, "try_acquire_maintenance_trigger", _fail_if_called)

    outcome = finalizer.maybe_trigger_maintenance(
        "batch-1",
        {"job_type": "NEW_CANDIDATE_SCREENING"},
        _NOW,
        _fake_config(),
        WatchlistBatchStatus.ABORTED,
    )

    assert outcome is MaintenanceTriggerOutcome.NOT_APPLICABLE


# --- テストD/E/F: DISPATCH_FAILED/TIMED_OUT/FINALIZE_FAILED→起動しない ------


@pytest.mark.parametrize(
    "final_status",
    [
        WatchlistBatchStatus.DISPATCH_FAILED,
        WatchlistBatchStatus.TIMED_OUT,
        WatchlistBatchStatus.FINALIZE_FAILED,
    ],
)
def test_def_technical_failure_statuses_do_not_trigger_maintenance(
    monkeypatch: pytest.MonkeyPatch, final_status: WatchlistBatchStatus
) -> None:
    """DISPATCH_FAILED/TIMED_OUT/FINALIZE_FAILEDはいずれも`_finish_batch()`へ
    構造的に到達しないが、`maybe_trigger_maintenance()`単体のガードとしても
    直接これらのfinal_statusを渡した場合に起動しないことを防御的に確認する。"""
    _patch_lambda_running(monkeypatch)

    def _fail_if_called(*_a: Any, **_kw: Any) -> bool:
        pytest.fail(f"try_acquire_maintenance_trigger should not be called for {final_status}")

    monkeypatch.setattr(finalizer, "try_acquire_maintenance_trigger", _fail_if_called)

    outcome = finalizer.maybe_trigger_maintenance(
        "batch-1",
        {"job_type": "NEW_CANDIDATE_SCREENING"},
        _NOW,
        _fake_config(),
        final_status,
    )

    assert outcome is MaintenanceTriggerOutcome.NOT_APPLICABLE


# --- テストG: 個別candidateの失敗があってもbatch全体COMPLETEDなら起動 --------


def test_g_individual_candidate_failures_do_not_block_trigger_when_batch_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILED_REQUIRED/FAILED_NO_TARGET_TYPE/NOT_FOUNDのcandidateを含んでいても、
    execution_result=NORMAL(→final_status=COMPLETED)であればmaintenanceを
    起動すること。`_finish_batch()`を経由し、個別candidateの結果一覧が
    起動可否の判定に一切影響しないことを実際の呼び出し経路で確認する。"""
    monkeypatch.setattr(finalizer, "record_batch_audit", lambda **kw: None)
    monkeypatch.setattr(finalizer, "mark_batch_audit_recorded", lambda *a: None)
    monkeypatch.setattr(finalizer, "mark_watchlist_batch_completed", lambda *a, **kw: None)
    monkeypatch.setattr(finalizer, "_maybe_commit_rotation", lambda *a: None)
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        finalizer, "maybe_trigger_maintenance", lambda *a: calls.append(a) or None
    )

    records = [
        _record("1111", "FAILED_REQUIRED"),
        _record("2222", "FAILED_NO_TARGET_TYPE"),
        _record("3333", "NOT_FOUND"),
        _record("4444", "PASSED"),
    ]

    finalizer._finish_batch(
        "batch-1",
        _NOW,
        _NOW,
        {"job_type": "NEW_CANDIDATE_SCREENING", "finalize_batch_audit_recorded": True},
        records,
        {},
        _fake_config(),
        "NORMAL",
        [],
        [],
        False,
        False,
    )

    assert len(calls) == 1
    assert calls[0][0] == "batch-1"
    assert calls[0][4] is WatchlistBatchStatus.COMPLETED


# --- 補足: lease未取得・invoke失敗・ローカル実行時の戻り値 -------------------


def test_invoke_payload_contains_trigger_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """テスト#9: triggered_by_batch_id/trigger_typeがchild batchへ正しく
    引き継がれること(invoke payload経由で伝播することを確認)。"""
    _patch_lambda_running(monkeypatch)
    monkeypatch.setattr(finalizer, "try_acquire_maintenance_trigger", lambda *a, **kw: True)
    fake_client = _FakeLambdaClient()
    monkeypatch.setattr(finalizer.boto3, "client", lambda *_a, **_kw: fake_client)
    monkeypatch.setattr(finalizer, "mark_maintenance_triggered", lambda *a: None)

    finalizer.maybe_trigger_maintenance(
        "batch-1",
        {"job_type": "NEW_CANDIDATE_SCREENING"},
        _NOW,
        _fake_config(),
        WatchlistBatchStatus.COMPLETED,
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

    outcome = finalizer.maybe_trigger_maintenance(
        "watchlist-maint-1",
        {"job_type": "WATCHLIST_MAINTENANCE"},
        _NOW,
        _fake_config(),
        WatchlistBatchStatus.COMPLETED,
    )

    assert outcome is MaintenanceTriggerOutcome.NOT_APPLICABLE


def test_skips_when_auto_removal_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail_if_called(*_a: Any, **_kw: Any) -> bool:
        pytest.fail("try_acquire_maintenance_trigger should not be called when disabled")

    monkeypatch.setattr(finalizer, "try_acquire_maintenance_trigger", _fail_if_called)

    outcome = finalizer.maybe_trigger_maintenance(
        "batch-1",
        {"job_type": "NEW_CANDIDATE_SCREENING"},
        _NOW,
        _fake_config(auto_removal_enabled=False),
        WatchlistBatchStatus.COMPLETED,
    )

    assert outcome is MaintenanceTriggerOutcome.NOT_APPLICABLE


def test_skips_when_lease_not_acquired(monkeypatch: pytest.MonkeyPatch) -> None:
    """テスト#5相当: 既にTRIGGERED、または他の主体がlease保持中の場合はinvokeしない
    (exactly-once)。"""
    _patch_lambda_running(monkeypatch)
    monkeypatch.setattr(finalizer, "try_acquire_maintenance_trigger", lambda *a, **kw: False)

    def _fail_if_called(*_a: Any, **_kw: Any) -> Any:
        pytest.fail("lambda client should not be constructed when lease is not acquired")

    monkeypatch.setattr(finalizer.boto3, "client", _fail_if_called)

    outcome = finalizer.maybe_trigger_maintenance(
        "batch-1",
        {"job_type": "NEW_CANDIDATE_SCREENING"},
        _NOW,
        _fake_config(),
        WatchlistBatchStatus.COMPLETED,
    )

    assert outcome is MaintenanceTriggerOutcome.SKIPPED_LEASE_UNAVAILABLE


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

    outcome = finalizer.maybe_trigger_maintenance(
        "batch-1",
        {"job_type": "NEW_CANDIDATE_SCREENING"},
        _NOW,
        _fake_config(),
        WatchlistBatchStatus.COMPLETED,
    )

    assert outcome is MaintenanceTriggerOutcome.INVOKE_FAILED
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

    outcome = finalizer.maybe_trigger_maintenance(
        "batch-1",
        {"job_type": "NEW_CANDIDATE_SCREENING"},
        _NOW,
        _fake_config(),
        WatchlistBatchStatus.COMPLETED,
    )

    assert outcome is MaintenanceTriggerOutcome.SKIPPED_LOCAL_EXECUTION
    assert marked == []


def test_finish_batch_passes_resolved_final_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """_finish_batch()(=業務finalize確定のchoke point)が、execution_result/
    notification_permanently_failedから計算した確定済みfinal_statusを
    maybe_trigger_maintenanceへ渡すこと(古いbatch_itemのstatusは参照しない)。"""
    monkeypatch.setattr(finalizer, "record_batch_audit", lambda **kw: None)
    monkeypatch.setattr(finalizer, "mark_batch_audit_recorded", lambda *a: None)
    monkeypatch.setattr(finalizer, "mark_watchlist_batch_completed", lambda *a, **kw: None)
    monkeypatch.setattr(finalizer, "_maybe_commit_rotation", lambda *a: None)
    triggered: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        finalizer, "maybe_trigger_maintenance", lambda *a: triggered.append(a)
    )

    # batch_item自体は古い(FINALIZE_PREPARING時点の)statusを持っていても、
    # final_statusはexecution_result="NORMAL"から正しくCOMPLETEDへ計算される
    # ことを確認する(finalize前に取得した古いbatch_itemのstatusは無視する)。
    stale_batch_item = {
        "job_type": "NEW_CANDIDATE_SCREENING",
        "finalize_batch_audit_recorded": True,
        "status": "FINALIZE_PREPARING",
    }

    finalizer._finish_batch(
        "batch-1",
        _NOW,
        _NOW,
        stale_batch_item,
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
    assert triggered[0][4] is WatchlistBatchStatus.COMPLETED


def test_finish_batch_resolves_notification_permanently_failed_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """execution_result=NORMAL かつ notification_permanently_failed=True の場合、
    final_statusはCOMPLETED_WITH_NOTIFICATION_FAILUREとして渡されること。"""
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
        True,
    )

    assert triggered[0][4] is WatchlistBatchStatus.COMPLETED_WITH_NOTIFICATION_FAILURE


def test_finish_batch_resolves_aborted_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """execution_result != NORMAL(abort理由)の場合、final_statusはABORTEDとして
    渡され、実際にmaybe_trigger_maintenance側で起動が抑止されること
    (High修正: この2段の組み合わせで初めてABORTED非起動が保証される)。"""
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
        "HIGH_THROTTLE_RATE",
        ["HIGH_THROTTLE_RATE"],
        [],
        False,
        False,
    )

    assert triggered[0][4] is WatchlistBatchStatus.ABORTED
