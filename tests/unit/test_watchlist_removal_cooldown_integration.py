"""計画Part C-4(再追加クールダウン)・C-6(削除監査)の結合テスト(テストI/J/K)。

`_write_watchlist_additions()`が追加直前にWatchlistRemovalHistoryを参照し、
クールダウン中の銘柄は追加をスキップすること(I/J)、および自動削除が
`record_removal_audit()`へ判定に必要な全項目を渡すこと(K)を、
実際に`maybe_finalize()`/`maybe_finalize_maintenance()`を駆動して確認する。
test_watchlist_finalize_integration.pyと同じmoto DynamoDBフィクスチャ・
フェイクRepositoryパターンを踏襲する。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.entities.enums import WatchlistRegistrationSource
from jstock_advisor.domain.entities.watchlist import WatchlistItem, WatchlistRemovalHistory
from jstock_advisor.domain.signals.watchlist_screening import RankingEntry
from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.infrastructure.aws.batch_tracker import (
    WatchlistBatchStatus,
    WatchlistProgressStatus,
)
from jstock_advisor.infrastructure.local_repository.audit_log_repository import (
    AuditLogRepository,
)
from jstock_advisor.infrastructure.local_repository.watchlist_removal_history_repository import (
    WatchlistRemovalHistoryRepository,
)
from jstock_advisor.services import audit_service as audit_service_module
from jstock_advisor.services import watchlist_batch_finalizer as finalizer_module
from jstock_advisor.services.watchlist_batch_finalizer import (
    maybe_finalize,
    maybe_finalize_maintenance,
)
from jstock_advisor.services.watchlist_maintenance_service import MaintenanceScreeningSummary
from jstock_advisor.services.watchlist_screening_audit import REPOSITORY_RESULT_SKIPPED_COOLDOWN

_REGION = "ap-northeast-1"
_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)
_BATCH_TABLE = "jstock-batch_runs"
_PROGRESS_TABLE = "jstock-watchlist_candidate_progress"


@pytest.fixture(autouse=True)
def _stub_display_name_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        finalizer_module,
        "build_stock_display_name_resolver",
        lambda *_a, **_kw: _FakeStockDisplayNameResolver(),
    )


@pytest.fixture(autouse=True)
def _isolate_audit_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    monkeypatch.setattr(
        audit_service_module,
        "AuditLogRepository",
        lambda store_dir=None: AuditLogRepository(store_dir=audit_dir),
    )


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


@pytest.fixture
def removal_history_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store_dir = tmp_path / "removal_history"

    def _factory(readd_cooldown_days: int) -> WatchlistRemovalHistoryRepository:
        return WatchlistRemovalHistoryRepository(readd_cooldown_days, store_dir=store_dir)

    monkeypatch.setattr(finalizer_module, "WatchlistRemovalHistoryRepository", _factory)
    return store_dir


class _FakeStockDisplayNameResolver:
    def resolve(self, stock_code, fallback_name=None, fallback_name_provider=None):  # noqa: ANN001, ANN201
        if fallback_name:
            return fallback_name
        if fallback_name_provider is not None:
            provided = fallback_name_provider()
            if provided:
                return provided
        return stock_code


class _FakeWatchlistRepository:
    def __init__(self, preexisting: list[Any] | None = None) -> None:
        self.added: list[Any] = list(preexisting or [])

    def add_if_new(self, item: Any) -> bool:
        if any(existing.stock_code == item.stock_code for existing in self.added):
            return False
        self.added.append(item)
        return True

    def get(self, stock_code: str) -> Any | None:
        return next((item for item in self.added if item.stock_code == stock_code), None)


class _FakeNotificationService:
    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    def notify_watchlist_additions(self, summary, content_hash):  # noqa: ANN001, ANN201
        self.calls.append(list(summary.items))
        return True


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


def _fake_config(*, readd_cooldown_days: int = 30) -> SimpleNamespace:
    watchlist_screening = SimpleNamespace(
        candidate_universe=SimpleNamespace(provider="jpx"),
        screening_policy="high_dividend_financial_health",
        max_watchlist_additions_per_run=20,
        notification_enabled=True,
        high_throttle_rate_threshold_pct=100.0,
        max_scoring_field_missing_rate_pct=100.0,
        max_data_error_rate_pct=100.0,
        max_not_found_rate_pct=100.0,
        max_terminal_failure_rate_pct=100.0,
        max_required_field_missing_rate_pct=100.0,
        max_notification_retry_attempts=3,
        scoring=_fake_scoring_config(),
        thresholds=_fake_thresholds_config(),
        stock_display_name=SimpleNamespace(jpx_name_negative_cache_ttl_seconds=60),
        auto_removal=SimpleNamespace(
            enabled=True,
            readd_cooldown_days=readd_cooldown_days,
            minimum_age_days=90,
            consecutive_not_qualified_required=3,
            minimum_not_qualified_span_days=28,
            stale_recheck_days=30,
            maximum_unconfirmed_days=180,
        ),
    )
    return SimpleNamespace(watchlist_screening=watchlist_screening)


def _providers() -> SimpleNamespace:
    return SimpleNamespace(financial_data=SimpleNamespace(get_financial_summary=lambda code: None))


def _make_ranking_entry(stock_code: str) -> str:
    return RankingEntry(
        stock_code=stock_code,
        total_score=80.0,
        policy_scores={"high_dividend_financial_health": 80.0},
        matched_criteria=[],
        main_metrics={},
    ).model_dump_json()


def _drive_batch_with_one_passed_candidate(now: dt.datetime, batch_id: str = "batch-1") -> None:
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


# --- テストI: クールダウン中は再追加しない --------------------------------------


def test_stock_in_cooldown_is_skipped_and_not_added(
    dynamo, removal_history_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = WatchlistRemovalHistoryRepository(30, store_dir=removal_history_store)
    repo.upsert(
        WatchlistRemovalHistory(
            stock_code="1111",
            removed_at=_NOW - dt.timedelta(days=10),
            removal_reason="債務超過のため対象外です",
            removal_category="IMMEDIATE",
            cooldown_until=_NOW + dt.timedelta(days=20),
        )
    )
    _drive_batch_with_one_passed_candidate(_NOW)
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = _providers()

    finalized = maybe_finalize("batch-1", _NOW, providers, config, fake_notification)
    assert finalized is True

    assert fake_repo.added == []
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.COMPLETED.value
    assert batch["repository_results"]["1111"] == REPOSITORY_RESULT_SKIPPED_COOLDOWN
    # クールダウン中はウォッチリストへ追加されないため通知対象にも含まれない。
    assert fake_notification.calls == []


# --- テストJ: クールダウン終了後は再追加可能 --------------------------------------


def test_stock_after_cooldown_expiry_can_be_readded(
    dynamo, removal_history_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = WatchlistRemovalHistoryRepository(30, store_dir=removal_history_store)
    repo.upsert(
        WatchlistRemovalHistory(
            stock_code="1111",
            removed_at=_NOW - dt.timedelta(days=40),
            removal_reason="債務超過のため対象外です",
            removal_category="IMMEDIATE",
            cooldown_until=_NOW - dt.timedelta(days=10),  # 既に終了済み
        )
    )
    _drive_batch_with_one_passed_candidate(_NOW)
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = _providers()

    finalized = maybe_finalize("batch-1", _NOW, providers, config, fake_notification)
    assert finalized is True

    assert {item.stock_code for item in fake_repo.added} == {"1111"}
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["repository_results"]["1111"] == finalizer_module.REPOSITORY_RESULT_ADDED
    assert len(fake_notification.calls) == 1


def test_stock_never_removed_before_has_no_cooldown_and_is_added(
    dynamo, removal_history_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """通常ケース(削除履歴が一切無い銘柄)ではクールダウンチェック自体が
    何の影響も与えないこと(既存の追加フローの回帰確認)。"""
    _drive_batch_with_one_passed_candidate(_NOW)
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = _providers()

    finalized = maybe_finalize("batch-1", _NOW, providers, config, fake_notification)
    assert finalized is True
    assert {item.stock_code for item in fake_repo.added} == {"1111"}


# --- テストK: 削除監査が完全に残る ------------------------------------------------


def test_removal_audit_records_full_decision_context(
    dynamo, removal_history_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自動削除時、record_removal_audit()へ理由・カテゴリ・スコア・一致タイプ・
    連続非該当回数・hard_exclusion_reasonsが漏れなく渡されること(計画Part C-6)。"""

    class _FakeMaintenanceWatchlistRepository:
        def __init__(self) -> None:
            self._items: dict[str, WatchlistItem] = {}

        def get(self, stock_code: str) -> WatchlistItem | None:
            return self._items.get(stock_code)

        def upsert(self, item: WatchlistItem) -> None:
            self._items[item.stock_code] = item

        def delete(self, stock_code: str) -> bool:
            return self._items.pop(stock_code, None) is not None

    fake_repo = _FakeMaintenanceWatchlistRepository()
    registered_at = _NOW - dt.timedelta(days=120)
    fake_repo.upsert(
        WatchlistItem(
            stock_code="1111",
            stock_name="テスト銘柄",
            reason="自動追加",
            registration_source=WatchlistRegistrationSource.AUTO_SCREENING,
            registration_policy="multi_style_monitoring",
            created_at=registered_at,
            updated_at=registered_at,
            consecutive_not_qualified_count=2,
            removal_candidate_since=_NOW - dt.timedelta(days=28),
        )
    )
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)

    captured: dict[str, Any] = {}
    original_record_removal_audit = finalizer_module.record_removal_audit

    def _capturing_record_removal_audit(*args: Any, **kwargs: Any) -> None:
        captured["args"] = args
        original_record_removal_audit(*args, **kwargs)

    monkeypatch.setattr(finalizer_module, "record_removal_audit", _capturing_record_removal_audit)

    config = _fake_config()
    batch_id = "watchlist-maint-1"
    batch_tracker.try_acquire_dispatch_lease(batch_id, "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total(batch_id, 1, 72, _NOW, job_type="WATCHLIST_MAINTENANCE")
    batch_tracker.create_missing_candidate_progress_rows(batch_id, ["1111"], _NOW, 72)
    batch_tracker.mark_dispatch_completed(batch_id, _NOW)
    batch_tracker.claim_candidate_lease(batch_id, "1111", "owner-a", _NOW, 240)
    summary = MaintenanceScreeningSummary(
        passed=False,
        total_score=25.0,
        matched_target_types=[],
        hard_exclusion_reasons=["開示情報にリスクキーワードを検出しました"],
        policy_name="multi_style_monitoring",
    )
    batch_tracker.complete_candidate(
        batch_id,
        "1111",
        "owner-a",
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result="FAILED",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=_NOW,
        screening_summary_json=summary.model_dump_json(),
    )

    finalized = maybe_finalize_maintenance(batch_id, _NOW, config)
    assert finalized is True

    # 削除が実行され、WatchlistRepositoryから消えていること。
    assert fake_repo.get("1111") is None

    assert "args" in captured
    (
        stock_code,
        stock_name,
        recorded_registered_at,
        registration_policy,
        removed_at,
        removal_reason,
        removal_category,
        last_monitoring_score,
        last_matched_target_types,
        consecutive_not_qualified_count,
        hard_exclusion_reasons,
        now_arg,
        recorded_batch_id,
    ) = captured["args"]
    assert stock_code == "1111"
    assert stock_name == "テスト銘柄"
    assert recorded_registered_at == registered_at
    assert registration_policy == "multi_style_monitoring"
    assert removed_at == _NOW
    assert removal_reason == "開示情報にリスクキーワードを検出しました"
    assert removal_category == "CONSECUTIVE_NOT_QUALIFIED"
    assert last_monitoring_score == 25.0
    assert last_matched_target_types == []
    assert consecutive_not_qualified_count == 3
    assert hard_exclusion_reasons == ["開示情報にリスクキーワードを検出しました"]
    assert now_arg == _NOW
    assert recorded_batch_id == batch_id
