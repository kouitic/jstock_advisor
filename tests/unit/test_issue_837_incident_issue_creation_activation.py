"""出荷 config の issue_creation_enabled の固定(Issue #837。#508 の activation)。

## 出荷値は false(10/12 のリリースでは有効化しない)

#876 で出荷値を true にしたが、新規 Issue の作成に occurrence 単位の gate・日あたりの上限が無く、
既存の Issue が closed の間は同じ障害の signal ごとに新規 Issue が立つ(独立確認: #188
issuecomment-6114701122)。USER 判断(#122 issuecomment-6114810773 の【1】)により、10/12 の
リリースでは自動起票を有効化せず、出荷値を false に戻す。抑止機構の実装後に、次回のリリースで
true にする(その切替は別の USER 承認)。

## 何を検証するか

    (a) 出荷 config は false で、YAML の真偽値として読める(引用符つきの文字列ではない。
        `"false"` などは不可。YAML の `no` / `off` は bool の False として読まれ、値も型も同じ)。
        他の値(dedup_window_minutes など)・キーの集合・label は #876 の前後で変えていない
    (b) 出荷 config のまま(テスト側で config を上書きしない)なら、配線(環境変数)が揃っていても
        GitHub を一切呼ばない。LINE は送られ、状態は SENT のまま(これが今回の Production の状態)
    (b') 有効の config(テスト側で明示的に true にする)と配線があれば、GitHub Issue を 1 件作成し、
        出荷 config の label を付ける(有効化したときの経路が壊れていないことの固定)。
        ★ Issue #926 以降は、新規起票の上限値(config/incident_issue_limits.yaml)が決定済みで
        あることが条件になる。出荷値は USER 承認の決定済みの値(#122 2026-10-15)だが、このテストは
        起票を妨げない値へ差し替えて経路を確かめる
    (b2) 有効の config(テスト側で明示的に true にする)と配線が揃っていても、上限値が未決定
        (テスト側で全項目 null へ差し替える)なら、新規 Issue を作らない(GitHub を一切呼ばない)。
        LINE は送られ、状態に抑止の理由(LIMITS_UNDECIDED)を残す。
        ★ 出荷の上限値は決定済み(USER 承認)なので、出荷の flag = false が新規起票を止めている
        唯一の条件(= (b))★
    (c) 有効の config でも、配線が無ければ GitHub を一切呼ばず、LINE も送られる
        (配線されていない環境では、状態に CONFIGURATION_ERROR を記録するだけで安全にスキップする)
    (d) LINE の通知(送った文面・件数・handler の戻り値・状態の SENT)は、flag が false のときと
        true のときで同一(GitHub の起票は LINE を変えない)
    (e) 無効の config(false)では GitHub を一切呼ばない
    (f) 既存 Issue の検索に使う label(_SEARCH_LABEL)が、起票時に付ける label に含まれる
        (ずれると、再発のたびに重複して起票する)

## 何を検証しないか

GitHub 側の失敗が LINE に影響しないこと・公開本文の allowlist・fingerprint の dedup は、
既存の `test_issue_508_*` が固定している(それらは config を上書きして flag を true にしているため、
出荷値には影響されない)。
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
from jstock_advisor.domain.notification.incident_issue_limits import (
    IncidentIssueLimits,
    undecided_limits,
)
from jstock_advisor.infrastructure.aws import incident_state_tracker as tracker
from jstock_advisor.infrastructure.github import client as github_client_module
from jstock_advisor.lambda_handlers import incident_notifier_handler as handler_module
from jstock_advisor.services import incident_github_issue_service

_REGION = "ap-northeast-1"
_SECRET_NAME = "github-app-incident-activation"
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "incident_notification.yaml"
_NOW = dt.datetime(2026, 9, 25, 0, 0, tzinfo=dt.UTC)


# --- (a) 出荷 config -----------------------------------------------------------------------


def test_shipped_config_keeps_issue_creation_disabled_as_a_real_boolean() -> None:
    raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    assert raw["issue_creation_enabled"] is False  # 引用符つきの文字列("false" など)ではない
    assert load_config().incident_notification.issue_creation_enabled is False


def test_shipped_config_has_the_known_keys_and_only_the_flag_is_off() -> None:
    raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    assert raw == {
        "version": 1,
        "dedup_window_minutes": 30,
        "claim_stale_minutes": 5,
        "issue_creation_enabled": False,
        "github_issue_claim_timeout_minutes": 10,
        "issue_labels": ["production-incident", "auto-generated"],
    }


def test_the_search_label_is_one_of_the_labels_attached_to_a_created_issue() -> None:
    # 既存 OPEN Issue の検索(incident_github_issue_service._SEARCH_LABEL)に使う label が、
    # 起票時に付ける label(出荷 config の issue_labels)に含まれること。ずれると、再発のたびに
    # 既存の OPEN Issue を見つけられず、公開 repo へ重複して起票してしまう(有効化で初めて効く経路)。
    from jstock_advisor.services import incident_github_issue_service

    labels = load_config().incident_notification.issue_labels
    assert incident_github_issue_service._SEARCH_LABEL in labels
    # 検索は専用 label で行う(auto-generated は週次改善レビューの Issue とも共有する)
    assert incident_github_issue_service._SEARCH_LABEL == "production-incident"


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


def _enabled_config() -> Any:
    """出荷 config の issue_creation_enabled だけを true にした config(有効化後の経路の検証用)。"""
    config = load_config()
    return config.model_copy(
        update={
            "incident_notification": config.incident_notification.model_copy(
                update={"issue_creation_enabled": True}
            )
        }
    )


def _decide_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """上限値(Issue #926)を、起票を妨げない決定済みの値にする(出荷値は全項目 null)。"""
    monkeypatch.setattr(
        incident_github_issue_service,
        "load_incident_issue_limits",
        lambda: IncidentIssueLimits(
            new_issue_min_interval_minutes=1,
            new_issue_limit_per_fingerprint=99,
            new_issue_limit_global=99,
            auto_stop_denied_threshold=99,
        ),
    )


# --- (b) 出荷 config のまま: 配線が揃っていても GitHub を呼ばない(今回の Production の状態) ------


def test_with_shipped_config_and_full_wiring_github_is_never_called(
    monkeypatch: pytest.MonkeyPatch, line: _RecordingLineClient, secret_arn: str
) -> None:
    _wire(monkeypatch, secret_arn)
    fake = _install_urlopen(monkeypatch, [])
    message = _alarm_message()

    # handler の load_config を上書きしない = 出荷 config がそのまま使われる。
    # 同じ signal を 3 回届けても(再配信・再試行を想定)、GitHub は一度も呼ばれない
    for _ in range(3):
        result = handler_module.handler(_sns_event(message), None)
        assert result == {"processed": 1}

    assert fake.requests == []  # installation token の取得も Issue の作成もない
    assert len(line.sent) == 1  # LINE は 1 通(2 回目以降は LINE 側の dedup で抑止される)
    state = tracker.get_incident_state(_fingerprint_of(message))
    assert state["status"] == "SENT"
    assert "github_issue_create_status" not in state  # 無効の間は状態も書かない
    assert "github_issue_number" not in state


# --- (b') 有効の config と配線があれば、起票する(有効化後の経路) --------------------------------


def test_with_an_enabled_config_and_wiring_one_github_issue_is_created(
    monkeypatch: pytest.MonkeyPatch, line: _RecordingLineClient, secret_arn: str
) -> None:
    enabled = _enabled_config()
    monkeypatch.setattr(handler_module, "load_config", lambda: enabled)
    _wire(monkeypatch, secret_arn)
    _decide_limits(monkeypatch)
    fake = _install_urlopen(monkeypatch, [_TOKEN, _ISSUE])
    message = _alarm_message()

    result = handler_module.handler(_sns_event(message), None)

    assert result == {"processed": 1}
    assert len(fake.requests) == 2  # installation token の取得 + Issue の作成
    created = json.loads(fake.requests[1].data.decode("utf-8"))
    assert created["labels"] == ["production-incident", "auto-generated"]
    state = tracker.get_incident_state(_fingerprint_of(message))
    assert state["github_issue_create_status"] == "CREATED"
    assert state["github_issue_number"] == 7


# --- (b2) 有効の config でも、出荷の上限値が未決定のままなら新規 Issue は作らない(Issue #926)


def test_with_an_enabled_config_and_undecided_limits_no_issue_is_created_and_line_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, line: _RecordingLineClient, secret_arn: str
) -> None:
    enabled = _enabled_config()
    monkeypatch.setattr(handler_module, "load_config", lambda: enabled)
    _wire(monkeypatch, secret_arn)
    fake = _install_urlopen(monkeypatch, [])
    message = _alarm_message()

    # 上限値を未決定(全項目 null)へ差し替える。出荷値は USER が承認した値(#122 2026-10-15)
    # に決定済みのため、『未決定のときに起票しない』経路は明示的に作る
    monkeypatch.setattr(
        incident_github_issue_service,
        "load_incident_issue_limits",
        lambda: undecided_limits(),
    )
    result = handler_module.handler(_sns_event(message), None)

    assert result == {"processed": 1}
    assert fake.requests == []  # GitHub を一切呼ばない
    assert len(line.sent) == 1  # LINE は通常どおり
    state = tracker.get_incident_state(_fingerprint_of(message))
    assert state["status"] == "SENT"
    assert "github_issue_number" not in state
    assert state["github_issue_last_denied_reason"] == "LIMITS_UNDECIDED"


# --- (c) 有効の config でも、配線が無ければ GitHub を呼ばない ------------------------------


@pytest.mark.parametrize("missing", ["both", "secret_arn", "repository"])
def test_with_an_enabled_config_but_no_wiring_github_is_never_called(
    monkeypatch: pytest.MonkeyPatch,
    line: _RecordingLineClient,
    secret_arn: str,
    missing: str,
) -> None:
    enabled = _enabled_config()
    monkeypatch.setattr(handler_module, "load_config", lambda: enabled)
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


# --- (e) 無効の config では GitHub を呼ばない(配線ありでも) ----------------


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
