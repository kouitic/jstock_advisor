"""Issue #926: infrastructure/aws/incident_state_tracker.py の起票の抑止(予算・停止・通知の確保)のテスト。

実際の DynamoDB(moto)で、条件つき UpdateItem の原子性・窓の切替・返却・停止の 1 回性・通知の確保を固定する。
LINE の claim / dedup(#502 / #503)・GitHub 起票の claim(#508)とは独立であること、制御用の項目
(`__control__:github_issue`)が fingerprint の行と混ざらないこと、IncidentStateTable に TTL がないこと
(制御項目が勝手に削除されない)も固定する。
"""

from __future__ import annotations

import ast
import datetime as dt
import re
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.notification import incident_issue_guard as guard
from jstock_advisor.infrastructure.aws import incident_state_tracker as tracker

_REGION = "ap-northeast-1"
_FP = "a" * 64
_FP2 = "b" * 64
_NOW = dt.datetime(2026, 10, 12, 9, 0, tzinfo=dt.UTC)
_W1 = "2026-10-12"
_W2 = "2026-10-13"
_REPO = Path(__file__).resolve().parents[2]


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


def _row(fingerprint: str = _FP) -> None:
    """LINE の claim が作る行(status・occurrence_count を持つ)を用意する。"""
    outcome, token = tracker.try_claim(
        fingerprint, _NOW, dt.timedelta(minutes=30), dt.timedelta(minutes=5)
    )
    assert token is not None
    tracker.mark_sent(fingerprint, token, _NOW)


def _state(fingerprint: str = _FP) -> dict:
    state = tracker.get_incident_state(fingerprint)
    assert state is not None
    return state


# --- 予算(fingerprint あたり)-----------------------------------------------------------------


def test_the_fingerprint_budget_allows_exactly_the_limit() -> None:
    _row()
    results = [tracker.try_consume_fingerprint_budget(_FP, _W1, 2) for _ in range(4)]
    assert results == [True, True, False, False]
    assert int(_state()["github_issue_window_created_count"]) == 2


def test_the_fingerprint_budget_resets_in_a_new_window() -> None:
    _row()
    assert tracker.try_consume_fingerprint_budget(_FP, _W1, 1) is True
    assert tracker.try_consume_fingerprint_budget(_FP, _W1, 1) is False
    assert tracker.try_consume_fingerprint_budget(_FP, _W2, 1) is True  # 日付が替わった
    assert _state()["github_issue_window_id"] == _W2
    assert int(_state()["github_issue_window_created_count"]) == 1


def test_the_fingerprint_budgets_are_independent() -> None:
    _row(_FP)
    _row(_FP2)
    assert tracker.try_consume_fingerprint_budget(_FP, _W1, 1) is True
    assert tracker.try_consume_fingerprint_budget(_FP, _W1, 1) is False
    assert tracker.try_consume_fingerprint_budget(_FP2, _W1, 1) is True


def test_the_fingerprint_budget_never_creates_a_row() -> None:
    # 行が無い fingerprint に部分的な行を作ると、以降の LINE の初回 claim が失敗する(#508 の注記)
    assert tracker.try_consume_fingerprint_budget(_FP, _W1, 3) is False
    assert tracker.get_incident_state(_FP) is None


def test_the_fingerprint_budget_keeps_the_line_fields() -> None:
    _row()
    before = {k: v for k, v in _state().items() if not k.startswith("github_issue_")}
    tracker.try_consume_fingerprint_budget(_FP, _W1, 2)
    after = {k: v for k, v in _state().items() if not k.startswith("github_issue_")}
    assert before == after and after["status"] == "SENT"


def test_the_fingerprint_refund_gives_one_back() -> None:
    _row()
    tracker.try_consume_fingerprint_budget(_FP, _W1, 1)
    tracker.refund_fingerprint_budget(_FP, _W1)
    assert int(_state()["github_issue_window_created_count"]) == 0
    assert tracker.try_consume_fingerprint_budget(_FP, _W1, 1) is True


def test_the_fingerprint_refund_never_goes_below_zero_and_ignores_other_windows() -> None:
    _row()
    tracker.refund_fingerprint_budget(_FP, _W1)  # 何も消費していない
    assert "github_issue_window_created_count" not in _state()
    tracker.try_consume_fingerprint_budget(_FP, _W1, 5)
    tracker.refund_fingerprint_budget(_FP, _W2)  # 別の窓の返却は効かない
    assert int(_state()["github_issue_window_created_count"]) == 1
    tracker.refund_fingerprint_budget(_FP, _W1)
    tracker.refund_fingerprint_budget(_FP, _W1)
    assert int(_state()["github_issue_window_created_count"]) == 0


# --- 予算(全体)--------------------------------------------------------------------------------


def test_the_global_budget_allows_exactly_the_limit_across_fingerprints() -> None:
    assert [tracker.try_consume_global_budget(_W1, 3) for _ in range(5)] == [
        True,
        True,
        True,
        False,
        False,
    ]
    control = tracker.read_github_issue_control()
    assert control is not None and int(control["global_created_count"]) == 3


def test_the_global_budget_resets_in_a_new_window() -> None:
    assert tracker.try_consume_global_budget(_W1, 1) is True
    assert tracker.try_consume_global_budget(_W1, 1) is False
    assert tracker.try_consume_global_budget(_W2, 1) is True


def test_the_global_refund_gives_one_back_and_stays_non_negative() -> None:
    tracker.try_consume_global_budget(_W1, 1)
    tracker.refund_global_budget(_W1)
    tracker.refund_global_budget(_W1)
    control = tracker.read_github_issue_control()
    assert control is not None and int(control["global_created_count"]) == 0
    assert tracker.try_consume_global_budget(_W1, 1) is True


# --- 全体の上限を超えた試行の数(自動停止の入力)-----------------------------------------------------


def test_the_global_denied_count_counts_up_and_resets_per_window() -> None:
    assert [tracker.record_global_denied(_W1) for _ in range(3)] == [1, 2, 3]
    assert tracker.record_global_denied(_W2) == 1


def test_the_denied_count_is_independent_of_the_created_count() -> None:
    tracker.try_consume_global_budget(_W1, 5)
    tracker.record_global_denied(_W1)
    control = tracker.read_github_issue_control()
    assert control is not None
    assert int(control["global_created_count"]) == 1 and int(control["global_denied_count"]) == 1


# --- kill switch -----------------------------------------------------------------------------------


def test_there_is_no_control_item_by_default() -> None:
    assert tracker.read_github_issue_control() is None
    assert tracker.is_kill_switch_on(None) is False
    assert tracker.is_kill_switch_on({}) is False


def test_tripping_happens_exactly_once() -> None:
    assert tracker.try_trip_kill_switch(_NOW, guard.KILL_SWITCH_REASON_AUTO) is True
    assert (
        tracker.try_trip_kill_switch(_NOW + dt.timedelta(minutes=1), guard.KILL_SWITCH_REASON_AUTO)
        is False
    )
    control = tracker.read_github_issue_control()
    assert control is not None and tracker.is_kill_switch_on(control)
    assert control["kill_switch_reason"] == "AUTO_GLOBAL_FLOOD"
    assert control["kill_switch_set_at"] == _NOW.isoformat()  # 2 回目は上書きしない


def test_tripping_keeps_the_budget_counters() -> None:
    tracker.try_consume_global_budget(_W1, 5)
    tracker.try_trip_kill_switch(_NOW, guard.KILL_SWITCH_REASON_AUTO)
    control = tracker.read_github_issue_control()
    assert control is not None and int(control["global_created_count"]) == 1


def test_a_manual_stop_written_directly_is_read_as_on() -> None:
    # 運用者が AWS CLI で書く形(運用手順書): 真偽値の属性を true にするだけ
    boto3.resource("dynamodb", region_name=_REGION).Table("jstock-incident_state").put_item(
        Item={
            "fingerprint": guard.CONTROL_KEY,
            "github_issue_kill_switch": True,
            "kill_switch_reason": "MANUAL",
        }
    )
    control = tracker.read_github_issue_control()
    assert tracker.is_kill_switch_on(control)


@pytest.mark.parametrize("value", [False, "true", "TRUE", 1, None])
def test_only_a_boolean_true_stops(value: object) -> None:
    assert tracker.is_kill_switch_on({"github_issue_kill_switch": value}) is False


def test_a_released_switch_can_trip_again() -> None:
    tracker.try_trip_kill_switch(_NOW, guard.KILL_SWITCH_REASON_AUTO)
    boto3.resource("dynamodb", region_name=_REGION).Table("jstock-incident_state").update_item(
        Key={"fingerprint": guard.CONTROL_KEY},
        UpdateExpression="SET github_issue_kill_switch = :f",
        ExpressionAttributeValues={":f": False},
    )
    assert tracker.try_trip_kill_switch(_NOW, guard.KILL_SWITCH_REASON_AUTO) is True


# --- 通知の確保(1 回だけ)---------------------------------------------------------------------------


def test_the_notification_is_claimed_once_after_a_trip() -> None:
    tracker.try_trip_kill_switch(_NOW, guard.KILL_SWITCH_REASON_AUTO)
    assert tracker.try_claim_kill_switch_notification(_NOW) is True
    assert tracker.try_claim_kill_switch_notification(_NOW) is False
    control = tracker.read_github_issue_control()
    assert control is not None and control["kill_switch_notified_at"] == _NOW.isoformat()


def test_the_notification_cannot_be_claimed_without_a_stop() -> None:
    assert tracker.try_claim_kill_switch_notification(_NOW) is False
    tracker.try_consume_global_budget(_W1, 3)  # 制御項目はあるが停止していない
    assert tracker.try_claim_kill_switch_notification(_NOW) is False


def test_a_released_notification_can_be_claimed_again() -> None:
    tracker.try_trip_kill_switch(_NOW, guard.KILL_SWITCH_REASON_AUTO)
    tracker.try_claim_kill_switch_notification(_NOW)
    tracker.release_kill_switch_notification()
    control = tracker.read_github_issue_control()
    assert control is not None and "kill_switch_notified_at" not in control
    assert tracker.try_claim_kill_switch_notification(_NOW) is True


def test_a_new_trip_clears_the_old_notification_mark() -> None:
    tracker.try_trip_kill_switch(_NOW, guard.KILL_SWITCH_REASON_AUTO)
    tracker.try_claim_kill_switch_notification(_NOW)
    boto3.resource("dynamodb", region_name=_REGION).Table("jstock-incident_state").update_item(
        Key={"fingerprint": guard.CONTROL_KEY},
        UpdateExpression="SET github_issue_kill_switch = :f",
        ExpressionAttributeValues={":f": False},
    )
    tracker.try_trip_kill_switch(_NOW + dt.timedelta(days=1), guard.KILL_SWITCH_REASON_AUTO)
    control = tracker.read_github_issue_control()
    assert control is not None and "kill_switch_notified_at" not in control


# --- 抑止の記録 ----------------------------------------------------------------------------------------


def test_recording_a_suppression_counts_it_without_touching_the_claim_state() -> None:
    _row()
    tracker.record_github_issue_suppressed(_FP, guard.DenyReason.MIN_INTERVAL, _NOW)
    tracker.record_github_issue_suppressed(_FP, guard.DenyReason.SAME_OCCURRENCE, _NOW)
    state = _state()
    assert int(state["github_issue_suppressed_count"]) == 2
    assert state["github_issue_last_denied_reason"] == "SAME_OCCURRENCE"
    assert state["github_issue_last_denied_at"] == _NOW.isoformat()
    assert "github_issue_create_status" not in state  # claim の状態は変えない
    assert state["status"] == "SENT"


def test_recording_a_suppression_never_creates_a_row() -> None:
    tracker.record_github_issue_suppressed(_FP, guard.DenyReason.LIMITS_UNDECIDED, _NOW)
    assert tracker.get_incident_state(_FP) is None


def test_marking_a_suppression_after_a_claim_releases_the_claim() -> None:
    _row()
    assert tracker.try_claim_new_github_issue_creation(_FP, _NOW, 10) is True
    tracker.mark_github_issue_suppressed(_FP, guard.DenyReason.GLOBAL_LIMIT, _NOW)
    state = _state()
    assert state["github_issue_create_status"] == "SUPPRESSED"
    assert "github_issue_claimed_at" not in state and "github_issue_claim_expires_at" not in state
    assert int(state["github_issue_suppressed_count"]) == 1
    assert state["github_issue_last_denied_reason"] == "GLOBAL_LIMIT"
    # 解放後は次の occurrence で再び claim できる
    assert tracker.try_claim_new_github_issue_creation(_FP, _NOW, 10) is True


def test_marking_a_suppression_keeps_the_previous_issue_number() -> None:
    _row()
    tracker.mark_github_issue_created(_FP, 42)
    tracker.try_claim_new_github_issue_creation(_FP, _NOW, 10)
    tracker.mark_github_issue_suppressed(_FP, guard.DenyReason.GLOBAL_LIMIT, _NOW)
    assert int(_state()["github_issue_number"]) == 42  # 閉じた Issue の番号を失わない


def test_suppressed_is_a_new_status_value() -> None:
    assert tracker.IncidentGithubIssueStatus.SUPPRESSED.value == "SUPPRESSED"
    assert {s.value for s in tracker.IncidentGithubIssueStatus} == {
        "CREATING",
        "CREATED",
        "CONFIGURATION_ERROR",
        "ISSUE_CREATION_FAILED",
        "SUPPRESSED",
    }


# --- 作成の記録(最小間隔・occurrence gate の入力)------------------------------------------------------


def test_creation_records_when_and_for_which_occurrence() -> None:
    _row()
    tracker.mark_github_issue_created(_FP, 7, created_at=_NOW, occurrence_count=4)
    state = _state()
    assert state["github_issue_last_created_at"] == _NOW.isoformat()
    assert int(state["github_issue_last_created_occurrence"]) == 4
    assert int(state["github_issue_number"]) == 7


def test_creation_without_the_new_arguments_keeps_working() -> None:
    _row()
    tracker.mark_github_issue_created(_FP, 7)  # 既存の呼び出し(復旧の経路)
    state = _state()
    assert int(state["github_issue_number"]) == 7
    assert "github_issue_last_created_at" not in state


def test_creation_with_a_previous_number_still_records_it() -> None:
    _row()
    tracker.mark_github_issue_created(
        _FP, 9, previous_issue_number=7, created_at=_NOW, occurrence_count=2
    )
    state = _state()
    assert (
        int(state["previous_github_issue_number"]) == 7 and int(state["github_issue_number"]) == 9
    )


# --- 制御項目と他のコードの共存(HANAKO の追加確認)---------------------------------------------------


def test_the_control_item_does_not_disturb_the_line_claim_of_any_fingerprint() -> None:
    tracker.try_trip_kill_switch(_NOW, guard.KILL_SWITCH_REASON_AUTO)
    tracker.try_consume_global_budget(_W1, 5)
    outcome, token = tracker.try_claim(_FP, _NOW, dt.timedelta(minutes=30), dt.timedelta(minutes=5))
    assert outcome is tracker.IncidentClaimOutcome.CLAIMED_NEW and token is not None


def test_the_tracker_never_scans_or_queries_the_table() -> None:
    # 同表を全件 Scan / Query する経路は無い(制御項目が想定外の行として集計・走査されない)
    tree = ast.parse(Path(tracker.__file__).read_text(encoding="utf-8"))
    names = {
        (n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", ""))
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert not names & {"scan", "query", "batch_get_item", "list_tables"}


def test_no_other_source_reads_the_incident_state_table() -> None:
    src = _REPO / "src" / "jstock_advisor"
    readers = []
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "incident_state.json" in text or "jstock-incident_state" in text:
            readers.append(path.relative_to(src).as_posix())
    assert readers == ["infrastructure/aws/incident_state_tracker.py"]


def test_the_table_has_no_ttl_so_the_control_item_is_never_expired() -> None:
    text = (_REPO / "infra" / "template.yaml").read_text(encoding="utf-8")
    start = text.index("\n  IncidentStateTable:")
    end = text.index("\n  IncidentNotifierFunction:", start)
    block = text[start:end]
    assert "TimeToLiveSpecification" not in block and "ttl" not in block.lower().replace(
        "settle", ""
    )


def test_the_tracker_sets_no_ttl_attribute() -> None:
    text = Path(tracker.__file__).read_text(encoding="utf-8")
    assert re.search(r"\bttl\b", text) is None
