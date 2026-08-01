"""運用ハードニング5節: finalize処理の再実行安全性(FINALIZING/FINALIZE_FAILED)の
結合テスト。batch_tracker(moto DynamoDB)と watchlist_batch_finalizer を実際に
組み合わせ、以下を確認する。

- finalize処理の途中(監査ログ記録時点)で例外が発生しても、それより前に成功した
  ウォッチリスト追加・LINE通知自体は取り消されないこと。
- 例外後にFINALIZE_FAILEDへ遷移し、retry_finalize()で再実行しても、
  既に追加済みの銘柄が重複追加されず、LINE通知も再送されないこと。
- 主要スコア項目の欠損率が高い場合、データ提供元障害疑い率が低くてもABORTEDとなり
  部分結果をウォッチリストへ追加しないこと。
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
from jstock_advisor.infrastructure.aws.batch_tracker import (
    WatchlistBatchStatus,
    WatchlistProgressStatus,
)
from jstock_advisor.services import watchlist_batch_finalizer as finalizer_module
from jstock_advisor.services.watchlist_batch_finalizer import maybe_finalize, retry_finalize

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


def _fake_config(
    *, high_throttle_rate_threshold_pct: float = 20.0, max_field_missing_rate_pct: float = 30.0
) -> SimpleNamespace:
    watchlist_screening = SimpleNamespace(
        candidate_universe=SimpleNamespace(provider="jpx"),
        screening_policy="high_dividend_financial_health",
        max_watchlist_additions_per_run=20,
        notification_enabled=True,
        high_throttle_rate_threshold_pct=high_throttle_rate_threshold_pct,
        max_field_missing_rate_pct=max_field_missing_rate_pct,
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
        self.calls: list[list[Any]] = []

    def notify_watchlist_additions(self, added_items, results_by_code, policy_name, now):
        self.calls.append(list(added_items))
        return True


def _make_ranking_entry(stock_code: str) -> str:
    return RankingEntry(
        stock_code=stock_code,
        total_score=80.0,
        policy_scores={"high_dividend_financial_health": 80.0},
        matched_criteria=[],
        main_metrics={},
    ).model_dump_json()


def _drive_batch_with_two_passed_candidates(now: dt.datetime) -> None:
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", now, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 2, 72, now)
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111", "2222"], now, 72)
    batch_tracker.mark_dispatch_completed("batch-1", now)
    for stock_code in ("1111", "2222"):
        batch_tracker.claim_candidate_lease("batch-1", stock_code, "owner-a", now, 240)
        batch_tracker.complete_candidate(
            "batch-1",
            stock_code,
            "owner-a",
            terminal_status=WatchlistProgressStatus.COMPLETED,
            evaluation_result="PASSED",
            ranking_entry=_make_ranking_entry(stock_code),
            is_provider_failure_suspected=False,
            missing_field_names=[],
            processing_duration_ms=100,
            now=now,
        )


def test_exception_after_watchlist_additions_does_not_lose_or_duplicate_on_retry(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """finalize処理中(LINE通知成功後・完了記録前)に例外が発生した場合の挙動。
    1回目: add_if_newで2件とも追加・LINE通知成功、その直後のrecord_batch_auditで
    例外(監査ログ書き込み失敗を想定)→ FINALIZE_FAILEDへ。
    2回目(retry_finalize): record_batch_auditは正常に動くよう差し替え、
    add_if_newは同じ銘柄コードに対して重複追加せず、LINE通知も再送されないこと。
    """
    _drive_batch_with_two_passed_candidates(_NOW)

    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()

    call_count = {"n": 0}
    original_record_batch_audit = finalizer_module.record_batch_audit

    def _flaky_record_batch_audit(**kwargs: Any) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("audit log write failed (simulated)")
        original_record_batch_audit(**kwargs)

    monkeypatch.setattr(finalizer_module, "record_batch_audit", _flaky_record_batch_audit)

    config = _fake_config()
    providers = SimpleNamespace(
        financial_data=SimpleNamespace(get_financial_summary=lambda code: None)
    )

    with pytest.raises(RuntimeError, match="audit log write failed"):
        maybe_finalize("batch-1", _NOW, providers, config, fake_notification)

    # 監査ログ書き込みより前に成功していたウォッチリスト追加・LINE通知は取り消されない。
    assert {item.stock_code for item in fake_repo.added} == {"1111", "2222"}
    assert len(fake_notification.calls) == 1
    assert {item.stock_code for item in fake_notification.calls[0]} == {"1111", "2222"}

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.FINALIZE_FAILED.value

    later = _NOW + dt.timedelta(minutes=1)
    retried = retry_finalize("batch-1", later, providers, config, fake_notification)
    assert retried is True

    # 再実行しても重複追加は発生しない。
    assert {item.stock_code for item in fake_repo.added} == {"1111", "2222"}
    assert len(fake_repo.added) == 2
    # 既に追加済みのため、再実行では「新規追加」対象が空になり再送されない。
    assert len(fake_notification.calls) == 1

    batch_after_retry = batch_tracker.get_watchlist_batch("batch-1")
    assert batch_after_retry is not None
    assert batch_after_retry["status"] == WatchlistBatchStatus.COMPLETED.value


def test_add_if_new_exception_for_one_candidate_does_not_block_others(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1銘柄のRepository書き込み失敗(add_if_new例外)が、他銘柄の追加や
    finalize全体の完了を妨げないこと。"""
    _drive_batch_with_two_passed_candidates(_NOW)

    class _PartiallyFailingRepository:
        def __init__(self) -> None:
            self.added: list[Any] = []

        def add_if_new(self, item: Any) -> bool:
            if item.stock_code == "2222":
                raise RuntimeError("dynamodb write failed (simulated)")
            self.added.append(item)
            return True

    fake_repo = _PartiallyFailingRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = SimpleNamespace(
        financial_data=SimpleNamespace(get_financial_summary=lambda code: None)
    )

    finalized = maybe_finalize("batch-1", _NOW, providers, config, fake_notification)
    assert finalized is True
    assert {item.stock_code for item in fake_repo.added} == {"1111"}

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.COMPLETED.value


def test_finalize_aborted_by_field_missing_rate_even_when_provider_failure_rate_is_low(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """運用ハードニング3節: provider_failure_suspected_rateが閾値(20%)未満でも、
    主要スコア項目の欠損率が閾値(既定30%)を超えていればABORTEDとし、
    部分結果をウォッチリストへ追加しないこと。"""
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 4, 72, _NOW)
    batch_tracker.create_missing_candidate_progress_rows(
        "batch-1", ["1111", "2222", "3333", "4444"], _NOW, 72
    )
    batch_tracker.mark_dispatch_completed("batch-1", _NOW)
    # 4件中1件のみprovider障害疑い(25%はテスト対象閾値20%を超えるので、別途
    # 閾値を50%へ緩めてfield_missing側のみで判定させる)。
    for stock_code in ("1111", "2222", "3333", "4444"):
        batch_tracker.claim_candidate_lease("batch-1", stock_code, "owner-a", _NOW, 240)
        # 4件中3件で配当利回りが欠損(75% > 既定閾値30%)。provider障害疑いは0件。
        missing = ["dividend_yield_pct"] if stock_code != "4444" else []
        batch_tracker.complete_candidate(
            "batch-1",
            stock_code,
            "owner-a",
            terminal_status=WatchlistProgressStatus.COMPLETED,
            evaluation_result="PASSED",
            ranking_entry=_make_ranking_entry(stock_code),
            is_provider_failure_suspected=False,
            missing_field_names=missing,
            processing_duration_ms=100,
            now=_NOW,
        )

    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config(high_throttle_rate_threshold_pct=20.0, max_field_missing_rate_pct=30.0)
    providers = SimpleNamespace(
        financial_data=SimpleNamespace(get_financial_summary=lambda code: None)
    )

    finalized = maybe_finalize("batch-1", _NOW, providers, config, fake_notification)
    assert finalized is True

    assert fake_repo.added == []  # 部分結果は採用されない
    assert fake_notification.calls == []

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.ABORTED.value
    assert batch["execution_result"] == (
        batch_tracker.EXECUTION_RESULT_PROVIDER_DATA_QUALITY_DEGRADED
    )
