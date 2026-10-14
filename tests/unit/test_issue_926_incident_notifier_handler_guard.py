"""Issue #926: incident_notifier_handler.py の自動停止の LINE 通知(1 回だけ)の契約テスト。

★最重要要件(#508): GitHub 側の抑止・停止・通知の失敗は、LINE の incident 通知の送信・
成否判定・claim の状態機械に一切影響しない。自動停止の通知は、停止にした実行が 1 回だけ
送る(push の失敗は確保を解放して次の signal で再試行する)。手動の停止は通知しない。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import boto3
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from moto import mock_aws

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.notification import incident_issue_guard as guard
from jstock_advisor.domain.notification.incident_issue_limits import (
    IncidentIssueLimits,
    undecided_limits,
)
from jstock_advisor.infrastructure.aws import incident_state_tracker as tracker
from jstock_advisor.infrastructure.github import client as github_client_module
from jstock_advisor.lambda_handlers import incident_notifier_handler as handler_module
from jstock_advisor.services import incident_github_issue_service as service

_REGION = "ap-northeast-1"
_SECRET = "github-app-incident-926-handler"
_NOW = dt.datetime(2026, 10, 12, 0, 0, tzinfo=dt.UTC)


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()
    ).decode()


def _alarm(name: str) -> dict[str, Any]:
    return {
        "AlarmName": name,
        "NewStateValue": "ALARM",
        "NewStateReason": "Threshold Crossed: 1 datapoint [2.0] was greater than the threshold.",
        "StateChangeTime": "2026-10-12T09:00:00.000+0000",
        "Trigger": {
            "MetricName": "Errors",
            "Namespace": "AWS/Lambda",
            "Dimensions": [{"name": "FunctionName", "value": "jstock-advisor-evaluation"}],
        },
    }


def _event(*messages: dict[str, Any]) -> dict[str, Any]:
    return {"Records": [{"Sns": {"Message": json.dumps(m)}} for m in messages]}


class Line:
    def __init__(self, fail_on_stop: int = 0) -> None:
        self.sent: list[str] = []
        self.fail_on_stop = fail_on_stop  # 自動停止の文面の push を、先頭からこの回数だけ失敗させる

    def push_message(self, text: str) -> None:
        if text == guard.build_auto_stop_message() and self.fail_on_stop > 0:
            self.fail_on_stop -= 1
            raise RuntimeError("LINE API unavailable")
        self.sent.append(text)

    @property
    def stops(self) -> int:
        return sum(1 for t in self.sent if t == guard.build_auto_stop_message())

    @property
    def incidents(self) -> int:
        return len(self.sent) - self.stops


class _Resp:
    def __init__(self, payload: Any) -> None:
        self._body = json.dumps(payload).encode() if payload is not None else b""

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        return None


class Github:
    def __init__(self) -> None:
        self.created = 0
        self.requests = 0

    def __call__(self, request: Any, timeout: int = 15) -> _Resp:
        self.requests += 1
        url = request.full_url
        if url.endswith("/access_tokens"):
            return _Resp({"token": "ghs_dummy", "expires_at": "2099-01-01T00:00:00Z"})
        if request.get_method() == "POST" and url.endswith("/issues"):
            self.created += 1
            n = 100 + self.created
            return _Resp(
                {
                    "number": n,
                    "html_url": f"https://github.com/o/r/issues/{n}",
                    "state": "open",
                    "body": "",
                }
            )
        raise AssertionError(f"unexpected {request.get_method()} {url}")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
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
        sm = boto3.client("secretsmanager", region_name=_REGION)
        sm.create_secret(
            Name=_SECRET,
            SecretString=json.dumps({"app_id": "1", "installation_id": "2", "private_key": _pem()}),
        )
        arn = sm.describe_secret(SecretId=_SECRET)["ARN"]
        real = load_config()
        enabled = real.model_copy(
            update={
                "incident_notification": real.incident_notification.model_copy(
                    update={
                        "issue_creation_enabled": True,
                        "github_issue_claim_timeout_minutes": 10,
                    }
                )
            }
        )
        monkeypatch.setattr(handler_module, "load_config", lambda: enabled)
        monkeypatch.setenv("GITHUB_APP_SECRET_ARN", arn)
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        yield


def _limits(monkeypatch: pytest.MonkeyPatch, **over: int) -> None:
    values = {
        "new_issue_min_interval_minutes": 360,
        "new_issue_limit_per_fingerprint": 2,
        "new_issue_limit_global": 1,
        "auto_stop_denied_threshold": 1,
    }
    values.update(over)
    monkeypatch.setattr(
        service, "load_incident_issue_limits", lambda: IncidentIssueLimits(**values)
    )


@pytest.fixture
def line(monkeypatch: pytest.MonkeyPatch) -> Line:
    fake = Line()
    monkeypatch.setattr(handler_module, "build_live_line_client_from_env", lambda: fake)
    return fake


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> Github:
    fake = Github()
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)
    return fake


# --- 自動停止の通知(1 回だけ)
# ------------------------------------------------------------------------


def test_the_auto_stop_is_notified_once_by_the_run_that_stopped(
    env, monkeypatch, line, github
) -> None:
    _limits(monkeypatch)
    assert handler_module.handler(_event(_alarm("alarm-one")), None) == {"processed": 1}
    assert (line.incidents, line.stops) == (1, 0)  # 通常の incident 通知のみ
    assert handler_module.handler(_event(_alarm("alarm-two")), None) == {"processed": 1}
    assert (line.incidents, line.stops) == (2, 1)  # 全体の上限を超えた試行 -> 停止 -> 1 回だけ通知
    assert github.created == 1
    assert handler_module.handler(_event(_alarm("alarm-three")), None) == {"processed": 1}
    assert (line.incidents, line.stops) == (
        3,
        1,
    )  # 停止中でも incident の LINE は届き、停止の通知は繰り返さない


def test_the_incident_line_message_is_the_same_with_or_without_the_guard(
    env, monkeypatch, line, github
) -> None:
    _limits(monkeypatch)
    handler_module.handler(_event(_alarm("alarm-one")), None)
    handler_module.handler(_event(_alarm("alarm-two")), None)  # 停止にした実行
    incident_texts = [t for t in line.sent if t != guard.build_auto_stop_message()]
    assert (
        len(incident_texts) == 2 and incident_texts[0] == incident_texts[1]
    )  # 同じ job の本文は同一


def test_a_failed_stop_notification_is_retried_by_the_next_signal(env, monkeypatch, github) -> None:
    flaky = Line(fail_on_stop=1)
    monkeypatch.setattr(handler_module, "build_live_line_client_from_env", lambda: flaky)
    _limits(monkeypatch)
    handler_module.handler(_event(_alarm("alarm-one")), None)
    assert handler_module.handler(_event(_alarm("alarm-two")), None) == {
        "processed": 1
    }  # push の失敗は握る
    assert flaky.stops == 0
    control = tracker.read_github_issue_control()
    assert control is not None and "kill_switch_notified_at" not in control  # 確保は解放されている
    handler_module.handler(_event(_alarm("alarm-three")), None)
    assert flaky.stops == 1  # 次の signal で再試行して届く
    handler_module.handler(_event(_alarm("alarm-four")), None)
    assert flaky.stops == 1  # その後は繰り返さない


def test_a_manual_stop_sends_no_notification(env, monkeypatch, line, github) -> None:
    _limits(monkeypatch)
    tracker.try_trip_kill_switch(_NOW, guard.KILL_SWITCH_REASON_MANUAL)
    handler_module.handler(_event(_alarm("alarm-one")), None)
    assert (line.incidents, line.stops) == (1, 0)
    assert github.requests == 0  # 停止中は GitHub を呼ばない


def test_the_stop_notification_text_is_the_fixed_plain_message(
    env, monkeypatch, line, github
) -> None:
    _limits(monkeypatch)
    handler_module.handler(_event(_alarm("alarm-one")), None)
    handler_module.handler(_event(_alarm("alarm-two")), None)
    stop_texts = [t for t in line.sent if t == guard.build_auto_stop_message()]
    assert stop_texts == [guard.build_auto_stop_message()]


# --- ★最重要要件: LINE の経路は抑止・停止・通知の失敗に影響されない
# -------------------------------------------


def test_a_failing_notification_claim_never_breaks_the_handler(
    env, monkeypatch, line, github
) -> None:
    _limits(monkeypatch)

    def boom(now: dt.datetime) -> bool:
        raise RuntimeError("dynamodb")

    monkeypatch.setattr(tracker, "try_claim_kill_switch_notification", boom)
    handler_module.handler(_event(_alarm("alarm-one")), None)
    assert handler_module.handler(_event(_alarm("alarm-two")), None) == {"processed": 1}
    assert line.incidents == 2 and line.stops == 0


def test_an_unreadable_control_item_leaves_the_line_path_untouched(
    env, monkeypatch, line, github
) -> None:
    _limits(monkeypatch)

    def boom() -> None:
        raise RuntimeError("dynamodb down")

    monkeypatch.setattr(tracker, "read_github_issue_control", boom)
    assert handler_module.handler(_event(_alarm("alarm-one")), None) == {"processed": 1}
    assert line.incidents == 1 and github.requests == 0


def test_an_unexpected_guard_bug_leaves_the_line_path_untouched(
    env, monkeypatch, line, github
) -> None:
    _limits(monkeypatch)

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(guard, "precheck_new_issue", boom)
    assert handler_module.handler(_event(_alarm("alarm-one")), None) == {"processed": 1}
    assert line.incidents == 1


def test_with_undecided_limits_the_line_path_is_unchanged_and_no_issue_is_created(
    env, line, github, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 上限値を未決定(全項目 null)へ差し替える(出荷値は USER 承認の決定済みの値)。
    # LINE は通常どおり、GitHub は呼ばない
    monkeypatch.setattr(service, "load_incident_issue_limits", undecided_limits)
    assert handler_module.handler(_event(_alarm("alarm-one")), None) == {"processed": 1}
    assert line.incidents == 1 and github.requests == 0
