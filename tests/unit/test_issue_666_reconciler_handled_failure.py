"""Issue #666(HF-1): Watchlist Reconcilerの隔離された部分失敗(B1〜B6)が、
正常終了へ埋没させずHANDLED_FAILUREとしてUSER通知されることのテスト。

Phase B設計(issuecomment、#666参照)のTest PlanをIssue固有分として適用する:

    HF1-AC1  B1〜B6すべてがHANDLED_FAILUREとして記録される
    HF1-AC2  1回のreconciler実行で複数境界が同時に失敗しても、run単位で
             集約された1件のenvelopeがpublishされる(境界ごとに1件。個別
             発生ごとには送らない)
    HF1-AC3  #529相当のtrade_event_reconciliation失敗でenvelopeが1件publish
             される代表E2E
    HF1-AC4  既存のwatchlist batch reconciliation本体の継続(isolation契約)
             に回帰がない
    T7       B1〜B6が互いに異なるfingerprint入力(failure_stage/failure_type/
             reason_code)を持つ

HF-0(#665)のdedup・LINE送信・GitHub Issue抑止自体は
test_issue_665_handled_failure_contract.pyが既に固定しているため、本ファイルは
reconciler側(envelopeの生成・publish呼び出し)の責務に限定する。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.lambda_handlers import watchlist_batch_reconciler_handler as handler_module

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


def _fake_config() -> SimpleNamespace:
    watchlist_screening = SimpleNamespace(
        enabled=True,
        scheduled_run_enabled=True,
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
        max_finalize_retry_attempts=3,
        max_notification_retry_attempts=3,
        max_timeout_finalize_rows_per_run=500,
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
    holiday_calendar = SimpleNamespace(
        recurring_market_closures=SimpleNamespace(dates_mm_dd=[]),
        additional_closures=SimpleNamespace(dates=[]),
    )
    notification = SimpleNamespace(
        trade_event_reconciliation=SimpleNamespace(max_records_per_run=100)
    )
    return SimpleNamespace(
        watchlist_screening=watchlist_screening,
        holiday_calendar=holiday_calendar,
        notification=notification,
    )


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """HF-0契約(#665)のpublish_incident_envelope()をrecordingへ差し替える。

    HF-1の新規コード(`_notify_handled_failures_if_any()`)は、既存の
    private `_publish_incident_envelope()`(#506/#507が使う旧SNS allowlist。
    failure_classキーを持たない)ではなく、`failure_class`を許可する新しい
    共有moduleの`publish_incident_envelope`を使う(#665設計。handler_module
    の名前空間へ直接importされている)。
    """
    envelopes: list[dict[str, Any]] = []
    monkeypatch.setattr(handler_module, "publish_incident_envelope", envelopes.append)
    # 既存の4種の検知(missed_schedule等)が使う旧経路は無関係のため、
    # 誤って実SNS呼び出しへ到達しないよう併せて無害化する。
    monkeypatch.setattr(handler_module, "_publish_incident_envelope", lambda envelope: None)
    return envelopes


@pytest.fixture(autouse=True)
def _stub_expensive_dependencies(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(handler_module, "load_config", lambda: _fake_config())
    monkeypatch.setattr(handler_module, "build_real_provider_bundle", lambda now, config: object())
    monkeypatch.setattr(
        handler_module, "build_cached_provider_bundle", lambda base, config, now: base
    )
    monkeypatch.setattr(
        handler_module, "_build_notification_service", lambda config, line_client=None: object()
    )
    monkeypatch.setattr(handler_module, "record_batch_audit", lambda **kw: None)
    monkeypatch.setattr(handler_module, "_fetch_watchlist_worker_metrics", lambda now: {})
    monkeypatch.setattr(
        handler_module,
        "WatchlistRemovalHistoryRepository",
        lambda *_a, **_kw: SimpleNamespace(list_all=lambda: []),
    )
    # 本ファイルはB1〜B6の計測・publishのみを検証対象とする。既存4種の検知
    # (S-2/S-4/S-6/S-7)はtest_watchlist_batch_reconciler_handler.py側の
    # 専任テストが担うため、本ファイルでは候補を空にして無関係にする。
    monkeypatch.setattr(handler_module, "list_watchlist_batches_by_status", lambda statuses: [])
    monkeypatch.setattr(handler_module, "list_stale_maintenance_triggers", lambda now: [])
    monkeypatch.setattr(
        handler_module, "reconcile_pending_trade_events", lambda *a, **kw: SimpleNamespace(
            remaining=0, processed=0, already_consumed_by_other_run=0
        )
    )
    return SimpleNamespace()


# --- B1: finalize recovery invoke failed(COMPLETION_RECOVERY) --------------------


def test_b1_completion_recovery_invoke_failure_is_handled_failure(
    monkeypatch: pytest.MonkeyPatch, dynamo, published: list[dict[str, Any]]
) -> None:
    """B1: buy/holdings finalize-only invokeの失敗がHANDLED_FAILUREとして
    1件publishされ、本体の継続(outcome=False)に影響しない。"""
    from jstock_advisor.domain.entities.execution_context import ExecutionContext
    from jstock_advisor.infrastructure.aws.batch_tracker import (
        BatchFamily,
        BatchProgress,
        CompletionBatchRecord,
    )

    progress = BatchProgress(
        total=1,
        completed=1,
        category_counts={},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[],
        sector_entries=[],
        holding_count=0,
        completed_codes=["7203"],
    )
    record = CompletionBatchRecord(
        batch_id="buy-1",
        family=BatchFamily.BUY_CANDIDATES,
        execution_context=ExecutionContext.normal(),
        progress=progress,
        attempt_count=0,
        finalize_started_at=None,
        finalize_completed_at=None,
        finalize_failed_at=None,
    )
    monkeypatch.setenv("BUY_CANDIDATES_FUNCTION_NAME", "fn-buy")
    monkeypatch.setattr(handler_module, "get_completion_batch", lambda batch_id: record)

    def _boom(name: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("invoke failed (simulated)")

    monkeypatch.setattr(handler_module, "dispatch_async", _boom)
    monkeypatch.setattr(
        handler_module,
        "list_watchlist_batches_by_status",
        lambda statuses: [
            {"batch_id": "buy-1", "status": "RUNNING", "batch_family": "BUY_CANDIDATES"}
        ],
    )

    result = handler_module.handler({}, object())

    assert result["completion_recovery_skipped"] == 1  # isolation維持(本体の継続)
    assert result["handled_failure_counts"]["COMPLETION_RECOVERY"] == 1
    assert len(published) == 1
    assert published[0]["failure_stage"] == "COMPLETION_RECOVERY"
    assert published[0]["failure_count"] == 1
    assert published[0]["failure_class"] == "HANDLED_FAILURE"


# --- B2: trade_event_reconciliation failed ----------------------------------------


def test_b2_trade_event_reconciliation_failure_is_handled_failure(
    monkeypatch: pytest.MonkeyPatch, dynamo, published: list[dict[str, Any]]
) -> None:
    """HF1-AC3代表E2E: #529相当のtrade_event_reconciliation失敗で、watchlist
    batch reconciliation本体は継続し(isolation契約。既存の回帰無し)、
    HANDLED_FAILUREが1件publishされる。"""

    def _boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("AccessDenied (simulated #529)")

    monkeypatch.setattr(handler_module, "reconcile_pending_trade_events", _boom)

    result = handler_module.handler({}, object())

    assert result["candidates"] == 0  # 本体(watchlist batch reconciliation)は完走した
    assert result["handled_failure_counts"]["TRADE_EVENT_RECONCILIATION"] == 1
    assert len(published) == 1
    assert published[0]["failure_stage"] == "TRADE_EVENT_RECONCILIATION"
    assert published[0]["failure_count"] == 1


# --- B3: retry_finalize unexpected error ------------------------------------------


def test_b3_retry_finalize_unexpected_error_is_handled_failure(
    monkeypatch: pytest.MonkeyPatch, dynamo, published: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(
        handler_module,
        "list_watchlist_batches_by_status",
        lambda statuses: [
            {"batch_id": "fin-1", "status": "FINALIZE_FAILED", "finalize_attempt_count": 0}
        ],
    )

    def _boom(*a: Any, **kw: Any) -> bool:
        raise RuntimeError("retry_finalize unexpected error (simulated)")

    monkeypatch.setattr(handler_module, "retry_finalize", _boom)

    result = handler_module.handler({}, object())

    assert result["handled_failure_counts"]["FINALIZE_RETRY"] == 1
    assert len(published) == 1
    assert published[0]["failure_stage"] == "FINALIZE_RETRY"


# --- B4: retry_notification unexpected error --------------------------------------


def test_b4_retry_notification_unexpected_error_is_handled_failure(
    monkeypatch: pytest.MonkeyPatch, dynamo, published: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(
        handler_module,
        "list_watchlist_batches_by_status",
        lambda statuses: [
            {
                "batch_id": "notif-1",
                "status": "NOTIFICATION_FAILED",
                "notification_failure_count": 0,
            }
        ],
    )

    def _boom(*a: Any, **kw: Any) -> bool:
        raise RuntimeError("retry_notification unexpected error (simulated)")

    monkeypatch.setattr(handler_module, "retry_notification", _boom)

    result = handler_module.handler({}, object())

    assert result["handled_failure_counts"]["NOTIFICATION_RETRY"] == 1
    assert len(published) == 1
    assert published[0]["failure_stage"] == "NOTIFICATION_RETRY"


# --- B5: timeout finalizing unexpected error ---------------------------------------


def test_b5_timeout_finalizing_unexpected_error_is_handled_failure(
    monkeypatch: pytest.MonkeyPatch, dynamo, published: list[dict[str, Any]]
) -> None:
    """status=TIMEOUT_FINALIZING(前回実行からの継続)は、try_acquire_timeout_
    finalization()を経由せず直接_process_timeout_finalizing()へ渡される
    (既存コードのfallback分岐)。後続のtransition_timeout_finalizing_to_failed()
    はConditionExpression不一致(テスト用batch_idは実テーブルに存在しない)を
    ベストエフォートで無視する既存実装のため、追加のDB行作成は不要。"""
    monkeypatch.setattr(
        handler_module,
        "list_watchlist_batches_by_status",
        lambda statuses: [{"batch_id": "timeout-1", "status": "TIMEOUT_FINALIZING"}],
    )

    def _boom(*a: Any, **kw: Any) -> None:
        raise RuntimeError("timeout finalizing unexpected error (simulated)")

    monkeypatch.setattr(handler_module, "_process_timeout_finalizing", _boom)

    result = handler_module.handler({}, object())

    assert result["handled_failure_counts"]["TIMEOUT_FINALIZING"] == 1
    assert len(published) == 1
    assert published[0]["failure_stage"] == "TIMEOUT_FINALIZING"


# --- B6: maintenance trigger retry unexpected error --------------------------------


def test_b6_maintenance_trigger_retry_unexpected_error_is_handled_failure(
    monkeypatch: pytest.MonkeyPatch, dynamo, published: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(
        handler_module,
        "list_stale_maintenance_triggers",
        lambda now: [{"batch_id": "maint-1", "status": "COMPLETED"}],
    )

    def _boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("maintenance trigger retry unexpected error (simulated)")

    monkeypatch.setattr(handler_module, "maybe_trigger_maintenance", _boom)

    result = handler_module.handler({}, object())

    assert result["handled_failure_counts"]["MAINTENANCE_TRIGGER_RETRY"] == 1
    assert len(published) == 1
    assert published[0]["failure_stage"] == "MAINTENANCE_TRIGGER_RETRY"


# --- HF1-AC2: 複数境界が同一run内で発生してもrun単位で集約される -------------------


def test_multiple_boundaries_in_one_run_publish_one_envelope_each(
    monkeypatch: pytest.MonkeyPatch, dynamo, published: list[dict[str, Any]]
) -> None:
    """B2とB3が同一run内で同時に発生した場合、envelopeは境界ごとに1件
    (合計2件)publishされる。個別batch発生ごとの重複publishにはならない
    (FINALIZE_FAILEDの候補を2件渡しても、failure_count=2で1件に集約される)。"""

    def _trade_event_boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("AccessDenied (simulated)")

    monkeypatch.setattr(handler_module, "reconcile_pending_trade_events", _trade_event_boom)
    monkeypatch.setattr(
        handler_module,
        "list_watchlist_batches_by_status",
        lambda statuses: [
            {"batch_id": "fin-1", "status": "FINALIZE_FAILED", "finalize_attempt_count": 0},
            {"batch_id": "fin-2", "status": "FINALIZE_FAILED", "finalize_attempt_count": 0},
        ],
    )

    def _retry_finalize_boom(*a: Any, **kw: Any) -> bool:
        raise RuntimeError("retry_finalize unexpected error (simulated)")

    monkeypatch.setattr(handler_module, "retry_finalize", _retry_finalize_boom)

    result = handler_module.handler({}, object())

    assert result["handled_failure_counts"]["TRADE_EVENT_RECONCILIATION"] == 1
    assert result["handled_failure_counts"]["FINALIZE_RETRY"] == 2  # 2バッチ分が集約された
    assert len(published) == 2  # 境界ごとに1件(合計2件。FINALIZE_RETRYは1件へ集約)
    stages = {e["failure_stage"] for e in published}
    assert stages == {"TRADE_EVENT_RECONCILIATION", "FINALIZE_RETRY"}
    finalize_envelope = next(e for e in published if e["failure_stage"] == "FINALIZE_RETRY")
    assert finalize_envelope["failure_count"] == 2


# --- HF1-AC1 / T7: 6境界すべてが記録され、互いに異なるfingerprint入力を持つ -------


def test_all_six_boundaries_have_distinct_fingerprint_inputs() -> None:
    """T7: B1〜B6の(failure_stage, failure_type, reason_code)の組が、6通り
    すべて互いに異なることを固定する(境界ごとに別fingerprintとして区別される
    ための入力契約)。"""
    metadata = handler_module._HANDLED_FAILURE_BOUNDARY_METADATA
    assert set(metadata) == {
        "COMPLETION_RECOVERY",
        "TRADE_EVENT_RECONCILIATION",
        "FINALIZE_RETRY",
        "NOTIFICATION_RETRY",
        "TIMEOUT_FINALIZING",
        "MAINTENANCE_TRIGGER_RETRY",
    }
    reason_codes = [reason_code for _failure_type, reason_code in metadata.values()]
    assert len(reason_codes) == len(set(reason_codes)), "reason_codeは6境界で重複してはならない"


def test_zero_handled_failures_publishes_nothing(
    dynamo, published: list[dict[str, Any]]
) -> None:
    """0件の境界では何もpublishしない(HF1-AC1の裏付け: 発生していないものを
    誤発火させない)。"""
    result = handler_module.handler({}, object())

    assert result["handled_failure_counts"] == dict.fromkeys(
        handler_module._HANDLED_FAILURE_BOUNDARY_METADATA, 0
    )
    assert published == []
