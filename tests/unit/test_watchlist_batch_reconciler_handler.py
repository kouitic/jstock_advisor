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

from jstock_advisor.domain.entities.enums import ExecutionMode as _B2ExecutionMode
from jstock_advisor.domain.entities.execution_context import (
    ExecutionContext as _B2ExecutionContext,
)
from jstock_advisor.domain.signals.watchlist_screening import RankingEntry
from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.infrastructure.aws.batch_tracker import (
    BatchFamily as _B2BatchFamily,
)
from jstock_advisor.infrastructure.aws.batch_tracker import (
    BatchProgress as _B2BatchProgress,
)
from jstock_advisor.infrastructure.aws.batch_tracker import (
    CompletionBatchRecord as _B2Record,
)
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


def _fake_scoring_config() -> SimpleNamespace:
    return SimpleNamespace(
        minimum_total_score=60.0,
        dividend_yield=SimpleNamespace(weight=30.0, zero_at_pct=3.5, full_at_pct=6.0),
        equity_ratio=SimpleNamespace(weight=25.0, zero_at_pct=40.0, full_at_pct=70.0),
        payout_ratio=SimpleNamespace(weight=15.0, healthy_min_pct=20.0, healthy_max_pct=60.0),
        dividend_growth=SimpleNamespace(weight=15.0, zero_at_years=0, full_at_years=10),
        shareholder_benefit=SimpleNamespace(
            weight=15.0, yield_full_at_pct=2.0, presence_only_score_ratio=0.5
        ),
    )


def _fake_thresholds_config() -> SimpleNamespace:
    return SimpleNamespace(
        minimum_market_cap_yen=50_000_000_000,
        require_positive_operating_cash_flow=True,
        exclude_dividend_cut_announced=True,
        exclude_debt_excess=True,
        exclude_deficit=True,
        exclude_going_concern_doubt=True,
        exclude_etf=True,
        exclude_reit=True,
    )


def _fake_config(
    *, max_finalize_retry_attempts: int = 3, max_notification_retry_attempts: int = 3
) -> SimpleNamespace:
    watchlist_screening = SimpleNamespace(
        candidate_universe=SimpleNamespace(provider="jpx"),
        screening_policy="high_dividend_financial_health",
        max_watchlist_additions_per_run=20,
        notification_enabled=True,
        high_throttle_rate_threshold_pct=20.0,
        max_scoring_field_missing_rate_pct=30.0,
        max_data_error_rate_pct=100.0,
        max_not_found_rate_pct=100.0,
        max_terminal_failure_rate_pct=100.0,
        max_required_field_missing_rate_pct=100.0,
        batch_processing_timeout_hours=24,
        finalizing_stuck_threshold_minutes=15,
        max_finalize_retry_attempts=max_finalize_retry_attempts,
        max_notification_retry_attempts=max_notification_retry_attempts,
        max_timeout_finalize_rows_per_run=500,
        scoring=_fake_scoring_config(),
        thresholds=_fake_thresholds_config(),
        stock_display_name=SimpleNamespace(jpx_name_negative_cache_ttl_seconds=60),
        auto_removal=SimpleNamespace(
            enabled=True,
            readd_cooldown_days=30,
            minimum_age_days=90,
            consecutive_not_qualified_required=3,
            minimum_not_qualified_span_days=28,
            stale_recheck_days=30,
            maximum_unconfirmed_days=180,
        ),
    )
    return SimpleNamespace(watchlist_screening=watchlist_screening)


class _FakeStockDisplayNameResolver:
    """テストではJPX/override/既存Watchlistの実I/Oを避け、fallbackのみで
    解決する(このリポジトリの既存テストの慣例に合わせる)。"""

    def resolve(self, stock_code, fallback_name=None, fallback_name_provider=None):  # noqa: ANN001, ANN201
        if fallback_name:
            return fallback_name
        if fallback_name_provider is not None:
            provided = fallback_name_provider()
            if provided:
                return provided
        return stock_code


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
        self.fail_next = False

    def notify_watchlist_additions(self, summary, content_hash):  # noqa: ANN001, ANN201
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("LINE push failed (simulated)")
        self.calls.append(list(summary.items))
        return True


@pytest.fixture(autouse=True)
def _stub_expensive_dependencies(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
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
    monkeypatch.setattr(
        finalizer_module,
        "build_stock_display_name_resolver",
        lambda *_a, **_kw: _FakeStockDisplayNameResolver(),
    )
    return SimpleNamespace(repo=fake_repo, notification=fake_notification)


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
        evaluation_result="DATA_ERROR",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=now,
    )
    assert batch_tracker.try_finalize_if_ready(batch_id, now) is True


def _make_ranking_entry(stock_code: str) -> str:
    return RankingEntry(
        stock_code=stock_code,
        total_score=80.0,
        policy_scores={"high_dividend_financial_health": 80.0},
        matched_criteria=[],
        main_metrics={},
    ).model_dump_json()


def _drive_batch_to_running_with_passed_candidate(batch_id: str, now: dt.datetime) -> None:
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
        evaluation_result="PASSED",
        ranking_entry=_make_ranking_entry("1111"),
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=now,
        total_score=80.0,
    )


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
    dynamo, _stub_expensive_dependencies: SimpleNamespace
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
    dynamo, _stub_expensive_dependencies: SimpleNamespace
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


# --- 本番検証2026-08対応: DISPATCH_FAILED/TIMED_OUT時のrotation dispatch lease解放 ---


def test_reconciler_releases_rotation_lease_when_dispatching_times_out(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DISPATCHINGのままタイムアウトしたバッチをDISPATCH_FAILEDへ遷移させる際、
    rotation dispatch leaseを明示的に解放すること(_finish_batch()のfinalize
    経路に一切到達しないため、ここで解放しないと次回dispatchがlease_expires_at
    の自然失効までブロックされ続ける)。"""
    far_past = dt.datetime(2000, 1, 1, tzinfo=dt.UTC)
    batch_tracker.try_acquire_dispatch_lease("batch-stuck", "dispatcher", far_past, 360, 72)
    batch = batch_tracker.get_watchlist_batch("batch-stuck")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.DISPATCHING.value

    release_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        handler_module,
        "release_rotation_dispatch_lease",
        lambda rotation_id, batch_id: release_calls.append((rotation_id, batch_id)),
    )

    result = handler_module.handler({}, object())

    assert result["dispatch_failed"] == 1
    assert release_calls == [("default", "batch-stuck")]
    batch_after = batch_tracker.get_watchlist_batch("batch-stuck")
    assert batch_after is not None
    assert batch_after["status"] == WatchlistBatchStatus.DISPATCH_FAILED.value


def test_process_timeout_finalizing_releases_rotation_lease_on_timed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TIMED_OUTは_finish_batch()/_maybe_commit_rotation()のfinalize経路を
    使わないため(モジュールdocstring参照)、_process_timeout_finalizing自身が
    transition_timeout_finalizing_to_timed_out直後にrotation dispatch leaseを
    明示的に解放すること。17節の実際のタイムアウト再計算ロジック(FAILED確定・
    件数照合)は別途batch_tracker側で検証済みのためここではモックし、今回追加した
    解放呼び出しの発生順序のみを検証する。"""
    fake_result = batch_tracker.TimeoutFinalizationPassResult(
        all_records=[], terminal_count=1, total=1, newly_failed_count=1
    )
    monkeypatch.setattr(
        handler_module, "run_timeout_finalization_pass", lambda *a, **kw: fake_result
    )
    monkeypatch.setattr(
        handler_module, "set_timeout_finalize_completed_count", lambda *a, **kw: True
    )
    monkeypatch.setattr(
        handler_module,
        "get_watchlist_batch",
        lambda batch_id: {"started_at": _NOW.isoformat()},
    )
    monkeypatch.setattr(
        handler_module,
        "compute_batch_metrics",
        lambda records: {"processed_count": 1},
    )
    monkeypatch.setattr(handler_module, "record_batch_audit", lambda **kw: None)

    call_order: list[str] = []
    monkeypatch.setattr(
        handler_module,
        "transition_timeout_finalizing_to_timed_out",
        lambda batch_id, now: call_order.append("transition"),
    )
    monkeypatch.setattr(
        handler_module,
        "release_rotation_dispatch_lease",
        lambda rotation_id, batch_id: call_order.append(f"release:{rotation_id}:{batch_id}"),
    )

    handler_module._process_timeout_finalizing("batch-timed-out", _NOW, 500, _fake_config())

    assert call_order == ["transition", "release:default:batch-timed-out"]


def test_reconciler_retries_notification_failed_without_rewriting_watchlist(
    dynamo, _stub_expensive_dependencies: SimpleNamespace
) -> None:
    """運用ハードニング第3弾1節: NOTIFICATION_FAILED状態のバッチに対し、
    Reconcilerが通知のみを再試行し、ウォッチリスト追加(add_if_new)が
    再実行されないこと。"""
    fake_notification = _stub_expensive_dependencies.notification
    fake_repo = _stub_expensive_dependencies.repo
    _drive_batch_to_running_with_passed_candidate("batch-1", _NOW)

    # 1回目: RUNNING→finalize実行、LINE送信が例外になりNOTIFICATION_FAILEDへ。
    fake_notification.fail_next = True
    first = handler_module.handler({}, object())
    assert first["rescued"] == 1
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.NOTIFICATION_FAILED.value
    assert len(fake_repo.added) == 1

    # 2回目: NOTIFICATION_FAILED→通知のみ再試行(fail_nextは既に消費済みのため成功)。
    second = handler_module.handler({}, object())
    assert second["notification_retried"] == 1
    assert len(fake_notification.calls) == 1
    # ウォッチリスト書込みは再実行されない。
    assert len(fake_repo.added) == 1

    batch_after = batch_tracker.get_watchlist_batch("batch-1")
    assert batch_after is not None
    assert batch_after["status"] == WatchlistBatchStatus.COMPLETED.value


# --- 平日毎日起動化(2026-08)対応: WATCHLIST_MAINTENANCE後続起動のstale retry ------


def _mark_batch_completed(batch_id: str) -> None:
    """再試行対象の親バッチについて、実運用と同じ順序(status確定→
    maintenance_trigger_status付与)を再現する。`maybe_trigger_maintenance`が
    `_finish_batch`到達時点で計算済みのfinal_statusを渡す設計になったため、
    Reconciler側は毎回フルスキャンで取得した`batch_item["status"]`から
    final_statusを復元する(list_stale_maintenance_triggers参照)。"""
    batch_tracker.mark_watchlist_batch_completed(
        batch_id, batch_tracker.EXECUTION_RESULT_NORMAL, _NOW
    )


def test_reconciler_retries_stale_maintenance_trigger(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """テスト#6相当: invoke失敗等でTRIGGERINGのままlease失効した親バッチを、
    毎時Reconcilerがmaybe_trigger_maintenance経由で再試行すること。
    Medium修正(2026-08再レビュー): 戻り値TRIGGEREDのみがmaintenance_trigger_
    retriedへ計上されることも合わせて確認する。"""
    _mark_batch_completed("batch-1")
    batch_tracker.try_acquire_maintenance_trigger(
        "batch-1", "watchlist-maint-batch-1", "owner-a", _NOW, lease_seconds=60
    )
    much_later = _NOW + dt.timedelta(hours=2)

    retried: list[str] = []

    def _fake_maybe_trigger_maintenance(batch_id, batch_item, now, config, final_status):  # noqa: ANN001, ANN201
        retried.append(batch_id)
        assert final_status is WatchlistBatchStatus.COMPLETED
        return finalizer_module.MaintenanceTriggerOutcome.TRIGGERED

    monkeypatch.setattr(
        handler_module, "maybe_trigger_maintenance", _fake_maybe_trigger_maintenance
    )
    # handler_module内の`dt`名(dispatcher/reconcilerが共有するdatetimeモジュールへの
    # エイリアス)だけを差し替える。dt.datetime自体(実際のstdlibクラス)を書き換えると
    # 同一プロセス内の他コードにも影響してしまうため、名前解決の付け替えのみを行う
    # (グローバルなdatetime moduleの改変は避ける)。
    monkeypatch.setattr(
        handler_module,
        "dt",
        SimpleNamespace(datetime=SimpleNamespace(now=lambda tz=None: much_later), UTC=dt.UTC),
    )

    result = handler_module.handler({}, object())

    assert retried == ["batch-1"]
    assert result["maintenance_trigger_retried"] == 1
    assert result["maintenance_trigger_retry_failed"] == 0
    assert result["maintenance_trigger_retry_skipped"] == 0
    assert result["maintenance_trigger_retry_configuration_error"] == 0


def test_reconciler_does_not_count_retried_when_lease_already_taken(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Medium修正(2026-08再レビュー)の本体: stale一覧取得後に他の主体が先に
    trigger leaseを再取得しmaybe_trigger_maintenance()が何もせずreturnした
    場合(SKIPPED_LEASE_UNAVAILABLE)、maintenance_trigger_retriedを誤って
    加算しないこと(retry_skippedへ計上すること)。"""
    _mark_batch_completed("batch-1")
    batch_tracker.try_acquire_maintenance_trigger(
        "batch-1", "watchlist-maint-batch-1", "owner-a", _NOW, lease_seconds=60
    )
    much_later = _NOW + dt.timedelta(hours=2)

    def _fake_maybe_trigger_maintenance(batch_id, batch_item, now, config, final_status):  # noqa: ANN001, ANN201
        return finalizer_module.MaintenanceTriggerOutcome.SKIPPED_LEASE_UNAVAILABLE

    monkeypatch.setattr(
        handler_module, "maybe_trigger_maintenance", _fake_maybe_trigger_maintenance
    )
    monkeypatch.setattr(
        handler_module,
        "dt",
        SimpleNamespace(datetime=SimpleNamespace(now=lambda tz=None: much_later), UTC=dt.UTC),
    )

    result = handler_module.handler({}, object())

    assert result["maintenance_trigger_retried"] == 0
    assert result["maintenance_trigger_retry_failed"] == 0
    assert result["maintenance_trigger_retry_skipped"] == 1
    assert result["maintenance_trigger_retry_configuration_error"] == 0


def test_reconciler_counts_invoke_failure_as_retried_and_retry_failed(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """leaseの再取得には成功したがinvoke自体が再び失敗したケース
    (INVOKE_FAILED)は「実際に再試行を試みた」ため retried へ計上しつつ、
    retry_failed でも区別できること。"""
    _mark_batch_completed("batch-1")
    batch_tracker.try_acquire_maintenance_trigger(
        "batch-1", "watchlist-maint-batch-1", "owner-a", _NOW, lease_seconds=60
    )
    much_later = _NOW + dt.timedelta(hours=2)

    def _fake_maybe_trigger_maintenance(batch_id, batch_item, now, config, final_status):  # noqa: ANN001, ANN201
        return finalizer_module.MaintenanceTriggerOutcome.INVOKE_FAILED

    monkeypatch.setattr(
        handler_module, "maybe_trigger_maintenance", _fake_maybe_trigger_maintenance
    )
    monkeypatch.setattr(
        handler_module,
        "dt",
        SimpleNamespace(datetime=SimpleNamespace(now=lambda tz=None: much_later), UTC=dt.UTC),
    )

    result = handler_module.handler({}, object())

    assert result["maintenance_trigger_retried"] == 1
    assert result["maintenance_trigger_retry_failed"] == 1
    assert result["maintenance_trigger_retry_skipped"] == 0
    assert result["maintenance_trigger_retry_configuration_error"] == 0


def test_reconciler_counts_configuration_error_separately_from_retried(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """再々レビュー修正(Medium、2026-08): leaseの再取得には成功したが、
    起動先関数名の環境変数未設定(CONFIGURATION_ERROR)によりLambda invoke()
    自体を呼ぶ前に終了したケースは、「実際にinvokeを試行した」わけではない
    ため maintenance_trigger_retried へ含めず、専用の
    maintenance_trigger_retry_configuration_error へのみ計上すること。"""
    _mark_batch_completed("batch-1")
    batch_tracker.try_acquire_maintenance_trigger(
        "batch-1", "watchlist-maint-batch-1", "owner-a", _NOW, lease_seconds=60
    )
    much_later = _NOW + dt.timedelta(hours=2)

    def _fake_maybe_trigger_maintenance(batch_id, batch_item, now, config, final_status):  # noqa: ANN001, ANN201
        return finalizer_module.MaintenanceTriggerOutcome.CONFIGURATION_ERROR

    monkeypatch.setattr(
        handler_module, "maybe_trigger_maintenance", _fake_maybe_trigger_maintenance
    )
    monkeypatch.setattr(
        handler_module,
        "dt",
        SimpleNamespace(datetime=SimpleNamespace(now=lambda tz=None: much_later), UTC=dt.UTC),
    )

    result = handler_module.handler({}, object())

    assert result["maintenance_trigger_retried"] == 0
    assert result["maintenance_trigger_retry_failed"] == 0
    assert result["maintenance_trigger_retry_skipped"] == 0
    assert result["maintenance_trigger_retry_configuration_error"] == 1


def test_reconciler_does_not_retry_maintenance_trigger_within_lease(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lease未失効(TRIGGERING中だがlease_expires_atが未来)のうちはReconcilerが
    再試行しないこと(invoke処理中の二重起動防止、テスト#6の裏側)。"""
    _mark_batch_completed("batch-1")
    batch_tracker.try_acquire_maintenance_trigger(
        "batch-1", "watchlist-maint-batch-1", "owner-a", _NOW, lease_seconds=3600
    )
    still_within_lease = _NOW + dt.timedelta(minutes=5)

    retried: list[str] = []

    def _fake_maybe_trigger_maintenance(batch_id, batch_item, now, config, final_status):  # noqa: ANN001, ANN201
        retried.append(batch_id)
        return finalizer_module.MaintenanceTriggerOutcome.TRIGGERED

    monkeypatch.setattr(
        handler_module, "maybe_trigger_maintenance", _fake_maybe_trigger_maintenance
    )
    monkeypatch.setattr(
        handler_module,
        "dt",
        SimpleNamespace(
            datetime=SimpleNamespace(now=lambda tz=None: still_within_lease), UTC=dt.UTC
        ),
    )

    result = handler_module.handler({}, object())

    assert retried == []
    assert result["maintenance_trigger_retried"] == 0


# --- Issue #57 Phase B2: buy/holdings finalize recovery の分岐隔離 ---------------
# reconcilerは`status`だけで全項目をscanするため、buy/holdingsの項目もここへ届く。
# family markerで積極識別し、**watchlistの既存status分岐へ流さない**ことを固定する。


_B2_NOW = dt.datetime(2026, 8, 31, 23, 0, tzinfo=dt.UTC)


def _b2_progress(total: int = 1, completed_codes: list[str] | None = None) -> _B2BatchProgress:
    codes = completed_codes if completed_codes is not None else ["7203"]
    return _B2BatchProgress(
        total=total,
        completed=len(codes),
        category_counts={},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[],
        sector_entries=[],
        holding_count=0,
        completed_codes=codes,
    )


def _b2_record(
    batch_id: str = "buy-1",
    family: _B2BatchFamily | None = _B2BatchFamily.BUY_CANDIDATES,
    context: _B2ExecutionContext | None = None,
    attempt_count: int = 0,
    completed_at: str | None = None,
    progress: _B2BatchProgress | None = None,
) -> _B2Record:
    return _B2Record(
        batch_id=batch_id,
        family=family,
        execution_context=(
            context if context is not None else _B2ExecutionContext.normal()
        ),
        progress=progress if progress is not None else _b2_progress(),
        attempt_count=attempt_count,
        finalize_started_at=None,
        finalize_completed_at=completed_at,
        finalize_failed_at=None,
    )


def _b2_patch(monkeypatch, record, invoked: list[tuple[str, dict]]):
    monkeypatch.setenv("BUY_CANDIDATES_FUNCTION_NAME", "fn-buy")
    monkeypatch.setenv("HOLDINGS_WATCHLIST_FUNCTION_NAME", "fn-holdings")
    monkeypatch.setattr(handler_module, "get_completion_batch", lambda batch_id: record)
    monkeypatch.setattr(
        handler_module,
        "dispatch_async",
        lambda name, payload: invoked.append((name, payload)),
    )


def test_b2_buy_batch_is_routed_to_finalize_recovery(monkeypatch) -> None:
    """T6: buyのバッチはwatchlist finalizerへ流れず、finalize-only invokeになる。"""
    invoked: list[tuple[str, dict]] = []
    _b2_patch(monkeypatch, _b2_record(), invoked)

    outcome = handler_module._handle_completion_recovery_candidate(
        {"batch_id": "buy-1", "status": "RUNNING", "batch_family": "BUY_CANDIDATES"},
        _B2_NOW,
    )

    assert outcome is True
    assert invoked == [
        (
            "fn-buy",
            {
                "recovery_action": "FINALIZE_ONLY",
                "batch_id": "buy-1",
                "batch_family": "BUY_CANDIDATES",
                "execution_mode": "NORMAL",
            },
        )
    ]


def test_b2_holdings_batch_is_routed_to_finalize_recovery(monkeypatch) -> None:
    """T7: holdingsのバッチも同様に隔離された経路へ入る。"""
    invoked: list[tuple[str, dict]] = []
    record = _b2_record(batch_id="hold-1", family=_B2BatchFamily.HOLDINGS_WATCHLIST)
    _b2_patch(monkeypatch, record, invoked)

    outcome = handler_module._handle_completion_recovery_candidate(
        {"batch_id": "hold-1", "status": "RUNNING", "batch_family": "HOLDINGS_WATCHLIST"},
        _B2_NOW,
    )

    assert outcome is True
    assert invoked[0][0] == "fn-holdings"
    assert invoked[0][1]["batch_family"] == "HOLDINGS_WATCHLIST"


def test_b2_marker_absent_falls_through_to_watchlist_path(monkeypatch) -> None:
    """T5/T27: 既存watchlist batchにはmarkerが無い。**Noneを返して既存経路を継続**する
    (marker不在を一律skipするとwatchlist recoveryを壊すため)。"""
    invoked: list[tuple[str, dict]] = []
    _b2_patch(monkeypatch, _b2_record(), invoked)

    outcome = handler_module._handle_completion_recovery_candidate(
        {"batch_id": "watchlist-1", "status": "RUNNING"}, _B2_NOW
    )

    assert outcome is None, "marker不在は既存watchlist経路へ継続しなければならない"
    assert invoked == []


def test_b2_unknown_family_is_fail_closed(monkeypatch) -> None:
    """T12: 未知のfamily値はfail-close。既存watchlist経路へも流さない。"""
    invoked: list[tuple[str, dict]] = []
    _b2_patch(monkeypatch, _b2_record(), invoked)

    outcome = handler_module._handle_completion_recovery_candidate(
        {"batch_id": "x-1", "status": "RUNNING", "batch_family": "SOMETHING_ELSE"}, _B2_NOW
    )

    assert outcome is False, "未知familyを既存経路へ流してはならない"
    assert invoked == []


def test_b2_validation_batch_is_not_invoked(monkeypatch) -> None:
    """T17: VALIDATIONバッチは自動re-driveしない。"""
    invoked: list[tuple[str, dict]] = []
    record = _b2_record(
        context=_B2ExecutionContext(mode=_B2ExecutionMode.VALIDATION)
    )
    _b2_patch(monkeypatch, record, invoked)

    outcome = handler_module._handle_completion_recovery_candidate(
        {"batch_id": "buy-1", "status": "RUNNING", "batch_family": "BUY_CANDIDATES"}, _B2_NOW
    )

    assert outcome is False
    assert invoked == []


def test_b2_unknown_context_is_not_invoked(monkeypatch) -> None:
    """実行文脈を復元できない項目はfail-close(NORMALと仮定しない)。"""
    invoked: list[tuple[str, dict]] = []
    _b2_patch(monkeypatch, _b2_record(context=None), invoked)
    # context=None を明示的に作る
    record = _b2_record()
    record = _B2Record(
        batch_id=record.batch_id,
        family=record.family,
        execution_context=None,
        progress=record.progress,
        attempt_count=0,
        finalize_started_at=None,
        finalize_completed_at=None,
        finalize_failed_at=None,
    )
    monkeypatch.setattr(handler_module, "get_completion_batch", lambda batch_id: record)

    outcome = handler_module._handle_completion_recovery_candidate(
        {"batch_id": "buy-1", "status": "RUNNING", "batch_family": "BUY_CANDIDATES"}, _B2_NOW
    )

    assert outcome is False
    assert invoked == []


def test_b2_already_finalized_batch_is_not_invoked(monkeypatch) -> None:
    """T24: 既にfinalize済みならinvokeしない。"""
    invoked: list[tuple[str, dict]] = []
    _b2_patch(monkeypatch, _b2_record(completed_at="2026-08-31T23:00:00+00:00"), invoked)

    outcome = handler_module._handle_completion_recovery_candidate(
        {"batch_id": "buy-1", "status": "RUNNING", "batch_family": "BUY_CANDIDATES"}, _B2_NOW
    )

    assert outcome is False
    assert invoked == []


def test_b2_incomplete_batch_is_not_invoked(monkeypatch) -> None:
    """全銘柄の処理が終わっていないバッチは通常進行中でありrecovery対象外。"""
    invoked: list[tuple[str, dict]] = []
    record = _b2_record(progress=_b2_progress(total=3, completed_codes=["7203"]))
    _b2_patch(monkeypatch, record, invoked)

    outcome = handler_module._handle_completion_recovery_candidate(
        {"batch_id": "buy-1", "status": "RUNNING", "batch_family": "BUY_CANDIDATES"}, _B2_NOW
    )

    assert outcome is False
    assert invoked == []


def test_b2_exhausted_batch_is_not_invoked(monkeypatch) -> None:
    """T5/T21: 取得回数上限に達したバッチは毎時invokeし続けない。"""
    invoked: list[tuple[str, dict]] = []
    _b2_patch(monkeypatch, _b2_record(attempt_count=3), invoked)

    outcome = handler_module._handle_completion_recovery_candidate(
        {"batch_id": "buy-1", "status": "RUNNING", "batch_family": "BUY_CANDIDATES"}, _B2_NOW
    )

    assert outcome is False
    assert invoked == []


def test_b2_invoke_failure_is_contained(monkeypatch) -> None:
    """T23: invoke自体が失敗しても他バッチの処理を止めず、gateも消費しない
    (attempt_countはgate取得時にしか増えないため、invoke失敗では増えない)。"""
    monkeypatch.setenv("BUY_CANDIDATES_FUNCTION_NAME", "fn-buy")
    monkeypatch.setattr(handler_module, "get_completion_batch", lambda batch_id: _b2_record())

    def _boom(name, payload):
        raise RuntimeError("invoke failed (simulated)")

    monkeypatch.setattr(handler_module, "dispatch_async", _boom)

    outcome = handler_module._handle_completion_recovery_candidate(
        {"batch_id": "buy-1", "status": "RUNNING", "batch_family": "BUY_CANDIDATES"}, _B2_NOW
    )

    assert outcome is False


def test_b2_missing_function_name_is_fail_closed(monkeypatch) -> None:
    """invoke先が未設定ならinvokeせずERRORを残す(環境設定漏れの検出)。"""
    invoked: list[tuple[str, dict]] = []
    _b2_patch(monkeypatch, _b2_record(), invoked)
    monkeypatch.delenv("BUY_CANDIDATES_FUNCTION_NAME", raising=False)

    outcome = handler_module._handle_completion_recovery_candidate(
        {"batch_id": "buy-1", "status": "RUNNING", "batch_family": "BUY_CANDIDATES"}, _B2_NOW
    )

    assert outcome is False
    assert invoked == []
