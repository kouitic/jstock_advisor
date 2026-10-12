"""Issue #926: incident の GitHub Issue 起票の抑止(純粋な判定)の契約テスト。

層 1 の事前確認(上限が未決定か・同じ occurrence か・最小間隔)、窓の id(JST の日付)、自動停止の
LINE 文面、制御項目の key。DynamoDB・GitHub・時計に触れない純粋な関数と型だけ。
"""

from __future__ import annotations

import ast
import datetime as dt
import re
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.notification import incident_issue_guard as guard
from jstock_advisor.domain.notification.incident_issue_limits import (
    IncidentIssueLimits,
    undecided_limits,
)

_UTC = dt.UTC
_NOW = dt.datetime(2026, 10, 12, 9, 0, tzinfo=_UTC)


def decided(**over: int | None) -> IncidentIssueLimits:
    values: dict[str, int | None] = {
        "new_issue_min_interval_minutes": 360,
        "new_issue_limit_per_fingerprint": 2,
        "new_issue_limit_global": 5,
        "auto_stop_denied_threshold": 10,
    }
    values.update(over)
    return IncidentIssueLimits(**values)  # type: ignore[arg-type]


def state(
    created_minutes_ago: int | None = None, occurrence: int | None = None
) -> dict[str, object]:
    out: dict[str, object] = {"occurrence_count": Decimal(5)}
    if created_minutes_ago is not None:
        out["github_issue_last_created_at"] = (
            _NOW - dt.timedelta(minutes=created_minutes_ago)
        ).isoformat()
    if occurrence is not None:
        out["github_issue_last_created_occurrence"] = Decimal(
            occurrence
        )  # DynamoDB の数値は Decimal
    return out


def check(limits: IncidentIssueLimits, st: dict[str, object] | None, occurrence: int, now=_NOW):
    return guard.precheck_new_issue(limits, st, occurrence, now)


# --- 事前確認: 上限が未決定 ------------------------------------------------------------------


def test_undecided_limits_deny_every_new_issue() -> None:
    assert check(undecided_limits(), None, 1) is guard.DenyReason.LIMITS_UNDECIDED


@pytest.mark.parametrize(
    "name",
    [
        "new_issue_min_interval_minutes",
        "new_issue_limit_per_fingerprint",
        "new_issue_limit_global",
        "auto_stop_denied_threshold",
    ],
)
def test_one_undecided_value_alone_denies(name: str) -> None:
    assert check(decided(**{name: None}), None, 1) is guard.DenyReason.LIMITS_UNDECIDED


def test_a_first_occurrence_of_a_new_fingerprint_passes_when_decided() -> None:
    assert check(decided(), None, 1) is None
    assert check(decided(), state(), 1) is None  # 前回作成の記録がない行


# --- 事前確認: occurrence gate -----------------------------------------------------------------


def test_the_same_occurrence_is_not_created_twice() -> None:
    assert (
        check(decided(), state(created_minutes_ago=1000, occurrence=3), 3)
        is guard.DenyReason.SAME_OCCURRENCE
    )


def test_an_older_occurrence_is_not_created_either() -> None:
    assert (
        check(decided(), state(created_minutes_ago=1000, occurrence=3), 2)
        is guard.DenyReason.SAME_OCCURRENCE
    )


def test_a_newer_occurrence_passes_the_occurrence_gate() -> None:
    assert check(decided(), state(created_minutes_ago=1000, occurrence=3), 4) is None


def test_decimal_and_int_occurrences_compare_the_same() -> None:
    assert (
        check(decided(), state(created_minutes_ago=1000, occurrence=3), Decimal(3))
        is guard.DenyReason.SAME_OCCURRENCE
    )  # type: ignore[arg-type]


# --- 事前確認: 最小間隔 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("minutes_ago", "expected"),
    [
        (0, guard.DenyReason.MIN_INTERVAL),
        (359, guard.DenyReason.MIN_INTERVAL),
        (360, None),  # ちょうど最小間隔は作ってよい(境界の連続性)
        (361, None),
    ],
)
def test_the_minimum_interval_boundary(minutes_ago: int, expected: guard.DenyReason | None) -> None:
    assert check(decided(), state(created_minutes_ago=minutes_ago, occurrence=1), 2) is expected


def test_a_future_created_at_is_treated_as_within_the_interval() -> None:
    # 時計のずれで『前回作成』が未来にあるときは、作らない側へ倒す
    assert (
        check(decided(), state(created_minutes_ago=-30, occurrence=1), 2)
        is guard.DenyReason.MIN_INTERVAL
    )


def test_an_unparsable_created_at_is_fail_closed() -> None:
    st = state(occurrence=1)
    st["github_issue_last_created_at"] = "not a time"
    assert check(decided(), st, 2) is guard.DenyReason.MIN_INTERVAL


def test_the_interval_follows_the_configured_minutes() -> None:
    assert (
        check(
            decided(new_issue_min_interval_minutes=10),
            state(created_minutes_ago=9, occurrence=1),
            2,
        )
        is guard.DenyReason.MIN_INTERVAL
    )
    assert (
        check(
            decided(new_issue_min_interval_minutes=10),
            state(created_minutes_ago=10, occurrence=1),
            2,
        )
        is None
    )


def test_the_order_of_the_reasons_is_fixed() -> None:
    both = state(created_minutes_ago=1, occurrence=3)
    assert check(decided(), both, 3) is guard.DenyReason.SAME_OCCURRENCE  # occurrence が先
    assert check(decided(), both, 4) is guard.DenyReason.MIN_INTERVAL
    assert check(undecided_limits(), both, 3) is guard.DenyReason.LIMITS_UNDECIDED  # 未決定が最初


def test_the_check_does_not_mutate_the_state() -> None:
    st = state(created_minutes_ago=1, occurrence=3)
    before = dict(st)
    check(decided(), st, 4)
    assert st == before


# --- 窓の id(JST の日付)-------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        (dt.datetime(2026, 10, 12, 14, 59, 59, tzinfo=_UTC), "2026-10-12"),  # JST 23:59:59
        (dt.datetime(2026, 10, 12, 15, 0, 0, tzinfo=_UTC), "2026-10-13"),  # JST 00:00:00
        (dt.datetime(2026, 10, 12, 0, 0, 0, tzinfo=_UTC), "2026-10-12"),
        (dt.datetime(2026, 12, 31, 15, 0, 0, tzinfo=_UTC), "2027-01-01"),
    ],
)
def test_the_window_id_is_the_jst_date(utc: dt.datetime, expected: str) -> None:
    assert guard.window_id(utc) == expected


def test_the_window_id_is_the_same_for_the_same_instant_in_any_timezone() -> None:
    jst = dt.timezone(dt.timedelta(hours=9))
    instant = dt.datetime(2026, 10, 12, 15, 0, 0, tzinfo=_UTC)
    assert guard.window_id(instant) == guard.window_id(instant.astimezone(jst))


def test_a_naive_time_is_rejected() -> None:
    with pytest.raises(ValueError):
        guard.window_id(dt.datetime(2026, 10, 12, 9, 0))


# --- 制御項目の key と理由の語彙 -----------------------------------------------------------------


def test_the_control_key_can_never_be_a_fingerprint() -> None:
    assert guard.CONTROL_KEY == "__control__:github_issue"
    assert re.fullmatch(r"[0-9a-f]{64}", guard.CONTROL_KEY) is None


def test_the_kill_switch_reasons_are_fixed() -> None:
    assert guard.KILL_SWITCH_REASON_MANUAL == "MANUAL"
    assert guard.KILL_SWITCH_REASON_AUTO == "AUTO_GLOBAL_FLOOD"


def test_the_deny_reasons_are_fixed() -> None:
    assert {r.value for r in guard.DenyReason} == {
        "LIMITS_UNDECIDED",
        "SAME_OCCURRENCE",
        "MIN_INTERVAL",
        "FINGERPRINT_LIMIT",
        "GLOBAL_LIMIT",
    }


def test_the_outcome_defaults_to_no_notification() -> None:
    assert guard.IssueGuardOutcome().auto_stop_notification_due is False
    assert guard.IssueGuardOutcome(auto_stop_notification_due=True).auto_stop_notification_due


# --- 自動停止の LINE 文面
# --------------------------------------------------------------------------


def test_the_auto_stop_message_is_plain_and_carries_no_identifier() -> None:
    text = guard.build_auto_stop_message()
    assert text and len(text) < 500
    assert "自動" in text and "LINE" in text
    for forbidden in ("http", "arn:", "github.com", "#", "fingerprint"):
        assert forbidden not in text.lower(), forbidden
    assert re.search(r"[0-9a-f]{16,}", text) is None  # fingerprint のような長い 16 進を含めない
    assert not any(ch.isdigit() for ch in text)  # 件数・番号を載せない


def test_the_auto_stop_message_is_deterministic() -> None:
    assert guard.build_auto_stop_message() == guard.build_auto_stop_message()


# --- 純粋性(AST)
# -------------------------------------------------------------------------------------


def test_the_module_is_pure() -> None:
    path = Path(guard.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert not any(
        m.startswith(("boto3", "botocore", "jstock_advisor.infrastructure")) for m in imported
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert name not in {"now", "utcnow", "today", "open", "print", "float"}, name
        if isinstance(node, ast.Constant):
            assert not isinstance(node.value, float)


# --- 防御の分岐(事前確認を通った後は到達しないが、単体で fail-closed であること)
# -----------------------------------------------------------------------------


def test_an_undecided_interval_is_read_as_inside_the_interval() -> None:
    assert guard._within_interval(_NOW.isoformat(), _NOW, undecided_limits()) is True
