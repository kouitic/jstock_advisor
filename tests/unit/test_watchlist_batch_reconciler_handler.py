"""運用ハードニング5節: Reconcilerによる FINALIZING/FINALIZE_FAILED の自動復旧、
および複数回実行時の冪等性のテスト。

build_real_provider_bundle/build_cached_provider_bundle/LineNotificationServiceの
実際の構築(ネットワーク・ローカルJSONストア書き込み)は避け、フェイクへ差し替える。
watchlist_batch_finalizer側の record_batch_audit も同様にフェイク化する
(AuditServiceの実書き込みを避けるための、このリポジトリの既存テストの慣例に合わせる)。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.infrastructure.aws.batch_tracker import (
    WatchlistBatchStatus,
    WatchlistProgressStatus,
)
from jstock_advisor.lambda_handlers import watchlist_batch_reconciler_handler as handler_module
from jstock_advisor.services import watchlist_batch_finalizer as finalizer_module

_REGION = "ap-northeast-1"
_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)
_BATCH_TABLE = "jstock-batch_runs"
_PROGRESS_TABLE = "jstock-watchlist_candidate_progress"


@pytest.fixture
def dynamo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_BATCH_TABLE,
            KeySchema=[{"AttributeName": "batch_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "batch_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName=_PROGRESS_TABLE,
            KeySchema=[
                {"AttributeName": "batch_id", "KeyType": "HASH"},
                {"AttributeName": "stock_code", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "batch_id", "AttributeType": "S"},
                {"AttributeName": "stock_code", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def _fake_config(*, max_finalize_retry_attempts: int = 3) -> SimpleNamespace:
    watchlist_screening = SimpleNamespace(
        candidate_universe=SimpleNamespace(provider="jpx"),
        screening_policy="high_dividend_financial_health",
        max_watchlist_additions_per_run=20,
        notification_enabled=True,
        high_throttle_rate_threshold_pct=20.0,
        max_field_missing_rate_pct=30.0,
        batch_processing_timeout_hours=24,
        finalizing_stuck_threshold_minutes=15,
        max_finalize_retry_attempts=max_finalize_retry_attempts,
        max_timeout_finalize_rows_per_run=500,
    )
    return SimpleNamespace(watchlist_screening=watchlist_screening)


class _FakeWatchlistRepository:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add_if_new(self, item: Any) -> bool:
        if any(existing.stock_code == item.stock_code for existing in self.added):
            return False
        self.added.append(item)
        return True


class _FakeNotificationService:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def notify_watchlist_additions(self, added_items, results_by_code, policy_name, now):
        self.calls.append(added_items)
        return True


@pytest.fixture(autouse=True)
def _stub_expensive_dependencies(monkeypatch: pytest.MonkeyPatch) -> _FakeWatchlistRepository:
    monkeypatch.setattr(handler_module, "load_config", lambda: _fake_config())
    monkeypatch.setattr(handler_module, "build_real_provider_bundle", lambda now, config: object())
    monkeypatch.setattr(
        handler_module, "build_cached_provider_bundle", lambda base, config, now: base
    )
    fake_notification = _FakeNotificationService()
    monkeypatch.setattr(
        handler_module, "_build_notification_service", lambda config: fake_notification
    )
    monkeypatch.setattr(handler_module, "record_batch_audit", lambda **kw: None)
    monkeypatch.setattr(finalizer_module, "record_batch_audit", lambda **kw: None)
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    return fake_repo


def _drive_batch_to_finalizing(batch_id: str, now: dt.datetime) -> None:
    batch_tracker.try_acquire_dispatch_lease(batch_id, "dispatcher", now, 360, 72)
    batch_tracker.set_watchlist_batch_total(batch_id, 1, 72, now)
    batch_tracker.create_missing_candidate_progress_rows(batch_id, ["1111"], now, 72)
    batch_tracker.mark_dispatch_completed(batch_id, now)
    batch_tracker.claim_candidate_lease(batch_id, "1111", "owner-a", now, 240)
    batch_tracker.complete_candidate(
        batch_id,
        "1111",
        "owner-a",
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result="DATA_INSUFFICIENT",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=now,
    )
    assert batch_tracker.try_finalize_if_ready(batch_id, now) is True


def test_reconciler_marks_stuck_finalizing_as_failed(dynamo) -> None:
    # handler()自体は内部でdt.datetime.now(dt.UTC)(実時刻)を使うため、テストの
    # 固定時刻(_NOW)へ依存せず確実に15分の閾値を超過させるよう、finalizing_started_at
    # を十分過去(2000年)に設定する。
    far_past = dt.datetime(2000, 1, 1, tzinfo=dt.UTC)
    _drive_batch_to_finalizing("batch-1", far_past)  # finalizing_started_at = far_past

    result = handler_module.handler({}, object())

    assert result["finalizing_marked_stuck"] == 1
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.FINALIZE_FAILED.value


def test_reconciler_retries_finalize_failed_under_attempt_cap(
    dynamo, _stub_expensive_dependencies: _FakeWatchlistRepository
) -> None:
    _drive_batch_to_finalizing("batch-1", _NOW)
    batch_tracker.mark_watchlist_finalize_failed("batch-1", _NOW, "simulated failure")
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert int(batch["finalize_attempt_count"]) == 1

    result = handler_module.handler({}, object())

    assert result["finalize_retried"] == 1
    assert result["finalize_retry_exhausted"] == 0
    batch_after = batch_tracker.get_watchlist_batch("batch-1")
    assert batch_after is not None
    assert batch_after["status"] == WatchlistBatchStatus.COMPLETED.value


def test_reconciler_does_not_retry_finalize_failed_once_attempts_exhausted(dynamo) -> None:
    _drive_batch_to_finalizing("batch-1", _NOW)
    for attempt in range(3):
        batch_tracker.mark_watchlist_finalize_failed("batch-1", _NOW, f"failure-{attempt}")
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert int(batch["finalize_attempt_count"]) == 3  # max_finalize_retry_attempts=3

    result = handler_module.handler({}, object())

    assert result["finalize_retried"] == 0
    assert result["finalize_retry_exhausted"] == 1
    batch_after = batch_tracker.get_watchlist_batch("batch-1")
    assert batch_after is not None
    # 上限に達したバッチは自動再試行されず、FINALIZE_FAILEDのまま(手動対応待ち)。
    assert batch_after["status"] == WatchlistBatchStatus.FINALIZE_FAILED.value


def test_reconciler_processes_same_batch_twice_without_duplicate_side_effects(
    dynamo, _stub_expensive_dependencies: _FakeWatchlistRepository
) -> None:
    """同一イベント(または同じ状態のバッチ)に対してReconcilerが2回実行されても、
    ウォッチリスト追加・LINE通知が重複しないこと(状態遷移により2回目は対象外になる)。"""
    _drive_batch_to_finalizing("batch-1", _NOW)
    batch_tracker.mark_watchlist_finalize_failed("batch-1", _NOW, "simulated failure")

    first = handler_module.handler({}, object())
    second = handler_module.handler({}, object())

    assert first["finalize_retried"] == 1
    # 1回目でCOMPLETEDへ遷移済みのため、2回目はReconcilerの対象状態リストに
    # 含まれず候補にすら挙がらない(冪等)。
    assert second["candidates"] == 0
    assert second["finalize_retried"] == 0

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.COMPLETED.value
