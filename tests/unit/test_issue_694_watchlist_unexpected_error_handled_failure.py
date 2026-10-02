"""Issue #694: watchlist screening batchのUNEXPECTED_ERRORがdata_unavailable_
countへ希釈され、既存のwatchlist追加サマリー通知からは技術的failureと区別
できない問題のテスト。

Phase B設計(#694参照)のTest Planを適用する:

    unexpected_error_count > 0のときHANDLED_FAILURE通知が発行されることを確認
    unexpected_error_count == 0(NOT_FOUND/DATA_ERRORのみ発生)では通知が
    発生しないことを確認(業務上のN/Aとの区別、必須回帰テスト)
    既存のdata_unavailable_count・watchlist追加サマリー通知が変更されて
    いないことを確認(既存回帰)

HF-0(#665)のdedup・LINE送信・GitHub Issue抑止自体は
test_issue_665_handled_failure_contract.pyが既に固定しているため、本ファイルは
finalizer側(envelopeの生成・publish呼び出し)の責務に限定する。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.signals.watchlist_screening import RankingEntry
from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.infrastructure.aws.batch_tracker import WatchlistProgressStatus
from jstock_advisor.services import watchlist_batch_finalizer as finalizer_module
from jstock_advisor.services.watchlist_batch_finalizer import maybe_finalize

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


@pytest.fixture
def dynamo(
    monkeypatch: pytest.MonkeyPatch, lambda_runtime_env: None, create_collection_table: Any
):
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
        create_collection_table("watchlist_removal_history.json", "stock_code")
        create_collection_table("audit_log.json", "audit_id")
        create_collection_table("notification_log.json", "notification_id")
        create_collection_table("notification_claims.json", "claim_id")
        create_collection_table("watchlist_rotation_dispatch_lease.json", "rotation_id")
        create_collection_table("watchlist_screening_rotation_state.json", "rotation_id")
        yield client


def _fake_config() -> SimpleNamespace:
    watchlist_screening = SimpleNamespace(
        candidate_universe=SimpleNamespace(provider="jpx"),
        screening_policy="high_dividend_financial_health",
        max_watchlist_additions_per_run=20,
        notification_enabled=True,
        universe_failure_notification_enabled=True,
        high_throttle_rate_threshold_pct=20.0,
        max_scoring_field_missing_rate_pct=30.0,
        max_data_error_rate_pct=100.0,
        max_not_found_rate_pct=100.0,
        max_terminal_failure_rate_pct=100.0,
        max_required_field_missing_rate_pct=100.0,
        max_notification_retry_attempts=3,
        scoring=SimpleNamespace(
            minimum_total_score=60.0,
            dividend_yield=SimpleNamespace(weight=30.0, zero_at_pct=3.5, full_at_pct=6.0),
            equity_ratio=SimpleNamespace(weight=25.0, zero_at_pct=40.0, full_at_pct=70.0),
            payout_ratio=SimpleNamespace(weight=15.0, healthy_min_pct=20.0, healthy_max_pct=60.0),
            dividend_growth=SimpleNamespace(weight=15.0, zero_at_years=0, full_at_years=10),
            shareholder_benefit=SimpleNamespace(
                weight=15.0, yield_full_at_pct=2.0, presence_only_score_ratio=0.5
            ),
        ),
        thresholds=SimpleNamespace(
            minimum_market_cap_yen=50_000_000_000,
            require_positive_operating_cash_flow=True,
            exclude_dividend_cut_announced=True,
            exclude_debt_excess=True,
            exclude_deficit=True,
            exclude_going_concern_doubt=True,
            exclude_etf=True,
            exclude_reit=True,
        ),
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

    def get(self, stock_code: str) -> Any | None:
        return next((item for item in self.added if item.stock_code == stock_code), None)


class _FakeNotificationService:
    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    def notify_watchlist_additions(self, summary, content_hash):  # noqa: ANN001, ANN201
        self.calls.append(list(summary.items))
        return True


def _make_ranking_entry(stock_code: str) -> str:
    return RankingEntry(
        stock_code=stock_code,
        total_score=80.0,
        policy_scores={"high_dividend_financial_health": 80.0},
        matched_criteria=[],
        main_metrics={},
    ).model_dump_json()


def _complete(
    batch_id: str, stock_code: str, now: dt.datetime, *, evaluation_result: str
) -> None:
    ranking_entry = _make_ranking_entry(stock_code) if evaluation_result == "PASSED" else None
    total_score = 80.0 if evaluation_result == "PASSED" else None
    batch_tracker.claim_candidate_lease(batch_id, stock_code, "owner-a", now, 240)
    batch_tracker.complete_candidate(
        batch_id,
        stock_code,
        "owner-a",
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result=evaluation_result,
        ranking_entry=ranking_entry,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=now,
        total_score=total_score,
    )


def _drive_batch(stock_code_to_result: dict[str, str], now: dt.datetime) -> None:
    batch_id = "batch-1"
    stock_codes = list(stock_code_to_result)
    batch_tracker.try_acquire_dispatch_lease(batch_id, "dispatcher", now, 360, 72)
    batch_tracker.set_watchlist_batch_total(batch_id, len(stock_codes), 72, now)
    batch_tracker.create_missing_candidate_progress_rows(batch_id, stock_codes, now, 72)
    batch_tracker.mark_dispatch_completed(batch_id, now)
    for stock_code, evaluation_result in stock_code_to_result.items():
        _complete(batch_id, stock_code, now, evaluation_result=evaluation_result)


def _providers() -> SimpleNamespace:
    return SimpleNamespace(financial_data=SimpleNamespace(get_financial_summary=lambda code: None))


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    envelopes: list[dict[str, Any]] = []
    monkeypatch.setattr(finalizer_module, "publish_incident_envelope", envelopes.append)
    return envelopes


def test_unexpected_error_count_positive_is_handled_failure(
    dynamo, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    """unexpected_error_count > 0のとき、HANDLED_FAILUREが1件publishされ、
    かつ既存のwatchlist追加サマリー通知(data_unavailable_countの算出含む)は
    変更されない(既存回帰)。"""
    _drive_batch(
        {"1111": "PASSED", "2222": "UNEXPECTED_ERROR", "3333": "NOT_FOUND"}, _NOW
    )
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: _FakeWatchlistRepository())
    notification = _FakeNotificationService()

    result = maybe_finalize("batch-1", _NOW, _providers(), _fake_config(), notification)

    assert result is True
    # 既存回帰: 追加されたのはPASSEDの1銘柄のみ(data_unavailable_countの算出・
    # watchlist追加サマリー自体に変更がないことの確認)。
    assert len(notification.calls) == 1
    assert [e.stock_code for e in notification.calls[0]] == ["1111"]

    assert len(published) == 1
    assert published[0]["failure_stage"] == "WATCHLIST_SCREENING_EVALUATION"
    assert published[0]["failure_type"] == "UNHANDLED_EXCEPTION"
    assert published[0]["failure_count"] == 1
    assert published[0]["failure_class"] == "HANDLED_FAILURE"


def test_unexpected_error_count_zero_publishes_nothing(
    dynamo, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    """unexpected_error_count == 0(NOT_FOUND/DATA_ERRORのみ発生)では、業務上の
    N/Aとして扱い追加通知しない(必須回帰: 誤発火しないことの確認)。"""
    _drive_batch({"1111": "PASSED", "2222": "NOT_FOUND", "3333": "DATA_ERROR"}, _NOW)
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: _FakeWatchlistRepository())
    notification = _FakeNotificationService()

    result = maybe_finalize("batch-1", _NOW, _providers(), _fake_config(), notification)

    assert result is True
    assert published == []


def test_multiple_unexpected_errors_in_one_batch_are_aggregated(
    dynamo, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    """batch_id単位で複数件のUNEXPECTED_ERRORが集約され、envelope1件・
    failure_count=件数として報告される(個別銘柄ごとに複数publishしない)。"""
    _drive_batch(
        {"1111": "UNEXPECTED_ERROR", "2222": "UNEXPECTED_ERROR", "3333": "PASSED"}, _NOW
    )
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: _FakeWatchlistRepository())
    notification = _FakeNotificationService()

    result = maybe_finalize("batch-1", _NOW, _providers(), _fake_config(), notification)

    assert result is True
    assert len(published) == 1
    assert published[0]["failure_count"] == 2
