"""Issue #503(#132 X-4): incident_state_tracker.py の claim / stale takeover / dedup 遷移の
テスト(moto。実際の DynamoDB ConditionExpression の挙動を検証する)。
"""

from __future__ import annotations

import datetime as dt

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.infrastructure.aws import incident_state_tracker as tracker

_REGION = "ap-northeast-1"
_FP = "fp-abc123"
_NOW = dt.datetime(2026, 9, 24, 9, 0, tzinfo=dt.UTC)
_WINDOW = dt.timedelta(minutes=30)
_STALE = dt.timedelta(minutes=5)


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


def _claim(now: dt.datetime = _NOW) -> tuple[tracker.IncidentClaimOutcome, str | None]:
    return tracker.try_claim(_FP, now, _WINDOW, _STALE)


# --- 新規 claim ---------------------------------------------------------------


def test_first_signal_is_claimed_new() -> None:
    outcome, token = _claim()

    assert outcome is tracker.IncidentClaimOutcome.CLAIMED_NEW
    assert token is not None
    state = tracker.get_incident_state(_FP)
    assert state is not None
    assert state["status"] == "CLAIMED"
    assert state["claim_token"] == token
    assert int(state["occurrence_count"]) == 1
    assert state["first_seen_at"] == state["claimed_at"] == _NOW.isoformat()


def test_second_signal_while_actively_claimed_is_suppressed() -> None:
    _claim()

    outcome, token = _claim(_NOW + dt.timedelta(seconds=1))

    assert outcome is tracker.IncidentClaimOutcome.SUPPRESSED_ACTIVE_CLAIM
    assert token is None
    assert int(tracker.get_incident_state(_FP)["occurrence_count"]) == 1  # 増えない


def test_concurrent_first_signals_only_one_wins() -> None:
    """★ 2つの実行が同時に同一fingerprintの新規claimを試みても、成功は1回だけ。

    実際の同時実行では両方が `attribute_not_exists(fingerprint)` の PutItem を投げ、
    DynamoDBが片方だけを成功させる。ここでは2回連続で呼ぶことで、1回目が成功させた
    item を2回目が見つけて抑止側へ回る(= まさにその条件付きPutItemが機能した)ことを
    確認する。
    """
    outcome_a, token_a = _claim()
    outcome_b, token_b = _claim(_NOW + dt.timedelta(milliseconds=1))

    assert {outcome_a, outcome_b} == {
        tracker.IncidentClaimOutcome.CLAIMED_NEW,
        tracker.IncidentClaimOutcome.SUPPRESSED_ACTIVE_CLAIM,
    }
    assert (token_a is not None) != (token_b is not None)  # どちらか一方だけ


# --- SENT・dedup window --------------------------------------------------------


def test_mark_sent_transitions_to_sent_and_records_last_notified_at() -> None:
    _, token = _claim()

    ok = tracker.mark_sent(_FP, token, _NOW)

    assert ok is True
    state = tracker.get_incident_state(_FP)
    assert state["status"] == "SENT"
    assert state["last_notified_at"] == _NOW.isoformat()


def test_mark_sent_fails_with_the_wrong_claim_token() -> None:
    _claim()

    ok = tracker.mark_sent(_FP, "not-my-token", _NOW)

    assert ok is False
    assert tracker.get_incident_state(_FP)["status"] == "CLAIMED"  # 遷移していない


def test_signal_within_dedup_window_after_sent_is_suppressed() -> None:
    _, token = _claim()
    tracker.mark_sent(_FP, token, _NOW)

    outcome, new_token = _claim(_NOW + _WINDOW - dt.timedelta(seconds=1))

    assert outcome is tracker.IncidentClaimOutcome.SUPPRESSED_DUPLICATE
    assert new_token is None
    assert tracker.get_incident_state(_FP)["status"] == "SENT"


def test_signal_exactly_at_the_window_boundary_is_not_a_duplicate() -> None:
    """#502 is_duplicate_within_window() と同じ境界(ちょうど window 経過は「重複ではない」側)。"""
    _, token = _claim()
    tracker.mark_sent(_FP, token, _NOW)

    outcome, new_token = _claim(_NOW + _WINDOW)

    assert outcome is tracker.IncidentClaimOutcome.CLAIMED_AFTER_DEDUP_WINDOW
    assert new_token is not None


def test_signal_after_dedup_window_reclaims_and_increments_occurrence() -> None:
    _, token = _claim()
    tracker.mark_sent(_FP, token, _NOW)

    outcome, new_token = _claim(_NOW + _WINDOW + dt.timedelta(minutes=1))

    assert outcome is tracker.IncidentClaimOutcome.CLAIMED_AFTER_DEDUP_WINDOW
    state = tracker.get_incident_state(_FP)
    assert state["status"] == "CLAIMED"
    assert state["claim_token"] == new_token != token
    assert int(state["occurrence_count"]) == 2
    assert state["first_seen_at"] == _NOW.isoformat()  # first_seen_at は変わらない


def test_concurrent_reclaim_after_window_only_one_wins() -> None:
    """★ 1回目が成功した時点で status は SENT→CLAIMED へ変わっている(claimed_at=later)ため、
    2回目は「window 経過後の SENT」ではなく「今まさに claim された CLAIMED(未stale)」を見て
    SUPPRESSED_ACTIVE_CLAIM になる。結果として重複 claim にはならない(occurrence_count が
    二重加算されない)ことが本質であり、2回目の抑止理由が SUPPRESSED_DUPLICATE か
    SUPPRESSED_ACTIVE_CLAIM かは、実行タイミングに依存する非本質的な違いである。
    """
    _, token = _claim()
    tracker.mark_sent(_FP, token, _NOW)
    later = _NOW + _WINDOW + dt.timedelta(minutes=1)

    outcome_a, token_a = _claim(later)
    outcome_b, token_b = _claim(later)

    assert {outcome_a, outcome_b} == {
        tracker.IncidentClaimOutcome.CLAIMED_AFTER_DEDUP_WINDOW,
        tracker.IncidentClaimOutcome.SUPPRESSED_ACTIVE_CLAIM,
    }
    assert int(tracker.get_incident_state(_FP)["occurrence_count"]) == 2  # 二重加算しない


# --- CLAIMED の stale takeover --------------------------------------------------


def test_still_claimed_within_stale_window_is_suppressed() -> None:
    _claim()

    outcome, token = _claim(_NOW + _STALE - dt.timedelta(seconds=1))

    assert outcome is tracker.IncidentClaimOutcome.SUPPRESSED_ACTIVE_CLAIM
    assert token is None


def test_claimed_past_stale_window_is_taken_over_and_increments_occurrence() -> None:
    _, old_token = _claim()

    outcome, new_token = _claim(_NOW + _STALE + dt.timedelta(seconds=1))

    assert outcome is tracker.IncidentClaimOutcome.CLAIMED_STALE_TAKEOVER
    state = tracker.get_incident_state(_FP)
    assert state["claim_token"] == new_token != old_token
    assert int(state["occurrence_count"]) == 2
    assert state["first_seen_at"] == _NOW.isoformat()


def test_stale_boundary_exactly_at_claim_stale_is_takeover_eligible() -> None:
    _claim()

    outcome, _ = _claim(_NOW + _STALE)

    assert outcome is tracker.IncidentClaimOutcome.CLAIMED_STALE_TAKEOVER


def test_concurrent_stale_takeover_only_one_wins() -> None:
    _claim()
    later = _NOW + _STALE + dt.timedelta(seconds=1)

    outcome_a, token_a = _claim(later)
    outcome_b, token_b = _claim(later)

    assert {outcome_a, outcome_b} == {
        tracker.IncidentClaimOutcome.CLAIMED_STALE_TAKEOVER,
        tracker.IncidentClaimOutcome.SUPPRESSED_ACTIVE_CLAIM,
    }
    assert int(tracker.get_incident_state(_FP)["occurrence_count"]) == 2


# --- release_claim(LINE push失敗) ----------------------------------------------


def test_release_claim_for_a_new_claim_deletes_the_item() -> None:
    _, token = _claim()

    tracker.release_claim(_FP, token, is_new=True)

    assert tracker.get_incident_state(_FP) is None
    # 次のretryは初出として即座に再claimできる(occurrence_countは1から)。
    outcome, _ = _claim(_NOW + dt.timedelta(seconds=1))
    assert outcome is tracker.IncidentClaimOutcome.CLAIMED_NEW
    assert int(tracker.get_incident_state(_FP)["occurrence_count"]) == 1


def test_release_claim_for_a_reclaim_keeps_history_but_allows_immediate_retry() -> None:
    """★ is_new=False の release は削除しない(occurrence_count等の履歴を保つ)。
    claim_stale の満了を待たずに、直後のretryが即座にtakeoverできる(baselineの意図)。
    """
    _, old_token = _claim()
    later = _NOW + _STALE + dt.timedelta(seconds=1)
    _, token = _claim(later)  # stale takeover(occurrence_count=2)

    tracker.release_claim(_FP, token, is_new=False)

    state = tracker.get_incident_state(_FP)
    assert state is not None  # 削除されていない(履歴を保持)
    assert int(state["occurrence_count"]) == 2  # 履歴は失われない
    # ★ claimed_atは「実行時点のnow()から見て古い値」ではなく、claim_staleの長さに
    #   依存しない絶対的に古い値(datetime.min)へ書き換わっていること(タイミング依存の
    #   偶然の一致でテストが通ってしまわないよう、直接値を検証する)。
    assert dt.datetime.fromisoformat(state["claimed_at"]).year < 1900

    # claim_stale(5分)がまだ全く経過していない直後のretryでも、即座にtakeoverできる。
    outcome, new_token = _claim(later + dt.timedelta(seconds=1))
    assert outcome is tracker.IncidentClaimOutcome.CLAIMED_STALE_TAKEOVER
    assert new_token != token


def test_release_claim_with_the_wrong_token_does_not_touch_another_executions_claim() -> None:
    """★ 他の実行が既にtakeoverしていたら、古い実行のrelease_claimはそのclaimを壊さない。"""
    _, old_token = _claim()
    later = _NOW + _STALE + dt.timedelta(seconds=1)
    _, new_token = _claim(later)  # 別実行がtakeover済み

    tracker.release_claim(_FP, old_token, is_new=False)  # 古いtokenでrelease(no-op)

    state = tracker.get_incident_state(_FP)
    assert state["claim_token"] == new_token  # 新しいclaimは壊れていない
    assert state["status"] == "CLAIMED"


def test_release_claim_new_with_the_wrong_token_does_not_delete() -> None:
    _claim()  # 別実行のCLAIMED_NEW

    tracker.release_claim(_FP, "not-my-token", is_new=True)

    assert tracker.get_incident_state(_FP) is not None  # 削除されていない
