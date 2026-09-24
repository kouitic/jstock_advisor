"""domain/notification/incident_github_issue_message.pyのテスト(Issue #508)。

PUBLIC repositoryへそのまま公開される本文である。#501
(test_issue_501_incident_message.py)と同じ観点で、allowlist外の値が本文に
現れないことを固定する。
"""

from __future__ import annotations

import datetime as dt

import pytest

from jstock_advisor.domain.notification.incident_github_issue_message import (
    IncidentFailureStage,
    IncidentIssueNotice,
    build_incident_comment_body,
    build_incident_issue_body,
    build_incident_issue_title,
    comment_marker,
    issue_marker,
    resolve_incident_failure_stage,
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
        "failure_stage": IncidentFailureStage.CLOUDWATCH_ALARM,
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


def test_notice_rejects_non_enum_failure_stage() -> None:
    """★ PR #563レビュー指摘(サブちゃん F1)への直接固定: failure_stageは
    `IncidentFailureStage`列挙のみを受理し、生の自由文字列は型で拒否する
    (`job`と同じ締め方)。"""
    with pytest.raises(TypeError, match="failure_stage"):
        _notice(failure_stage="cloudwatch_alarm")  # 生文字列(列挙ではない)


def test_notice_rejects_empty_string_failure_stage() -> None:
    with pytest.raises(TypeError, match="failure_stage"):
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
        failure_stage=IncidentFailureStage.QUEUE_BACKLOG,
        failure_count=5,
        consecutive_days=2,
        is_ongoing=True,
    )

    body = build_incident_issue_body(notice)

    assert "対象: 過去の推奨の評価" in body
    assert "分類: 処理キューの滞留" in body
    assert "発生時刻: 2026-09-25 09:03 JST" in body
    assert "発生回数: 3回目" in body
    assert "件数: 5件" in body
    assert "連続日数: 2日" in body
    assert "継続中: はい" in body
    assert issue_marker(_FP) in body


# --- resolve_incident_failure_stage(): 既知集合以外を出さない(★F1対応) --------


def test_resolve_incident_failure_stage_maps_all_known_internal_values() -> None:
    assert (
        resolve_incident_failure_stage("cloudwatch_alarm") is IncidentFailureStage.CLOUDWATCH_ALARM
    )
    assert resolve_incident_failure_stage("SCHEDULE") is IncidentFailureStage.SCHEDULE
    assert resolve_incident_failure_stage("UNIVERSE_LOAD") is IncidentFailureStage.UNIVERSE_LOAD
    assert resolve_incident_failure_stage("QUEUE_BACKLOG") is IncidentFailureStage.QUEUE_BACKLOG
    assert resolve_incident_failure_stage("WATCHLIST_SIZE") is IncidentFailureStage.WATCHLIST_SIZE


def test_resolve_incident_failure_stage_falls_back_to_other_for_unknown_string() -> None:
    """★ F1の直接固定: 未知の内部文字列(将来の新設含む)は、値そのものではなく
    OTHERへ丸められ、生文字列はPUBLIC repositoryへ一切現れない。"""
    resolved = resolve_incident_failure_stage(
        "some-未来-new_stage-arn:aws:iam::970547364058:role/x"
    )
    assert resolved is IncidentFailureStage.OTHER
    assert "arn:aws" not in resolved.value
    assert "970547364058" not in resolved.value


def test_resolve_incident_failure_stage_falls_back_to_other_for_non_string() -> None:
    assert resolve_incident_failure_stage(None) is IncidentFailureStage.OTHER
    assert resolve_incident_failure_stage(123) is IncidentFailureStage.OTHER


def test_resolve_incident_failure_stage_does_not_partial_match() -> None:
    """`resolve_incident_job`の部分一致拒否と同じ観点。"""
    assert resolve_incident_failure_stage("SCHEDULE_EXTRA") is IncidentFailureStage.OTHER
    assert resolve_incident_failure_stage("X_SCHEDULE") is IncidentFailureStage.OTHER


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
