"""domain/notification/incident_github_issue_message.pyのテスト(Issue #508)。

PUBLIC repositoryへそのまま公開される本文である。#501
(test_issue_501_incident_message.py)と同じ観点で、allowlist外の値が本文に
現れないことを固定する。
"""

from __future__ import annotations

import datetime as dt

import pytest

from jstock_advisor.domain.notification.incident_github_issue_message import (
    IncidentIssueNotice,
    build_incident_comment_body,
    build_incident_issue_body,
    build_incident_issue_title,
    comment_marker,
    issue_marker,
)
from jstock_advisor.domain.notification.incident_message import IncidentJob

_FP = "b" * 64
_NOW = dt.datetime(2026, 9, 25, 0, 3, tzinfo=dt.UTC)  # 09:03 JST


def _notice(**overrides: object) -> IncidentIssueNotice:
    defaults: dict[str, object] = {
        "job": IncidentJob.BUY_CANDIDATES,
        "occurred_at": _NOW,
        "fingerprint": _FP,
        "occurrence_count": 1,
        "failure_stage": "cloudwatch_alarm",
    }
    defaults.update(overrides)
    return IncidentIssueNotice(**defaults)  # type: ignore[arg-type]


# --- 型で締める(禁止する値を受け取れない) --------------------------------------


def test_notice_rejects_non_job_enum() -> None:
    with pytest.raises(TypeError):
        _notice(job="買い候補チェック")


def test_notice_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone"):
        _notice(occurred_at=dt.datetime(2026, 9, 25, 9, 3))  # noqa: DTZ001


def test_notice_rejects_empty_fingerprint() -> None:
    with pytest.raises(ValueError, match="fingerprint"):
        _notice(fingerprint="")


def test_notice_rejects_non_int_occurrence_count() -> None:
    with pytest.raises(TypeError):
        _notice(occurrence_count="1")


def test_notice_rejects_bool_occurrence_count() -> None:
    with pytest.raises(TypeError):
        _notice(occurrence_count=True)


def test_notice_rejects_occurrence_count_below_one() -> None:
    with pytest.raises(ValueError, match="occurrence_count"):
        _notice(occurrence_count=0)


def test_notice_rejects_empty_failure_stage() -> None:
    with pytest.raises(ValueError, match="failure_stage"):
        _notice(failure_stage="")


def test_notice_rejects_bool_failure_count() -> None:
    with pytest.raises(TypeError):
        _notice(failure_count=True)


def test_notice_rejects_negative_failure_count() -> None:
    with pytest.raises(ValueError, match="failure_count"):
        _notice(failure_count=-1)


def test_notice_rejects_non_bool_is_ongoing() -> None:
    with pytest.raises(TypeError):
        _notice(is_ongoing="yes")


# --- allowlist: 禁止する値が本文に現れないこと(反証つき) ------------------------

_FORBIDDEN_VALUES = (
    "arn:aws:lambda:ap-northeast-1:970547364058:function:jstock-advisor-buy-candidates",
    "970547364058",  # account ID
    "req-12345-abcde",  # request ID風
    "Traceback (most recent call last)",  # stack trace風
    "ValueError: invalid literal",  # exception message風
    "sk-secret-abcdef",  # secret風
    "owner-a",  # owner
    "7203",  # 銘柄コード風
)


def test_issue_title_contains_no_forbidden_values() -> None:
    notice = _notice()
    title = build_incident_issue_title(notice)
    for forbidden in _FORBIDDEN_VALUES:
        assert forbidden not in title


def test_issue_body_contains_no_forbidden_values() -> None:
    notice = _notice(failure_count=3, consecutive_days=2, is_ongoing=True)
    body = build_incident_issue_body(notice)
    for forbidden in _FORBIDDEN_VALUES:
        assert forbidden not in body


def test_comment_body_contains_no_forbidden_values() -> None:
    notice = _notice(occurrence_count=2, failure_count=3, is_ongoing=False)
    body = build_incident_comment_body(notice)
    for forbidden in _FORBIDDEN_VALUES:
        assert forbidden not in body


def test_allowlist_check_itself_is_not_vacuous() -> None:
    """★ 検査自体の確認(#501と同じ観点): 禁止する値を実際に本文へ混入させると、
    このテストの手法(単純なin検査)が確実に検知することを固定する。"""
    poisoned_body = build_incident_issue_body(_notice()) + "\narn:aws:iam::970547364058:role/x"
    with pytest.raises(AssertionError):
        for forbidden in _FORBIDDEN_VALUES:
            assert forbidden not in poisoned_body


# --- 本文の内容(allowlistの範囲内の項目が正しく現れること) ----------------------


def test_issue_title_includes_user_facing_job_name() -> None:
    notice = _notice(job=IncidentJob.HOLDINGS_WATCHLIST)
    title = build_incident_issue_title(notice)
    assert "保有株チェック" in title


def test_issue_body_includes_allowlisted_fields() -> None:
    notice = _notice(
        job=IncidentJob.EVALUATION,
        occurrence_count=3,
        failure_stage="cloudwatch_alarm",
        failure_count=5,
        consecutive_days=2,
        is_ongoing=True,
    )

    body = build_incident_issue_body(notice)

    assert "対象: 過去の推奨の評価" in body
    assert "分類: cloudwatch_alarm" in body
    assert "発生時刻: 2026-09-25 09:03 JST" in body
    assert "発生回数: 3回目" in body
    assert "件数: 5件" in body
    assert "連続日数: 2日" in body
    assert "継続中: はい" in body
    assert issue_marker(_FP) in body


def test_issue_body_includes_previous_issue_reference_when_given() -> None:
    notice = _notice()

    body = build_incident_issue_body(notice, previous_issue_number=123)

    assert "Previous issue: #123" in body


def test_issue_body_omits_previous_issue_reference_by_default() -> None:
    notice = _notice()

    body = build_incident_issue_body(notice)

    assert "Previous issue:" not in body


def test_issue_body_omits_optional_fields_when_none() -> None:
    notice = _notice(failure_count=None, consecutive_days=None, is_ongoing=None)

    body = build_incident_issue_body(notice)

    assert "件数:" not in body
    assert "連続日数:" not in body
    assert "継続中:" not in body


def test_comment_body_includes_occurrence_and_marker() -> None:
    notice = _notice(occurrence_count=4, failure_count=1)

    body = build_incident_comment_body(notice)

    assert "再発(4回目)" in body
    assert comment_marker(_FP, 4) in body


def test_issue_marker_and_comment_marker_embed_the_fingerprint_verbatim() -> None:
    assert _FP in issue_marker(_FP)
    assert _FP in comment_marker(_FP, 1)
