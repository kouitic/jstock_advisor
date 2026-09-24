"""infrastructure/aws/incident_state_tracker.pyのGitHub Issue接続部分のテスト
(Issue #508)。

LINEのclaim/dedup(#502/#503。既存の`try_claim`/`mark_sent`/`release_claim`)とは
独立した状態機械であることを、実際のDynamoDB(moto)への読み書きで固定する。
"""

from __future__ import annotations

import datetime as dt

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.infrastructure.aws import incident_state_tracker as tracker

_REGION = "ap-northeast-1"
_FP = "a" * 64
_NOW = dt.datetime(2026, 9, 25, 9, 0, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def incident_state_table(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "jstock")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName="jstock-incident_state",
            KeySchema=[{"AttributeName": "fingerprint", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "fingerprint", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


# --- 新規Issue作成のclaim ------------------------------------------------------


def test_claim_new_github_issue_creation_succeeds_on_first_attempt() -> None:
    claimed = tracker.try_claim_new_github_issue_creation(_FP, _NOW, timeout_minutes=10)

    assert claimed is True
    state = tracker.get_incident_state(_FP)
    assert state["github_issue_create_status"] == "CREATING"
    assert state["github_issue_claimed_at"] == _NOW.isoformat()
    assert state["github_issue_claim_expires_at"] == (_NOW + dt.timedelta(minutes=10)).isoformat()


def test_claim_new_github_issue_creation_fails_while_already_creating() -> None:
    """★ 同時実行の反証: 既にCREATING中の場合、期限に関わらず絶対に奪わない。"""
    tracker.try_claim_new_github_issue_creation(_FP, _NOW, timeout_minutes=10)

    second_attempt = tracker.try_claim_new_github_issue_creation(
        _FP, _NOW + dt.timedelta(minutes=1), timeout_minutes=10
    )

    assert second_attempt is False


def test_claim_new_github_issue_creation_succeeds_after_creation_failed() -> None:
    """一時失敗(ISSUE_CREATION_FAILED)後は、次のoccurrenceで即座に再claimできる
    (claim解放済みのため)。"""
    tracker.try_claim_new_github_issue_creation(_FP, _NOW, timeout_minutes=10)
    tracker.mark_github_issue_creation_failed(_FP)

    reclaimed = tracker.try_claim_new_github_issue_creation(
        _FP, _NOW + dt.timedelta(seconds=1), timeout_minutes=10
    )

    assert reclaimed is True


def test_claim_new_github_issue_creation_succeeds_after_configuration_error() -> None:
    tracker.try_claim_new_github_issue_creation(_FP, _NOW, timeout_minutes=10)
    tracker.mark_github_issue_configuration_error(_FP)

    reclaimed = tracker.try_claim_new_github_issue_creation(
        _FP, _NOW + dt.timedelta(seconds=1), timeout_minutes=10
    )

    assert reclaimed is True


# --- stale takeover ------------------------------------------------------------


def test_reclaim_stale_github_issue_creation_succeeds_after_timeout() -> None:
    tracker.try_claim_new_github_issue_creation(_FP, _NOW, timeout_minutes=10)
    state = tracker.get_incident_state(_FP)
    claimed_at = state["github_issue_claimed_at"]
    past_expiry = _NOW + dt.timedelta(minutes=11)

    reclaimed = tracker.try_reclaim_stale_github_issue_creation(
        _FP, claimed_at, past_expiry, timeout_minutes=10
    )

    assert reclaimed is True
    state = tracker.get_incident_state(_FP)
    assert state["github_issue_claimed_at"] == past_expiry.isoformat()


def test_reclaim_stale_github_issue_creation_fails_before_timeout() -> None:
    """★ 反証: claim_stale未経過ならstale takeoverは失敗する(境界のちょうど手前)。"""
    tracker.try_claim_new_github_issue_creation(_FP, _NOW, timeout_minutes=10)
    state = tracker.get_incident_state(_FP)
    claimed_at = state["github_issue_claimed_at"]
    not_yet_expired = _NOW + dt.timedelta(minutes=9, seconds=59)

    reclaimed = tracker.try_reclaim_stale_github_issue_creation(
        _FP, claimed_at, not_yet_expired, timeout_minutes=10
    )

    assert reclaimed is False


def test_reclaim_stale_github_issue_creation_fails_if_already_reclaimed_by_another_run() -> None:
    """★ 反証: expected_claimed_atが既に更新されていた場合(他実行が先にtakeover
    済み)、楽観的排他により失敗する。"""
    tracker.try_claim_new_github_issue_creation(_FP, _NOW, timeout_minutes=10)
    original_claimed_at = tracker.get_incident_state(_FP)["github_issue_claimed_at"]
    tracker.try_reclaim_stale_github_issue_creation(
        _FP, original_claimed_at, _NOW + dt.timedelta(minutes=11), timeout_minutes=10
    )

    stale_reclaim_with_old_token = tracker.try_reclaim_stale_github_issue_creation(
        _FP, original_claimed_at, _NOW + dt.timedelta(minutes=22), timeout_minutes=10
    )

    assert stale_reclaim_with_old_token is False


# --- Issue作成成功の記録 --------------------------------------------------------


def test_mark_github_issue_created_records_number_and_clears_claim() -> None:
    tracker.try_claim_new_github_issue_creation(_FP, _NOW, timeout_minutes=10)

    tracker.mark_github_issue_created(_FP, 42)

    state = tracker.get_incident_state(_FP)
    assert state["github_issue_create_status"] == "CREATED"
    assert state["github_issue_number"] == 42
    assert "github_issue_claimed_at" not in state
    assert "github_issue_claim_expires_at" not in state
    assert "previous_github_issue_number" not in state


def test_mark_github_issue_created_records_previous_issue_number_when_given() -> None:
    tracker.try_claim_new_github_issue_creation(_FP, _NOW, timeout_minutes=10)

    tracker.mark_github_issue_created(_FP, 99, previous_issue_number=42)

    state = tracker.get_incident_state(_FP)
    assert state["github_issue_number"] == 99
    assert state["previous_github_issue_number"] == 42


# --- コメント追記のclaim(occurrence単位) ---------------------------------------


def test_claim_new_github_comment_succeeds_for_a_new_occurrence() -> None:
    claimed = tracker.try_claim_new_github_comment(_FP, 2, _NOW, timeout_minutes=10)

    assert claimed is True
    state = tracker.get_incident_state(_FP)
    assert state["comment_claim_occurrence_count"] == 2


def test_claim_new_github_comment_fails_for_the_same_occurrence_already_commented() -> None:
    """★ 反証: 同一occurrenceへの二重コメントを防ぐ。"""
    tracker.try_claim_new_github_comment(_FP, 2, _NOW, timeout_minutes=10)
    tracker.mark_github_comment_posted(_FP, 2)

    duplicate_attempt = tracker.try_claim_new_github_comment(
        _FP, 2, _NOW + dt.timedelta(minutes=1), timeout_minutes=10
    )

    assert duplicate_attempt is False


def test_claim_new_github_comment_succeeds_for_a_later_occurrence_after_previous_completed() -> (
    None
):
    tracker.try_claim_new_github_comment(_FP, 2, _NOW, timeout_minutes=10)
    tracker.mark_github_comment_posted(_FP, 2)

    next_occurrence = tracker.try_claim_new_github_comment(
        _FP, 3, _NOW + dt.timedelta(minutes=1), timeout_minutes=10
    )

    assert next_occurrence is True


def test_reclaim_stale_github_comment_succeeds_after_timeout() -> None:
    tracker.try_claim_new_github_comment(_FP, 2, _NOW, timeout_minutes=10)
    state = tracker.get_incident_state(_FP)
    expires_at = state["comment_claim_expires_at"]
    past_expiry = _NOW + dt.timedelta(minutes=11)

    reclaimed = tracker.try_reclaim_stale_github_comment(
        _FP, 2, expires_at, past_expiry, timeout_minutes=10
    )

    assert reclaimed is True


def test_mark_github_comment_posted_clears_claim_and_records_occurrence() -> None:
    tracker.try_claim_new_github_comment(_FP, 2, _NOW, timeout_minutes=10)

    tracker.mark_github_comment_posted(_FP, 2)

    state = tracker.get_incident_state(_FP)
    assert state["last_commented_occurrence_count"] == 2
    assert "comment_claim_occurrence_count" not in state
    assert "comment_claim_expires_at" not in state


# --- LINE側の状態機械との独立性(★最重要) --------------------------------------


def test_github_issue_tracking_does_not_interfere_with_line_claim_state() -> None:
    """★ 最重要の反証: GitHub側の状態遷移が、LINEのclaim/dedup判定(既存の`status`
    フィールド)を一切変更しないこと。逆方向(LINE側の関数がgithub_issue_*を
    変更しないこと)は#503の既存契約(本ファイルの対象外)。
    """
    outcome, claim_token = tracker.try_claim(
        _FP, _NOW, dt.timedelta(minutes=30), dt.timedelta(minutes=5)
    )
    assert outcome == tracker.IncidentClaimOutcome.CLAIMED_NEW
    assert claim_token is not None

    tracker.try_claim_new_github_issue_creation(_FP, _NOW, timeout_minutes=10)
    tracker.mark_github_issue_created(_FP, 42)

    state = tracker.get_incident_state(_FP)
    assert state["status"] == "CLAIMED"  # LINE側はGitHub操作の影響を受けていない
    assert state["claim_token"] == claim_token

    tracker.mark_sent(_FP, claim_token, _NOW)
    state = tracker.get_incident_state(_FP)
    assert state["status"] == "SENT"
    assert state["github_issue_create_status"] == "CREATED"  # GitHub側もLINE操作の影響を受けない
    assert state["github_issue_number"] == 42
