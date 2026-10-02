"""Issue #665(HF-0): 共通Handled Failure契約のテスト。

確認すること(Issue本文のAcceptance Criteria HF0-AC1〜AC7 / Test Plan T3〜T10):

    T3  failure_class="HANDLED_FAILURE"のenvelopeがIncidentSignalへ正しく反映される
    T4  HANDLED_FAILUREでもLINE送信は既存どおり実行される
    T5  HANDLED_FAILUREのときgithub_safe_to_attemptがFalseになり、GitHub Issue
        作成が試行されない
    T6  同一fingerprintの再発が既存のdedup機構で抑止される(HANDLED_FAILURE固有の
        新規ロジックではなく、既存機構がそのまま効く)
    T8  incident_envelope_publisher.pyのallowlist fail-closedを確認
    T9  failure_class省略(既定UNHANDLED_FAILURE)で、既存のUNHANDLED経路
        (GitHub Issue試行を含む)が一切変わらないことを確認(回帰確認)
    T10 既存のCloudWatch Alarm由来の信号は本Issueの変更の影響を受けない
        (failure_classキー自体を持たないため、常に既定値UNHANDLED_FAILUREとなる)
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.notification.incident_signal import FailureClass, IncidentSignal
from jstock_advisor.lambda_handlers import incident_notifier_handler as handler_module
from jstock_advisor.services.incident_envelope_publisher import (
    INCIDENT_ENVELOPE_ALLOWLIST,
    publish_incident_envelope,
)

_REGION = "ap-northeast-1"
_NOW = dt.datetime(2026, 10, 2, 0, 0, tzinfo=dt.UTC)


class _RecordingLineClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)


def _internal_message(
    *,
    source: str = "buy_candidates",
    job_name: str = "buy-candidates",
    failure_stage: str = "CANDIDATE_ANALYSIS",
    failure_type: str = "UNEXPECTED_EXCEPTION",
    reason_code: str = "BUY_CANDIDATES_ANALYSIS_FAILED",
    occurred_at: str = "2026-10-02T00:00:00+00:00",
    failure_count: int | None = 1,
    failure_class: str | None = "HANDLED_FAILURE",
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "source": source,
        "job_name": job_name,
        "failure_stage": failure_stage,
        "failure_type": failure_type,
        "reason_code": reason_code,
        "occurred_at": occurred_at,
    }
    if failure_count is not None:
        message["failure_count"] = failure_count
    if failure_class is not None:
        message["failure_class"] = failure_class
    return message


def _sns_event(*messages: dict[str, Any]) -> dict[str, Any]:
    return {"Records": [{"Sns": {"Message": json.dumps(m)}} for m in messages]}


@pytest.fixture(autouse=True)
def incident_state_table(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "jstock")
    # github_issue_creation_enabled既定falseのまま(GITHUB_APP_SECRET_ARN未設定)
    monkeypatch.delenv("GITHUB_APP_SECRET_ARN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName="jstock-incident_state",
            KeySchema=[{"AttributeName": "fingerprint", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "fingerprint", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


@pytest.fixture
def recording_line_client(monkeypatch: pytest.MonkeyPatch) -> _RecordingLineClient:
    fake = _RecordingLineClient()
    monkeypatch.setattr(handler_module, "build_live_line_client_from_env", lambda: fake)
    return fake


# --- T3: 正規化の確認 ---------------------------------------------------------------


def test_failure_class_handled_failure_is_normalized_correctly() -> None:
    message = _internal_message(failure_class="HANDLED_FAILURE")
    signal = handler_module._normalize_internal_message(message, _NOW)
    assert signal.failure_class is FailureClass.HANDLED_FAILURE


def test_failure_class_omitted_defaults_to_unhandled_failure() -> None:
    message = _internal_message(failure_class=None)
    signal = handler_module._normalize_internal_message(message, _NOW)
    assert signal.failure_class is FailureClass.UNHANDLED_FAILURE


def test_failure_class_unknown_value_fails_safe_to_unhandled_failure() -> None:
    """#665設計§7: タイプミス等の未知の値は、実害の小さい方向(UNHANDLEDへfallback)へ倒す。"""
    message = _internal_message(failure_class="HANDLED_FAILURE_TYPO")
    signal = handler_module._normalize_internal_message(message, _NOW)
    assert signal.failure_class is FailureClass.UNHANDLED_FAILURE


def test_incident_signal_rejects_non_failure_class_value() -> None:
    with pytest.raises(TypeError, match="failure_class"):
        IncidentSignal(
            source="s",
            job_name="j",
            failure_stage="fs",
            failure_type="ft",
            error_type="et",
            error_message="em",
            occurred_at=_NOW,
            failure_class="HANDLED_FAILURE",  # type: ignore[arg-type]
        )


# --- T4/T5: HANDLED_FAILUREはLINE通知するがGitHub Issueは作らない ------------------------


def test_handled_failure_sends_line_but_skips_github_issue(
    recording_line_client: _RecordingLineClient,
) -> None:
    message = _internal_message(failure_class="HANDLED_FAILURE")

    result = handler_module.handler(_sns_event(message), None)

    assert result == {"processed": 1}
    assert len(recording_line_client.sent) == 1  # T4: LINEは既存どおり送信される
    from jstock_advisor.infrastructure.aws import incident_state_tracker as tracker

    fingerprint = handler_module.compute_fingerprint(
        handler_module._build_fingerprint_input(
            handler_module._normalize_internal_message(message, _NOW)
        )
    )
    state = tracker.get_incident_state(fingerprint)
    assert state["status"] == "SENT"
    # T5: github_issue_create_statusが記録されていない = 試行されていない
    assert "github_issue_create_status" not in state


# --- T9: failure_class省略時の回帰確認(既存UNHANDLED経路は変わらない) -----------------


def test_unhandled_failure_still_attempts_github_issue_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    recording_line_client: _RecordingLineClient,
) -> None:
    """T9: issue_creation_enabled=trueかつfailure_class省略(既定UNHANDLED_FAILURE)
    であれば、GitHub Issue作成が試行されること(実際のAPI呼び出し自体はテスト
    対象外。github_safe_to_attemptがTrueのまま通ること=既存のGithub処理関数が
    呼ばれることを、_attempt_github_issueのモンキーパッチで確認する)。
    """
    from jstock_advisor.config.loader import load_config

    real_config = load_config()
    enabled_incident_notification = real_config.incident_notification.model_copy(
        update={"issue_creation_enabled": True}
    )
    enabled_config = real_config.model_copy(
        update={"incident_notification": enabled_incident_notification}
    )
    monkeypatch.setattr(handler_module, "load_config", lambda: enabled_config)

    attempted: list[Any] = []
    monkeypatch.setattr(
        handler_module, "_attempt_github_issue", lambda *a, **kw: attempted.append(a)
    )

    message = _internal_message(failure_class=None)  # 省略 = 既定UNHANDLED_FAILURE
    result = handler_module.handler(_sns_event(message), None)

    assert result == {"processed": 1}
    assert len(attempted) == 1  # UNHANDLED_FAILUREではGitHub側が試行される


def test_handled_failure_does_not_attempt_github_issue_even_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    recording_line_client: _RecordingLineClient,
) -> None:
    """T5の裏付け: issue_creation_enabled=trueであっても、failure_class=
    HANDLED_FAILUREならGitHub側の処理関数自体が呼ばれないこと。"""
    from jstock_advisor.config.loader import load_config

    real_config = load_config()
    enabled_incident_notification = real_config.incident_notification.model_copy(
        update={"issue_creation_enabled": True}
    )
    enabled_config = real_config.model_copy(
        update={"incident_notification": enabled_incident_notification}
    )
    monkeypatch.setattr(handler_module, "load_config", lambda: enabled_config)

    attempted: list[Any] = []
    monkeypatch.setattr(
        handler_module, "_attempt_github_issue", lambda *a, **kw: attempted.append(a)
    )

    message = _internal_message(failure_class="HANDLED_FAILURE")
    result = handler_module.handler(_sns_event(message), None)

    assert result == {"processed": 1}
    assert attempted == []  # HANDLED_FAILUREでは一度も呼ばれない


# --- T10: CloudWatch Alarm由来は常にUNHANDLED_FAILURE(回帰確認) -----------------------


def test_cloudwatch_alarm_signal_is_always_unhandled_failure() -> None:
    alarm_message = {
        "AlarmName": "jstock-advisor-evaluation-errors",
        "NewStateValue": "ALARM",
        "NewStateReason": "Threshold Crossed",
        "StateChangeTime": "2026-10-02T00:00:00.000+0000",
        "Trigger": {
            "MetricName": "Errors",
            "Namespace": "AWS/Lambda",
            "Dimensions": [{"name": "FunctionName", "value": "jstock-advisor-evaluation"}],
        },
    }
    signal = handler_module._normalize_alarm_message(alarm_message, _NOW)
    assert signal.failure_class is FailureClass.UNHANDLED_FAILURE


# --- T6: dedupは既存機構がそのまま効く(新規ロジックではない) --------------------------


def test_handled_failure_recurrence_within_dedup_window_sends_line_once(
    recording_line_client: _RecordingLineClient,
) -> None:
    message = _internal_message(failure_class="HANDLED_FAILURE")

    handler_module.handler(_sns_event(message), None)
    handler_module.handler(_sns_event(message), None)  # 同一fingerprint、dedup window内

    assert len(recording_line_client.sent) == 1  # 2回目は抑止される(既存#503契約)


# --- T8: incident_envelope_publisher.pyのallowlist fail-closed -----------------------


def test_publish_incident_envelope_rejects_non_allowlisted_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INCIDENT_NOTIFICATION_TOPIC_ARN", "arn:aws:sns:ap-northeast-1:0:topic")
    envelope = {
        "source": "buy_candidates",
        "job_name": "buy-candidates",
        "failure_stage": "CANDIDATE_ANALYSIS",
        "failure_type": "UNEXPECTED_EXCEPTION",
        "reason_code": "X",
        "occurred_at": "2026-10-02T00:00:00+00:00",
        "stock_code": "1234",  # allowlist外
    }
    with pytest.raises(ValueError, match="non-allowlisted"):
        publish_incident_envelope(envelope)


def test_publish_incident_envelope_allows_failure_class_key() -> None:
    assert "failure_class" in INCIDENT_ENVELOPE_ALLOWLIST


@mock_aws
def test_publish_incident_envelope_publishes_allowlisted_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    sns = boto3.client("sns", region_name=_REGION)
    topic_arn = sns.create_topic(Name="jstock-incident-notifications-test")["TopicArn"]
    monkeypatch.setenv("INCIDENT_NOTIFICATION_TOPIC_ARN", topic_arn)
    envelope = {
        "source": "buy_candidates",
        "job_name": "buy-candidates",
        "failure_stage": "CANDIDATE_ANALYSIS",
        "failure_type": "UNEXPECTED_EXCEPTION",
        "reason_code": "X",
        "occurred_at": "2026-10-02T00:00:00+00:00",
        "failure_class": "HANDLED_FAILURE",
    }
    publish_incident_envelope(envelope)  # 例外を投げないことを確認する
