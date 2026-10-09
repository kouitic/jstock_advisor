"""出荷 config の issue_creation_enabled = true の固定(Issue #837。#508 の activation)。

## 何を検証するか

`config/incident_notification.yaml` の `issue_creation_enabled` を false から true にする変更が、
**その 1 値だけ** で、かつ次の性質を壊さないことを固定する。

    (a) 出荷 config は true で、YAML の真偽値として読める(`"true"` `yes` のような文字列ではない)。
        他の値(dedup_window_minutes など)・キーの集合・label は変えていない
    (b) 出荷 config のまま(テスト側で config を上書きしない)、infra の配線(環境変数)が
        あれば GitHub Issue を 1 件作成し、出荷 config の label を付ける
    (c) 出荷 config が true でも、配線(環境変数)が無ければ GitHub を一切呼ばず、LINE も送られる
        (配線されていない環境では、状態に CONFIGURATION_ERROR を記録するだけで安全にスキップする)
    (d) LINE の通知(送った文面・件数・handler の戻り値・状態の SENT)は、flag が false のときと
        true のときで同一(GitHub の起票は LINE を変えない)
    (e) 無効の config(false)では GitHub を一切呼ばない(出荷が true になっても、無効の挙動は残る)

## 何を検証しないか

GitHub 側の失敗が LINE に影響しないこと・公開本文の allowlist・fingerprint の dedup は、
既存の `test_issue_508_*` が固定している(それらは config を上書きして flag を true にしているため、
出荷値の変更には影響されない)。本ファイルは「出荷値が true であること」と、
その状態での LINE 不変・配線なしの安全なスキップを固定する。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import boto3
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from moto import mock_aws

from jstock_advisor.config.loader import load_config
from jstock_advisor.infrastructure.aws import incident_state_tracker as tracker
from jstock_advisor.infrastructure.github import client as github_client_module
from jstock_advisor.lambda_handlers import incident_notifier_handler as handler_module

_REGION = "ap-northeast-1"
_SECRET_NAME = "github-app-incident-activation"
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "incident_notification.yaml"
_NOW = dt.datetime(2026, 9, 25, 0, 0, tzinfo=dt.UTC)


# --- (a) 出荷 config -----------------------------------------------------------------------


def test_shipped_config_enables_issue_creation_as_a_real_boolean() -> None:
    raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    assert raw["issue_creation_enabled"] is True  # 文字列の "true" / "yes" ではない
    assert load_config().incident_notification.issue_creation_enabled is True


def test_shipped_config_changes_only_the_one_value() -> None:
    raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    assert raw == {
        "version": 1,
        "dedup_window_minutes": 30,
        "claim_stale_minutes": 5,
        "issue_creation_enabled": True,
        "github_issue_claim_timeout_minutes": 10,
        "issue_labels": ["production-incident", "auto-generated"],
    }


def test_the_model_default_stays_disabled_so_a_missing_key_never_enables_creation() -> None:
    # 出荷 config から key が消えても有効にならない(fail-closed の向き)
    from jstock_advisor.config.models import IncidentNotificationConfig

    config = IncidentNotificationConfig(version=1, dedup_window_minutes=30, claim_stale_minutes=5)
    assert config.issue_creation_enabled is False


# --- ハーネス(配線あり / なしの handler 呼び出し) ----------------------------------------------


class _RecordingLineClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._body = json.dumps(payload).encode("utf-8") if payload is not None else b""

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _FakeUrlopen:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: int = 15) -> _FakeResponse:
        self.requests.append(request)
        return _FakeResponse(self._responses.pop(0))


def _generate_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=NoEncryption(),
    ).decode("utf-8")


def _alarm_message() -> dict[str, Any]:
    return {
        "AlarmName": "jstock-advisor-evaluation-errors",
        "NewStateValue": "ALARM",
        "NewStateReason": "Threshold Crossed: 1 datapoint [2.0] was greater than the threshold.",
        "StateChangeTime": "2026-09-25T09:00:00.000+0000",
        "Trigger": {
            "MetricName": "Errors",
            "Namespace": "AWS/Lambda",
            "Dimensions": [{"name": "FunctionName", "value": "jstock-advisor-evaluation"}],
        },
    }


def _sns_event(message: dict[str, Any]) -> dict[str, Any]:
    return {"Records": [{"Sns": {"Message": json.dumps(message)}}]}


def _fingerprint_of(message: dict[str, Any]) -> str:
    from jstock_advisor.domain.notification.incident_fingerprint import compute_fingerprint

    signal = handler_module._normalize_alarm_message(message, dt.datetime.now(dt.UTC))
    return compute_fingerprint(handler_module._build_fingerprint_input(signal))


@pytest.fixture
def secret_arn(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "jstock")
    with mock_aws():
        boto3.client("dynamodb", region_name=_REGION).create_table(
            TableName="jstock-incident_state",
            KeySchema=[{"AttributeName": "fingerprint", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "fingerprint", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        secretsmanager = boto3.client("secretsmanager", region_name=_REGION)
        secretsmanager.create_secret(
            Name=_SECRET_NAME,
            SecretString=json.dumps(
                {"app_id": "1", "installation_id": "2", "private_key": _generate_private_key_pem()}
            ),
        )
        yield secretsmanager.describe_secret(SecretId=_SECRET_NAME)["ARN"]


@pytest.fixture
def line(monkeypatch: pytest.MonkeyPatch) -> _RecordingLineClient:
    fake = _RecordingLineClient()
    monkeypatch.setattr(handler_module, "build_live_line_client_from_env", lambda: fake)
    return fake


def _wire(monkeypatch: pytest.MonkeyPatch, arn: str) -> None:
    monkeypatch.setenv("GITHUB_APP_SECRET_ARN", arn)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")


def _install_urlopen(monkeypatch: pytest.MonkeyPatch, responses: list[Any]) -> _FakeUrlopen:
    fake = _FakeUrlopen(responses)
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)
    return fake


_TOKEN = {"token": "ghs_dummy", "expires_at": "2026-09-25T01:00:00Z"}
_ISSUE = {"number": 7, "html_url": "https://github.com/owner/repo/issues/7", "state": "open"}


# --- (b) 出荷 config のまま、配線があれば起票する -------------------------------------------------


def test_with_shipped_config_and_wiring_one_github_issue_is_created(
    monkeypatch: pytest.MonkeyPatch, line: _RecordingLineClient, secret_arn: str
) -> None:
    _wire(monkeypatch, secret_arn)
    fake = _install_urlopen(monkeypatch, [_TOKEN, _ISSUE])
    message = _alarm_message()

    # handler の load_config を上書きしない = 出荷 config がそのまま使われる
    result = handler_module.handler(_sns_event(message), None)

    assert result == {"processed": 1}
    assert len(fake.requests) == 2  # installation token の取得 + Issue の作成
    created = json.loads(fake.requests[1].data.decode("utf-8"))
    assert created["labels"] == ["production-incident", "auto-generated"]
    state = tracker.get_incident_state(_fingerprint_of(message))
    assert state["github_issue_create_status"] == "CREATED"
    assert state["github_issue_number"] == 7


# --- (c) 出荷 config が true でも、配線が無ければ GitHub を呼ばない ------------------------------


@pytest.mark.parametrize("missing", ["both", "secret_arn", "repository"])
def test_with_shipped_config_but_no_wiring_github_is_never_called(
    monkeypatch: pytest.MonkeyPatch,
    line: _RecordingLineClient,
    secret_arn: str,
    missing: str,
) -> None:
    _wire(monkeypatch, secret_arn)
    if missing in ("both", "secret_arn"):
        monkeypatch.delenv("GITHUB_APP_SECRET_ARN")
    if missing in ("both", "repository"):
        monkeypatch.delenv("GITHUB_REPOSITORY")
    fake = _install_urlopen(monkeypatch, [])
    message = _alarm_message()

    result = handler_module.handler(_sns_event(message), None)

    assert result == {"processed": 1}
    assert fake.requests == []
    assert len(line.sent) == 1  # LINE は送られる
    state = tracker.get_incident_state(_fingerprint_of(message))
    assert state["status"] == "SENT"
    # 配線が無い状態は『設定不備』として記録するだけ(GitHub は呼ばない・LINE は影響を受けない)
    assert state["github_issue_create_status"] == "CONFIGURATION_ERROR"
    assert "github_issue_number" not in state


# --- (d) LINE は flag の true / false で同一 ------------------------------------------------------


def _run_once(
    monkeypatch: pytest.MonkeyPatch,
    line: _RecordingLineClient,
    secret_arn: str,
    *,
    enabled: bool,
) -> tuple[dict[str, Any], list[str], str]:
    config = load_config()
    patched = config.model_copy(
        update={
            "incident_notification": config.incident_notification.model_copy(
                update={"issue_creation_enabled": enabled}
            )
        }
    )
    monkeypatch.setattr(handler_module, "load_config", lambda: patched)
    _wire(monkeypatch, secret_arn)
    _install_urlopen(monkeypatch, [_TOKEN, _ISSUE] if enabled else [])
    line.sent.clear()
    message = _alarm_message()
    # 同じ fingerprint の dedup に当たらないよう、前回の状態を消す
    boto3.client("dynamodb", region_name=_REGION).delete_item(
        TableName="jstock-incident_state",
        Key={"fingerprint": {"S": _fingerprint_of(message)}},
    )
    result = handler_module.handler(_sns_event(message), None)
    state = tracker.get_incident_state(_fingerprint_of(message))
    return result, list(line.sent), str(state["status"])


def test_line_output_is_identical_whether_issue_creation_is_enabled_or_not(
    monkeypatch: pytest.MonkeyPatch, line: _RecordingLineClient, secret_arn: str
) -> None:
    disabled = _run_once(monkeypatch, line, secret_arn, enabled=False)
    enabled = _run_once(monkeypatch, line, secret_arn, enabled=True)

    assert disabled == enabled
    result, sent, status = enabled
    assert result == {"processed": 1}
    assert len(sent) == 1
    assert status == "SENT"


# --- (e) 無効の config では GitHub を呼ばない(出荷が true でも、無効の挙動は残る) ----------------


def test_with_a_disabled_config_github_is_never_called_even_when_wired(
    monkeypatch: pytest.MonkeyPatch, line: _RecordingLineClient, secret_arn: str
) -> None:
    config = load_config()
    disabled = config.model_copy(
        update={
            "incident_notification": config.incident_notification.model_copy(
                update={"issue_creation_enabled": False}
            )
        }
    )
    monkeypatch.setattr(handler_module, "load_config", lambda: disabled)
    _wire(monkeypatch, secret_arn)
    fake = _install_urlopen(monkeypatch, [])
    message = _alarm_message()

    result = handler_module.handler(_sns_event(message), None)

    assert result == {"processed": 1}
    assert fake.requests == []
    state = tracker.get_incident_state(_fingerprint_of(message))
    assert state["status"] == "SENT"
    assert "github_issue_create_status" not in state
