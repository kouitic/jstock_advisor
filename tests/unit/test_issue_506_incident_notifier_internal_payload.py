"""Issue #506(#132 O-1): incident_notifier_handler.py がInternal structured incident
payload(reconciler等)を、CloudWatch Alarm経由と同一の共通処理(fingerprint/claim/
builder/LINE)へ流すことの確認。USER決定(#506 issuecomment-5805278274)。

確認すること:

    1 Internal payloadがCloudWatch Alarm payloadと正しく区別され、IncidentSignalへ
      正規化されること
    2 正規化後は、Alarm経路と完全に同じ関数(_process_signal)で処理され、二重実装がないこと
    3 Internal payloadのfailure_count/consecutive_days/is_ongoingが、そのまま
      IncidentNoticeへ伝わり、本文へ反映されること(occurrence_countで上書きされない)
    4 allowlist外のキー(stock_code等)を含めても、それらはIncidentSignalへ運ばれない
      (型で締める。IncidentSignalが対応するフィールドを持たない)
    5 reason_codeがfingerprintのerror_type/error_messageとして使われ、悪化(failure_count
      の増加)だけではfingerprintが変わらないこと(#502のnormalize_error_signatureの
      数字列正規化と合わせて、日次の再通知が同一incidentとして扱われることを保証する)
    6 必須フィールド欠落時にfail-closeする(自由文を受け取らない)
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.notification.incident_fingerprint import compute_fingerprint
from jstock_advisor.domain.notification.incident_signal import IncidentSignal
from jstock_advisor.lambda_handlers import incident_notifier_handler as handler_module

_REGION = "ap-northeast-1"


class _RecordingLineClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)


def _internal_message(
    *,
    source: str = "watchlist_reconciler",
    job_name: str = "watchlist-dispatcher",
    failure_stage: str = "SCHEDULE",
    failure_type: str = "MISSED_SCHEDULE",
    reason_code: str = "WATCHLIST_MISSED_SCHEDULE",
    occurred_at: str = "2026-09-24T08:30:00+00:00",
    failure_count: int | None = 1,
    consecutive_days: int | None = None,
    is_ongoing: bool | None = True,
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
    if consecutive_days is not None:
        message["consecutive_days"] = consecutive_days
    if is_ongoing is not None:
        message["is_ongoing"] = is_ongoing
    return message


def _sns_event(*messages: dict[str, Any]) -> dict[str, Any]:
    return {"Records": [{"Sns": {"Message": json.dumps(m)}} for m in messages]}


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


@pytest.fixture
def recording_line_client(monkeypatch: pytest.MonkeyPatch) -> _RecordingLineClient:
    fake = _RecordingLineClient()
    monkeypatch.setattr(handler_module, "build_live_line_client_from_env", lambda: fake)
    return fake


# --- 1・2: 区別・正規化・共通処理 -------------------------------------------------


def test_internal_payload_is_recognized_and_not_treated_as_an_alarm() -> None:
    message = _internal_message()
    assert handler_module._is_internal_payload(message) is True

    alarm_message = {
        "AlarmName": "x",
        "Trigger": {"MetricName": "Errors", "Dimensions": []},
    }
    assert handler_module._is_internal_payload(alarm_message) is False


def test_internal_payload_normalizes_to_incident_signal() -> None:
    message = _internal_message()
    now = dt.datetime.now(dt.UTC)

    signal = handler_module._normalize(message, now)

    assert isinstance(signal, IncidentSignal)
    assert signal.source == "watchlist_reconciler"
    assert signal.job_name == "watchlist-dispatcher"
    assert signal.failure_stage == "SCHEDULE"
    assert signal.failure_type == "MISSED_SCHEDULE"
    assert signal.error_type == "WATCHLIST_MISSED_SCHEDULE"
    assert signal.failure_count == 1
    assert signal.is_ongoing is True


def test_internal_payload_reaches_line_via_the_same_handler_path(
    recording_line_client: _RecordingLineClient,
) -> None:
    """★ Alarm経路と同じhandler()・同じ_process_signal()を通ることの直接確認
    (二重実装〔internal専用のLINE送信コード〕が無いことを、実際にLINEへ届くことで固定する)。
    """
    message = _internal_message()

    result = handler_module.handler(_sns_event(message), None)

    assert result == {"processed": 1}
    assert len(recording_line_client.sent) == 1
    assert "対象: ウォッチリスト自動追加" in recording_line_client.sent[0]


# --- 3: failure_count/consecutive_days/is_ongoingがoccurrence_countで上書きされない ---


def test_internal_payload_failure_count_is_not_overridden_by_occurrence_count(
    recording_line_client: _RecordingLineClient,
) -> None:
    message = _internal_message(failure_count=3, consecutive_days=3, is_ongoing=True)

    handler_module.handler(_sns_event(message), None)

    text = recording_line_client.sent[0]
    assert "件数: 3件" in text
    assert "連続日数: 3日" in text
    assert "継続中: はい" in text


def test_alarm_path_still_uses_occurrence_count_unchanged(
    recording_line_client: _RecordingLineClient,
) -> None:
    """★ 既存のAlarm経路の挙動(occurrence_countをfailure_countへ使う)が変わっていないこと。"""
    alarm_message = {
        "AlarmName": "jstock-advisor-evaluation-errors",
        "NewStateValue": "ALARM",
        "NewStateReason": "Threshold Crossed",
        "StateChangeTime": "2026-09-24T09:00:00.000+0000",
        "Trigger": {
            "MetricName": "Errors",
            "Dimensions": [{"name": "FunctionName", "value": "jstock-advisor-evaluation"}],
        },
    }

    handler_module.handler(_sns_event(alarm_message), None)

    assert "件数: 1件" in recording_line_client.sent[0]
    assert "連続日数" not in recording_line_client.sent[0]
    assert "継続中" not in recording_line_client.sent[0]


# --- 4: allowlist外のキーはIncidentSignalへ運ばれない -----------------------------


def test_disallowed_keys_do_not_survive_normalization() -> None:
    message = _internal_message()
    message["stock_code"] = "7203"  # 架空の混入(型で締めることを確認するだけで実データではない)
    message["owner"] = "owner-a"
    message["stack_trace"] = "Traceback ..."

    now = dt.datetime.now(dt.UTC)
    signal = handler_module._normalize(message, now)

    for forbidden in ("stock_code", "owner", "stack_trace"):
        assert not hasattr(signal, forbidden)
    assert "7203" not in str(signal)
    assert "owner-a" not in str(signal)


# --- 5: reason_codeがfingerprintの識別を担い、件数増加だけでは変わらない -------------


def test_worsening_failure_count_does_not_change_the_fingerprint() -> None:
    now = dt.datetime.now(dt.UTC)
    a = compute_fingerprint(
        handler_module._build_fingerprint_input(
            handler_module._normalize(_internal_message(failure_count=3), now)
        )
    )
    b = compute_fingerprint(
        handler_module._build_fingerprint_input(
            handler_module._normalize(_internal_message(failure_count=10), now)
        )
    )
    assert a == b


def test_different_reason_code_is_a_different_fingerprint() -> None:
    now = dt.datetime.now(dt.UTC)
    a = compute_fingerprint(
        handler_module._build_fingerprint_input(
            handler_module._normalize(
                _internal_message(reason_code="WATCHLIST_MISSED_SCHEDULE"), now
            )
        )
    )
    b = compute_fingerprint(
        handler_module._build_fingerprint_input(
            handler_module._normalize(
                _internal_message(reason_code="WATCHLIST_UNIVERSE_LOAD_FAILURE"), now
            )
        )
    )
    assert a != b


def test_internal_and_alarm_sources_for_the_same_job_are_different_fingerprints() -> None:
    """★ sourceはfingerprintに混ぜない設計だが、failure_stage/error_typeが異なるため
    (Alarm経路のfailure_stage="cloudwatch_alarm" / error_type="CloudWatchAlarm"に対し、
    internal経路はreason_codeをerror_typeにする)、実際には別のfingerprintになることを
    確認する(同じjob_nameでも発生源によって根本原因は別であるため、丸めてはならない)。
    """
    now = dt.datetime.now(dt.UTC)
    internal_fp = compute_fingerprint(
        handler_module._build_fingerprint_input(
            handler_module._normalize(_internal_message(job_name="jstock-advisor-evaluation"), now)
        )
    )
    alarm_signal = handler_module._normalize_alarm_message(
        {
            "AlarmName": "jstock-advisor-evaluation-errors",
            "Trigger": {
                "MetricName": "Errors",
                "Dimensions": [{"name": "FunctionName", "value": "jstock-advisor-evaluation"}],
            },
        },
        now,
    )
    alarm_fp = compute_fingerprint(handler_module._build_fingerprint_input(alarm_signal))
    assert internal_fp != alarm_fp


# --- 6: 必須フィールド欠落はfail-close -------------------------------------------


@pytest.mark.parametrize(
    "missing_key", ["job_name", "failure_stage", "failure_type", "reason_code"]
)
def test_missing_required_field_raises(missing_key: str) -> None:
    message = _internal_message()
    del message[missing_key]
    now = dt.datetime.now(dt.UTC)

    with pytest.raises(ValueError):
        handler_module._normalize(message, now)


def test_missing_source_falls_back_to_alarm_classification_without_raising() -> None:
    """★ `source`は正規化前の発生源判別そのものに使う鍵であり、他の必須フィールドとは
    性質が違う。`source`が無い(かつAlarmMessageの特徴〔AlarmName/Trigger〕も無い)
    payloadは、Alarm経路にfall backする(#_is_internal_payload()のdocstring)。
    Alarm経路自体はdimensions等が無くても"unknown"へfail-softする既存契約のため、
    ここではraiseしない(handler自体を落とさない安全側の既定動作)。
    """
    message = _internal_message()
    del message["source"]
    now = dt.datetime.now(dt.UTC)

    signal = handler_module._normalize(message, now)

    assert signal.source == "cloudwatch_alarm"
    assert signal.job_name == "unknown"


def test_non_int_failure_count_raises() -> None:
    message = _internal_message()
    message["failure_count"] = "3"  # 文字列(不正)
    now = dt.datetime.now(dt.UTC)

    with pytest.raises(TypeError):
        handler_module._normalize(message, now)


def test_bool_failure_count_raises() -> None:
    """★ boolはintのサブクラスのため、isinstance(value, int)だけでは通ってしまう
    (Trueが1として紛れ込む)ことを防ぐ。"""
    message = _internal_message()
    message["failure_count"] = True
    now = dt.datetime.now(dt.UTC)

    with pytest.raises(TypeError):
        handler_module._normalize(message, now)


def test_non_bool_is_ongoing_raises() -> None:
    message = _internal_message()
    message["is_ongoing"] = "yes"
    now = dt.datetime.now(dt.UTC)

    with pytest.raises(TypeError):
        handler_module._normalize(message, now)


def test_negative_failure_count_raises() -> None:
    message = _internal_message()
    message["failure_count"] = -1
    now = dt.datetime.now(dt.UTC)

    with pytest.raises(ValueError):
        handler_module._normalize(message, now)
