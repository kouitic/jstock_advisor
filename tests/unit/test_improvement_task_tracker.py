"""improvement_task_tracker.pyの原子的状態遷移テスト(振り返り機能改修)。

batch_tracker.py向けのmoto実テーブルパターン(test_watchlist_batch_tracker.py)を
踏襲する。
"""

from __future__ import annotations

import datetime as dt

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.entities.enums import (
    ImprovementPriority,
    ImprovementTaskStatus,
    RecommendationType,
)
from jstock_advisor.infrastructure.aws import improvement_task_tracker as tracker

_REGION = "ap-northeast-1"
_NOW = dt.datetime(2026, 8, 10, 9, 0, tzinfo=dt.UTC)
_TABLE = "jstock-improvement_tasks"
_KEY = "BUY|v11|ALL|PERFORMANCE_DEGRADED"


@pytest.fixture
def dynamo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_TABLE,
            KeySchema=[{"AttributeName": "candidate_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "candidate_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def _ensure_task(now: dt.datetime = _NOW) -> None:
    tracker.ensure_task_exists(
        _KEY, RecommendationType.BUY, "v11", None, ImprovementPriority.B, now
    )


# --- ensure_task_exists ---------------------------------------------------


def test_ensure_task_exists_creates_candidate_status(dynamo) -> None:
    _ensure_task()
    task = tracker.get_improvement_task(_KEY)
    assert task is not None
    assert task["status"] == ImprovementTaskStatus.CANDIDATE.value


def test_ensure_task_exists_is_idempotent(dynamo) -> None:
    _ensure_task()
    tracker.try_claim_new_issue_creation(_KEY, _NOW, 10)
    _ensure_task(_NOW + dt.timedelta(minutes=1))  # 2回目は何もしない

    task = tracker.get_improvement_task(_KEY)
    assert task is not None
    assert task["status"] == ImprovementTaskStatus.ISSUE_CREATING.value


# --- try_claim_new_issue_creation -----------------------------------------


def test_try_claim_new_issue_creation_succeeds_on_candidate_status(dynamo) -> None:
    _ensure_task()
    assert tracker.try_claim_new_issue_creation(_KEY, _NOW, 10) is True
    task = tracker.get_improvement_task(_KEY)
    assert task is not None
    assert task["status"] == ImprovementTaskStatus.ISSUE_CREATING.value


def test_try_claim_new_issue_creation_fails_while_unexpired_creating(dynamo) -> None:
    _ensure_task()
    assert tracker.try_claim_new_issue_creation(_KEY, _NOW, 10) is True
    # 期限内に他の実行が奪おうとしても失敗する(期限が切れていても即座には奪えない、
    # try_claim_new_issue_creationはISSUE_CREATINGの間は無条件に拒否する)
    later_but_still_within_timeout = _NOW + dt.timedelta(minutes=5)
    assert tracker.try_claim_new_issue_creation(_KEY, later_but_still_within_timeout, 10) is False


def test_try_claim_new_issue_creation_fails_even_when_expired(dynamo) -> None:
    """stale(期限切れ)であっても、try_claim_new_issue_creationは絶対に奪わない
    (reconciliation済みのtry_reclaim_stale_issue_creation経由でのみ奪える)。"""
    _ensure_task()
    tracker.try_claim_new_issue_creation(_KEY, _NOW, 10)
    much_later = _NOW + dt.timedelta(minutes=30)
    assert tracker.try_claim_new_issue_creation(_KEY, much_later, 10) is False


def test_try_claim_new_issue_creation_succeeds_after_failure_status(dynamo) -> None:
    _ensure_task()
    tracker.try_claim_new_issue_creation(_KEY, _NOW, 10)
    tracker.mark_issue_creation_failed(_KEY, _NOW)
    assert tracker.try_claim_new_issue_creation(_KEY, _NOW + dt.timedelta(hours=1), 10) is True


# --- try_reclaim_stale_issue_creation --------------------------------------


def test_try_reclaim_stale_issue_creation_succeeds_when_expired(dynamo) -> None:
    _ensure_task()
    tracker.try_claim_new_issue_creation(_KEY, _NOW, 10)
    task = tracker.get_improvement_task(_KEY)
    assert task is not None
    expired_now = _NOW + dt.timedelta(minutes=30)

    ok = tracker.try_reclaim_stale_issue_creation(
        _KEY, task["issue_claimed_at"], expired_now, 10
    )
    assert ok is True
    updated = tracker.get_improvement_task(_KEY)
    assert updated is not None
    assert updated["status"] == ImprovementTaskStatus.ISSUE_CREATING.value


def test_try_reclaim_stale_issue_creation_fails_when_not_yet_expired(dynamo) -> None:
    _ensure_task()
    tracker.try_claim_new_issue_creation(_KEY, _NOW, 10)
    task = tracker.get_improvement_task(_KEY)
    assert task is not None
    still_within_timeout = _NOW + dt.timedelta(minutes=5)

    ok = tracker.try_reclaim_stale_issue_creation(
        _KEY, task["issue_claimed_at"], still_within_timeout, 10
    )
    assert ok is False


def test_try_reclaim_stale_issue_creation_fails_on_stale_expected_claim_mismatch(dynamo) -> None:
    """2並行実行が同時にreconciliationした場合、先に成功した1件だけがreclaimできる
    (expected_claimed_atの楽観的排他)。"""
    _ensure_task()
    tracker.try_claim_new_issue_creation(_KEY, _NOW, 10)
    task = tracker.get_improvement_task(_KEY)
    assert task is not None
    expired_now = _NOW + dt.timedelta(minutes=30)

    first = tracker.try_reclaim_stale_issue_creation(
        _KEY, task["issue_claimed_at"], expired_now, 10
    )
    second = tracker.try_reclaim_stale_issue_creation(
        _KEY, task["issue_claimed_at"], expired_now, 10
    )
    assert first is True
    assert second is False  # issue_claimed_atが既に更新されているため一致しない


# --- mark_issue_created / mark_issue_creation_failed -----------------------


def test_mark_issue_created_sets_fields_and_clears_claim(dynamo) -> None:
    _ensure_task()
    tracker.try_claim_new_issue_creation(_KEY, _NOW, 10)
    tracker.mark_issue_created(_KEY, 123, "https://github.com/x/y/issues/123", _NOW)

    task = tracker.get_improvement_task(_KEY)
    assert task is not None
    assert task["status"] == ImprovementTaskStatus.ISSUE_CREATED.value
    assert task["github_issue_number"] == 123
    assert "issue_claimed_at" not in task
    assert "issue_claim_expires_at" not in task


def test_mark_issue_created_records_previous_issue_number_on_reopen(dynamo) -> None:
    _ensure_task()
    tracker.try_claim_new_issue_creation(_KEY, _NOW, 10)
    tracker.mark_issue_created(
        _KEY, 456, "https://github.com/x/y/issues/456", _NOW, previous_issue_number=123
    )

    task = tracker.get_improvement_task(_KEY)
    assert task is not None
    assert task["github_issue_number"] == 456
    assert task["previous_github_issue_number"] == 123


def test_mark_issue_creation_failed_releases_claim_for_retry(dynamo) -> None:
    _ensure_task()
    tracker.try_claim_new_issue_creation(_KEY, _NOW, 10)
    tracker.mark_issue_creation_failed(_KEY, _NOW)

    task = tracker.get_improvement_task(_KEY)
    assert task is not None
    assert task["status"] == ImprovementTaskStatus.ISSUE_CREATION_FAILED.value
    assert "issue_claimed_at" not in task
    # 次回週次実行(status<>ISSUE_CREATING)が再claimできること
    assert tracker.try_claim_new_issue_creation(_KEY, _NOW + dt.timedelta(days=7), 10) is True


# --- comment claim ----------------------------------------------------------


def test_try_claim_new_comment_succeeds_when_no_prior_claim(dynamo) -> None:
    _ensure_task()
    assert tracker.try_claim_new_comment(_KEY, "2026-W32", _NOW, 10) is True


def test_try_claim_new_comment_fails_when_already_commented_this_week(dynamo) -> None:
    _ensure_task()
    tracker.try_claim_new_comment(_KEY, "2026-W32", _NOW, 10)
    tracker.mark_comment_posted(_KEY, "2026-W32", _NOW)

    later = _NOW + dt.timedelta(hours=1)
    assert tracker.try_claim_new_comment(_KEY, "2026-W32", later, 10) is False


def test_try_claim_new_comment_allows_new_week_after_previous_completed(dynamo) -> None:
    _ensure_task()
    tracker.try_claim_new_comment(_KEY, "2026-W32", _NOW, 10)
    tracker.mark_comment_posted(_KEY, "2026-W32", _NOW)

    next_week = _NOW + dt.timedelta(days=7)
    assert tracker.try_claim_new_comment(_KEY, "2026-W33", next_week, 10) is True


def test_try_claim_new_comment_fails_while_same_week_claim_active_even_if_stale(dynamo) -> None:
    _ensure_task()
    tracker.try_claim_new_comment(_KEY, "2026-W32", _NOW, 10)
    much_later = _NOW + dt.timedelta(minutes=30)
    # 失効していても、try_claim_new_commentは絶対に奪わない
    assert tracker.try_claim_new_comment(_KEY, "2026-W32", much_later, 10) is False


def test_try_reclaim_stale_comment_succeeds_when_expired(dynamo) -> None:
    _ensure_task()
    tracker.try_claim_new_comment(_KEY, "2026-W32", _NOW, 10)
    task = tracker.get_improvement_task(_KEY)
    assert task is not None
    expired_now = _NOW + dt.timedelta(minutes=30)

    ok = tracker.try_reclaim_stale_comment(
        _KEY, "2026-W32", task["comment_claim_expires_at"], expired_now, 10
    )
    assert ok is True


def test_try_reclaim_stale_comment_fails_when_not_expired(dynamo) -> None:
    _ensure_task()
    tracker.try_claim_new_comment(_KEY, "2026-W32", _NOW, 10)
    task = tracker.get_improvement_task(_KEY)
    assert task is not None
    still_within = _NOW + dt.timedelta(minutes=5)

    ok = tracker.try_reclaim_stale_comment(
        _KEY, "2026-W32", task["comment_claim_expires_at"], still_within, 10
    )
    assert ok is False


def test_mark_comment_posted_clears_claim(dynamo) -> None:
    _ensure_task()
    tracker.try_claim_new_comment(_KEY, "2026-W32", _NOW, 10)
    tracker.mark_comment_posted(_KEY, "2026-W32", _NOW)

    task = tracker.get_improvement_task(_KEY)
    assert task is not None
    assert task["last_commented_review_week"] == "2026-W32"
    assert "comment_claim_review_week" not in task
    assert "comment_claim_expires_at" not in task
