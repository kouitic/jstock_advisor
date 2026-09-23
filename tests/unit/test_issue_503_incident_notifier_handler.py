"""Issue #503(#132 X-4): incident_notifier_handler.py のテスト(moto + line clientの差し替え)。"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.notification.incident_message import (
    IncidentNotice,
    build_incident_message,
    resolve_incident_job,
)
from jstock_advisor.infrastructure.aws import incident_state_tracker as tracker
from jstock_advisor.infrastructure.line import client as line_client_module
from jstock_advisor.lambda_handlers import incident_notifier_handler as handler_module

_REGION = "ap-northeast-1"


class _RecordingLineClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)


class _FailingLineClient:
    def push_message(self, text: str) -> None:
        raise RuntimeError("LINE API unavailable")


def _alarm_message(
    *,
    alarm_name: str = "jstock-advisor-evaluation-errors",
    metric_name: str = "Errors",
    function_name: str = "jstock-advisor-evaluation",
    state_change_time: str = "2026-09-24T09:00:00.000+0000",
    new_state_reason: str = (
        "Threshold Crossed: 1 datapoint [2.0] was greater than the threshold (1.0)."
    ),
) -> dict[str, Any]:
    # ★ レビュー指摘 R1: NewStateReason(自由文)へ実際の値を入れる。空文字だと
    # 「本文へNewStateReasonを連結する」変異が(連結内容が空になるため)無害化され、
    # F1の等値固定テストで検知できなくなる。
    return {
        "AlarmName": alarm_name,
        "NewStateValue": "ALARM",
        "NewStateReason": new_state_reason,
        "StateChangeTime": state_change_time,
        "Trigger": {
            "MetricName": metric_name,
            "Namespace": "AWS/Lambda",
            "Dimensions": [{"name": "FunctionName", "value": function_name}],
        },
    }


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


def _fingerprint_of(message: dict[str, Any]) -> str:
    from jstock_advisor.domain.notification.incident_fingerprint import compute_fingerprint

    return compute_fingerprint(handler_module._build_fingerprint_input(message))


# --- 正常系: 新規 incident → LINE 送信 → SENT ------------------------------------


def test_new_alarm_sends_line_and_marks_sent(recording_line_client: _RecordingLineClient) -> None:
    message = _alarm_message()

    result = handler_module.handler(_sns_event(message), None)

    assert result == {"processed": 1}
    assert len(recording_line_client.sent) == 1
    assert "対象: 過去の推奨の評価" in recording_line_client.sent[0]  # evaluation → IncidentJob
    assert "件数: 1件" in recording_line_client.sent[0]  # occurrence_count = 1

    state = tracker.get_incident_state(_fingerprint_of(message))
    assert state["status"] == "SENT"


def test_line_body_is_exactly_the_builder_output_with_nothing_appended(
    recording_line_client: _RecordingLineClient,
) -> None:
    """★ レビュー指摘 F1 の直接固定: 送信本文が build_incident_message() の出力と完全一致する
    (部分一致〔in〕ではない)。本文末尾へ何か(例: AlarmのStateReason)を連結する変異を検知する。
    """
    message = _alarm_message()

    handler_module.handler(_sns_event(message), None)

    expected = build_incident_message(
        IncidentNotice(
            job=resolve_incident_job(handler_module._extract_function_name(message)),
            occurred_at=handler_module._extract_occurred_at(message, dt.datetime.now(dt.UTC)),
            failure_count=1,
        )
    )
    assert recording_line_client.sent[0] == expected


def test_occurred_at_uses_the_alarm_state_change_time(
    recording_line_client: _RecordingLineClient,
) -> None:
    # StateChangeTime = UTC 08:03(JST 17:03相当)
    message = _alarm_message(state_change_time="2026-09-24T08:03:00.000+0000")

    handler_module.handler(_sns_event(message), None)

    assert "発生時刻: 17:03" in recording_line_client.sent[0]


# --- dedup: 同一 fingerprint の連続発火は抑止される ------------------------------


def test_repeated_alarm_within_window_is_suppressed_and_line_sent_once(
    recording_line_client: _RecordingLineClient,
) -> None:
    message = _alarm_message()

    handler_module.handler(_sns_event(message), None)
    handler_module.handler(_sns_event(message), None)  # 直後の再発火(retry相当)

    assert len(recording_line_client.sent) == 1  # 2通目は送られない


def test_different_alarm_name_is_a_different_incident(
    recording_line_client: _RecordingLineClient,
) -> None:
    handler_module.handler(_sns_event(_alarm_message(alarm_name="alarm-A")), None)
    handler_module.handler(_sns_event(_alarm_message(alarm_name="alarm-B")), None)

    assert len(recording_line_client.sent) == 2  # 別incidentとして両方送る


def test_different_function_name_is_a_different_incident(
    recording_line_client: _RecordingLineClient,
) -> None:
    """★ 異なるLambda関数の同時障害を同一incidentへ丸めない(#502 のAC)。"""
    handler_module.handler(
        _sns_event(_alarm_message(alarm_name="a", function_name="jstock-advisor-evaluation")), None
    )
    handler_module.handler(
        _sns_event(_alarm_message(alarm_name="a", function_name="jstock-advisor-weekly-review")),
        None,
    )

    assert len(recording_line_client.sent) == 2


# --- LINE push失敗時の契約(claim解除 → Lambdaを失敗させる) -------------------------


def test_line_push_failure_releases_the_new_claim_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handler_module, "build_live_line_client_from_env", lambda: _FailingLineClient()
    )
    message = _alarm_message()

    with pytest.raises(RuntimeError, match="LINE API unavailable"):
        handler_module.handler(_sns_event(message), None)

    # is_new=True の release: item が削除され、次のretryが初出として即座に再claimできる。
    assert tracker.get_incident_state(_fingerprint_of(message)) is None


def test_retry_after_line_push_failure_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    message = _alarm_message()
    monkeypatch.setattr(
        handler_module, "build_live_line_client_from_env", lambda: _FailingLineClient()
    )
    with pytest.raises(RuntimeError):
        handler_module.handler(_sns_event(message), None)

    fake = _RecordingLineClient()
    monkeypatch.setattr(handler_module, "build_live_line_client_from_env", lambda: fake)
    handler_module.handler(_sns_event(message), None)  # SNS/Lambdaのretry相当

    assert len(fake.sent) == 1
    assert "件数: 1件" in fake.sent[0]  # release済みなので初出扱い(occurrence_countは1から)


def test_handler_uses_the_strict_line_constructor_not_the_cli_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ レビュー指摘 F2 の直接固定: build_line_client_from_env()(CLI用フォールバック)へ
    差し替える変異を検知する。LINE構築関数そのものはmonkeypatchせず、認証情報だけを
    未設定にしてhandlerを直接呼ぶ。strictな構築(build_live_line_client_from_env)なら
    LineCredentialsMissingErrorが送出されるが、CLI用フォールバックへ差し替えられていると
    ConsoleLineClientへ黙って逃げて例外が出ない(Issue #117の再発検知)。
    """
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_USER_ID", raising=False)
    message = _alarm_message()

    with pytest.raises(line_client_module.LineCredentialsMissingError):
        handler_module.handler(_sns_event(message), None)


def test_credentials_missing_releases_the_claim_and_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_missing() -> Any:
        raise line_client_module.LineCredentialsMissingError("missing")

    monkeypatch.setattr(handler_module, "build_live_line_client_from_env", _raise_missing)
    message = _alarm_message()

    with pytest.raises(line_client_module.LineCredentialsMissingError):
        handler_module.handler(_sns_event(message), None)

    assert tracker.get_incident_state(_fingerprint_of(message)) is None


# --- fingerprint の安定性(StateReasonの自由文を使わない) -------------------------


def test_fingerprint_input_does_not_use_the_free_text_state_reason() -> None:
    message = _alarm_message()  # NewStateReasonに実際の自由文が入っている(既定値)

    fp_input = handler_module._build_fingerprint_input(message)

    assert fp_input.error_message == "jstock-advisor-evaluation-errors"  # AlarmName
    assert "Threshold Crossed" not in fp_input.error_message
    assert fp_input.error_type == "CloudWatchAlarm"
    assert fp_input.failure_type == "Errors"
    assert fp_input.job_name == "jstock-advisor-evaluation"


def test_unresolvable_dimensions_fall_back_to_unknown_without_raising() -> None:
    message = {"AlarmName": "x", "Trigger": {"MetricName": "Errors", "Dimensions": []}}

    fp_input = handler_module._build_fingerprint_input(message)

    assert fp_input.job_name == "unknown"


def test_function_name_is_picked_by_name_not_by_position() -> None:
    """★ Dimensionsに複数要素があっても、name=="FunctionName" の値だけを使う
    (他の次元(例 Resource)を誤って job_name にしない)。"""
    message = {
        "AlarmName": "x",
        "Trigger": {
            "MetricName": "Errors",
            "Dimensions": [
                {"name": "Resource", "value": "jstock-advisor-evaluation:$LATEST"},
                {"name": "FunctionName", "value": "jstock-advisor-evaluation"},
            ],
        },
    }

    fp_input = handler_module._build_fingerprint_input(message)

    assert fp_input.job_name == "jstock-advisor-evaluation"
    assert "$LATEST" not in fp_input.job_name


# --- 複数レコード ---------------------------------------------------------------


def test_multiple_records_in_one_event_are_each_processed(
    recording_line_client: _RecordingLineClient,
) -> None:
    result = handler_module.handler(
        _sns_event(_alarm_message(alarm_name="a"), _alarm_message(alarm_name="b")), None
    )

    assert result == {"processed": 2}
    assert len(recording_line_client.sent) == 2


def test_incident_notification_config_is_loaded_from_the_real_config_files() -> None:
    """既定の config/incident_notification.yaml が読め、handlerが使う値を持つこと。"""
    config = load_config()
    assert config.incident_notification.dedup_window_minutes > 0
    assert config.incident_notification.claim_stale_minutes > 0
