"""incident_notifier_handler.pyにおける、LINE通知経路とGitHub Issue自動起票の
失敗分離(Issue #508 Phase A設計の★最重要要件)のテスト。

LINE × GitHub の成功/失敗 4象限を固定する:
    LINE成功・GitHub成功 / LINE成功・GitHub失敗 / LINE失敗・GitHub成功 /
    LINE失敗・GitHub失敗

GitHub側の処理がLINEの送信・成否判定・claim状態機械へ一切影響しないこと、および
LINE側の失敗がGitHub側の試行機会を奪わないことを、実際のhandler呼び出しで確認する。
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
from jstock_advisor.infrastructure.aws import incident_state_tracker as tracker
from jstock_advisor.infrastructure.github import client as github_client_module
from jstock_advisor.lambda_handlers import incident_notifier_handler as handler_module

_REGION = "ap-northeast-1"
_SECRET_NAME = "github-app-incident-isolation"


class _RecordingLineClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)


class _FailingLineClient:
    def push_message(self, text: str) -> None:
        raise RuntimeError("LINE API unavailable")


def _generate_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=NoEncryption(),
    )
    return pem.decode("utf-8")


def _alarm_message(
    *,
    alarm_name: str = "jstock-advisor-evaluation-errors",
    metric_name: str = "Errors",
    function_name: str = "jstock-advisor-evaluation",
    state_change_time: str = "2026-09-25T09:00:00.000+0000",
) -> dict[str, Any]:
    return {
        "AlarmName": alarm_name,
        "NewStateValue": "ALARM",
        "NewStateReason": "Threshold Crossed: 1 datapoint [2.0] was greater than the threshold.",
        "StateChangeTime": state_change_time,
        "Trigger": {
            "MetricName": metric_name,
            "Namespace": "AWS/Lambda",
            "Dimensions": [{"name": "FunctionName", "value": function_name}],
        },
    }


def _sns_event(*messages: dict[str, Any]) -> dict[str, Any]:
    return {"Records": [{"Sns": {"Message": json.dumps(m)}} for m in messages]}


def _fingerprint_of(message: dict[str, Any]) -> str:
    from jstock_advisor.domain.notification.incident_fingerprint import compute_fingerprint

    signal = handler_module._normalize_alarm_message(message, dt.datetime.now(dt.UTC))
    return compute_fingerprint(handler_module._build_fingerprint_input(signal))


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch):
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
        secretsmanager = boto3.client("secretsmanager", region_name=_REGION)
        secretsmanager.create_secret(
            Name=_SECRET_NAME,
            SecretString=json.dumps(
                {"app_id": "1", "installation_id": "2", "private_key": _generate_private_key_pem()}
            ),
        )
        secret_arn = secretsmanager.describe_secret(SecretId=_SECRET_NAME)["ARN"]
        yield secret_arn


@pytest.fixture
def recording_line_client(monkeypatch: pytest.MonkeyPatch) -> _RecordingLineClient:
    fake = _RecordingLineClient()
    monkeypatch.setattr(handler_module, "build_live_line_client_from_env", lambda: fake)
    return fake


@pytest.fixture
def failing_line_client(monkeypatch: pytest.MonkeyPatch) -> _FailingLineClient:
    fake = _FailingLineClient()
    monkeypatch.setattr(handler_module, "build_live_line_client_from_env", lambda: fake)
    return fake


@pytest.fixture
def github_enabled(monkeypatch: pytest.MonkeyPatch, aws_env: str):
    """issue_creation_enabled=trueへ上書きし、infra配線相当の環境変数を設定する
    (実際のinfra/template.yaml配線はD9解放待ちの別PRだが、コード側の挙動は
    env var/config済みの状態として検証できる)。"""
    real_config = load_config()
    enabled_incident_notification = real_config.incident_notification.model_copy(
        update={"issue_creation_enabled": True, "github_issue_claim_timeout_minutes": 10}
    )
    enabled_config = real_config.model_copy(
        update={"incident_notification": enabled_incident_notification}
    )
    monkeypatch.setattr(handler_module, "load_config", lambda: enabled_config)
    monkeypatch.setenv("GITHUB_APP_SECRET_ARN", aws_env)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    return enabled_config


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
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return _FakeResponse(response)


def _token_response(now: dt.datetime) -> dict[str, Any]:
    expires_at = (now + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"token": "ghs_dummy", "expires_at": expires_at}


def _issue_response(number: int = 1, state: str = "open") -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "state": state,
        "body": "",
    }


_NOW = dt.datetime(2026, 9, 25, 0, 0, tzinfo=dt.UTC)


# --- LINE成功 × GitHub成功 -------------------------------------------------------


def test_line_success_and_github_success(
    monkeypatch: pytest.MonkeyPatch,
    recording_line_client: _RecordingLineClient,
    github_enabled,
) -> None:
    fake = _FakeUrlopen([_token_response(_NOW), _issue_response(42)])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)
    message = _alarm_message()

    result = handler_module.handler(_sns_event(message), None)

    assert result == {"processed": 1}
    assert len(recording_line_client.sent) == 1
    state = tracker.get_incident_state(_fingerprint_of(message))
    assert state["status"] == "SENT"
    assert state["github_issue_create_status"] == "CREATED"
    assert state["github_issue_number"] == 42


# --- LINE成功 × GitHub失敗(★最重要) ----------------------------------------------


def test_line_success_and_github_failure_does_not_fail_the_handler(
    monkeypatch: pytest.MonkeyPatch,
    recording_line_client: _RecordingLineClient,
    github_enabled,
) -> None:
    """★ 最重要要件の直接固定: GitHub側がAPI呼び出しで失敗しても、handler全体は
    成功として返り、LINEは正しく送信・SENT記録される。"""
    import urllib.error

    fake = _FakeUrlopen([urllib.error.URLError("github api down")])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)
    message = _alarm_message()

    result = handler_module.handler(_sns_event(message), None)  # 例外を投げないこと自体が検証

    assert result == {"processed": 1}
    assert len(recording_line_client.sent) == 1  # LINEは影響を受けていない
    state = tracker.get_incident_state(_fingerprint_of(message))
    assert state["status"] == "SENT"  # LINE側のclaimは正常に完了している
    assert state["github_issue_create_status"] == "ISSUE_CREATION_FAILED"


def test_line_success_and_github_unexpected_bug_does_not_fail_the_handler(
    monkeypatch: pytest.MonkeyPatch,
    recording_line_client: _RecordingLineClient,
    github_enabled,
) -> None:
    """★ GitHub側の未知のバグ(GithubApiError/GithubConfigurationErrorではない
    予期しない例外)であっても、handlerへ一切伝播しない。"""
    from jstock_advisor.services import incident_github_issue_service

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(incident_github_issue_service, "_process", _boom)
    message = _alarm_message()

    result = handler_module.handler(_sns_event(message), None)

    assert result == {"processed": 1}
    assert len(recording_line_client.sent) == 1


def test_line_success_and_notice_construction_bug_does_not_fail_the_handler(
    monkeypatch: pytest.MonkeyPatch,
    recording_line_client: _RecordingLineClient,
    github_enabled,
) -> None:
    """★ 最重要要件の反証(PR #563レビュー対応で追加した防衛線の固定):
    `incident_github_issue_service.process_incident_issue()`のtry/exceptより
    "手前"のコード(`IncidentIssueNotice`構築等)がバグで例外を投げても、
    handler全体は成功として返り、LINEは影響を受けない。"""

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("bug in notice construction, before process_incident_issue is called")

    monkeypatch.setattr(handler_module, "IncidentIssueNotice", _boom)
    message = _alarm_message()

    result = handler_module.handler(_sns_event(message), None)

    assert result == {"processed": 1}
    assert len(recording_line_client.sent) == 1
    state = tracker.get_incident_state(_fingerprint_of(message))
    assert state["status"] == "SENT"
    assert "github_issue_create_status" not in state  # 試行自体が例外で止まっている


# --- LINE失敗 × GitHub成功(★最重要) ----------------------------------------------


def test_line_failure_on_brand_new_incident_defers_github_to_the_next_retry(
    monkeypatch: pytest.MonkeyPatch,
    failing_line_client: _FailingLineClient,
    github_enabled,
) -> None:
    """★ 反証(実装中に実測で検知した重大な競合の固定): 初出(CLAIMED_NEW)の
    incidentでLINE送信が失敗すると、既存契約(#503)の`release_claim(is_new=True)`が
    fingerprint行そのものをDeleteItemする。この直後にGitHub側が書き込むと、
    status欠落の部分的な行が復活し、以降のLINE再claim(`_put_new`の
    `attribute_not_exists(fingerprint)`)が永久に失敗する重大な回帰になる。
    このためCLAIMED_NEW×LINE失敗の組み合わせでは、GitHub側は今回は試行せず、
    SNS/Lambda retryによる次のCLAIMED_NEWへ委ねることを固定する。
    """
    fake = _FakeUrlopen([])  # GitHubは一切呼ばれないはず
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)
    message = _alarm_message()

    with pytest.raises(RuntimeError, match="LINE API unavailable"):
        handler_module.handler(_sns_event(message), None)

    assert fake.requests == []  # GitHub側は試行されていない
    assert (
        tracker.get_incident_state(_fingerprint_of(message)) is None
    )  # 行は削除済み(#503既存契約)


def test_retry_after_deferred_github_succeeds_normally(
    monkeypatch: pytest.MonkeyPatch,
    failing_line_client: _FailingLineClient,
    recording_line_client: _RecordingLineClient,
    github_enabled,
) -> None:
    """↑の直後、SNS/Lambda retryが来てLINEが今度は成功すれば、その回のCLAIMED_NEWで
    GitHub Issueも正常に作成される(取りこぼしなく、次のretryで必ず試行される)。
    """
    monkeypatch.setattr(
        handler_module, "build_live_line_client_from_env", lambda: failing_line_client
    )
    message = _alarm_message()
    with pytest.raises(RuntimeError):
        handler_module.handler(_sns_event(message), None)

    monkeypatch.setattr(
        handler_module, "build_live_line_client_from_env", lambda: recording_line_client
    )
    fake = _FakeUrlopen([_token_response(_NOW), _issue_response(42)])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    result = handler_module.handler(_sns_event(message), None)

    assert result == {"processed": 1}
    assert len(recording_line_client.sent) == 1
    state = tracker.get_incident_state(_fingerprint_of(message))
    assert state["status"] == "SENT"
    assert state["github_issue_create_status"] == "CREATED"
    assert state["github_issue_number"] == 42


def test_line_failure_on_a_recurrence_still_lets_github_post_a_comment(
    monkeypatch: pytest.MonkeyPatch,
    recording_line_client: _RecordingLineClient,
    github_enabled,
) -> None:
    """★ 最重要要件の直接固定(is_new=Falseの場合): dedup window経過後の再発
    (CLAIMED_AFTER_DEDUP_WINDOW)でLINE送信が失敗しても、この場合の
    `release_claim(is_new=False)`はfingerprint行を削除しない(claimed_atを
    巻き戻すのみ)ため、GitHub側は安全に独立して試行・成功できる。

    `handler()`は`now`を内部で`dt.datetime.now()`から取得し引数化されていないため、
    時刻を制御できる`_process_signal()`を直接呼ぶ(#503既存テストにも同種の
    handler経由でのdedup_window制御precedentは無い)。
    """
    message = _alarm_message()
    signal = handler_module._normalize_alarm_message(message, _NOW)

    # 1回目: LINE・GitHubとも成功させ、既存Issueを作っておく。
    fake1 = _FakeUrlopen([_token_response(_NOW), _issue_response(42)])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake1)
    handler_module._process_signal(signal, github_enabled, _NOW)
    assert len(recording_line_client.sent) == 1

    # 2回目: dedup_window(既定30分)経過後の再発。LINEを失敗させる。
    second_now = _NOW + dt.timedelta(minutes=31)
    monkeypatch.setattr(
        handler_module, "build_live_line_client_from_env", lambda: _FailingLineClient()
    )
    fake2 = _FakeUrlopen([_token_response(second_now), _issue_response(42, state="open"), None])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake2)

    with pytest.raises(RuntimeError, match="LINE API unavailable"):
        handler_module._process_signal(signal, github_enabled, second_now)

    state = tracker.get_incident_state(_fingerprint_of(message))
    assert state["status"] == "CLAIMED"  # release_claim(is_new=False)。行は削除されていない
    assert state["occurrence_count"] == 1  # LINE失敗によりrelease_claimが+1を打ち消す(#503既存契約)
    assert state["github_issue_number"] == 42
    # GitHub側はLINEの成否に関わらず、読み取り時点のoccurrence_count(=1)で
    # 独立してコメント追記に成功する(★最重要要件)。
    assert state["last_commented_occurrence_count"] == 1


# --- config flag無効時の安全性(既定) ---------------------------------------------


def test_disabled_flag_makes_zero_github_calls(
    monkeypatch: pytest.MonkeyPatch, recording_line_client: _RecordingLineClient, aws_env: str
) -> None:
    """既定(issue_creation_enabled=false)では、env varが設定されていても
    GitHub APIを一切呼ばないこと。"""
    monkeypatch.setenv("GITHUB_APP_SECRET_ARN", aws_env)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    fake = _FakeUrlopen([])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)
    message = _alarm_message()

    result = handler_module.handler(_sns_event(message), None)

    assert result == {"processed": 1}
    assert fake.requests == []
    state = tracker.get_incident_state(_fingerprint_of(message))
    assert "github_issue_create_status" not in state


def test_default_env_without_wiring_makes_zero_github_calls(
    monkeypatch: pytest.MonkeyPatch, recording_line_client: _RecordingLineClient, aws_env: str
) -> None:
    """infra配線(GITHUB_APP_SECRET_ARN/GITHUB_REPOSITORY)が無い現在の
    Production相当の状態でも安全にスキップされること(D9解放待ちの間の実際の状態)。"""
    monkeypatch.delenv("GITHUB_APP_SECRET_ARN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    fake = _FakeUrlopen([])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)
    message = _alarm_message()

    result = handler_module.handler(_sns_event(message), None)

    assert result == {"processed": 1}
    assert fake.requests == []
