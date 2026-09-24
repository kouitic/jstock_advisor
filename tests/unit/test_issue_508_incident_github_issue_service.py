"""services/incident_github_issue_service.pyのテスト(Issue #508)。

DynamoDB(incident_state_tracker)はmotoの実テーブル、GitHub APIは
urllib.request.urlopenのスタブ化で検証する(実APIへは接続しない)。
`services/github_issue_service.py`のテスト(test_github_issue_service.py)と
同じ手法を踏襲する。
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
from jstock_advisor.domain.notification.incident_github_issue_message import (
    IncidentIssueNotice,
    issue_marker,
)
from jstock_advisor.domain.notification.incident_message import IncidentJob
from jstock_advisor.infrastructure.aws import incident_state_tracker as tracker
from jstock_advisor.infrastructure.github import client as github_client_module
from jstock_advisor.services import incident_github_issue_service

_REGION = "ap-northeast-1"
_NOW = dt.datetime(2026, 9, 25, 0, 0, tzinfo=dt.UTC)
_SECRET_NAME = "github-app-incident"
_FP = "c" * 64


def _generate_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=NoEncryption(),
    )
    return pem.decode("utf-8")


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "jstock")
    with mock_aws():
        dynamo = boto3.client("dynamodb", region_name=_REGION)
        dynamo.create_table(
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
def config():
    cfg = load_config()
    return cfg.incident_notification.model_copy(
        update={"issue_creation_enabled": True, "github_issue_claim_timeout_minutes": 10}
    )


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


def _token_response(now: dt.datetime = _NOW) -> dict[str, Any]:
    expires_at = (now + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"token": "ghs_dummy", "expires_at": expires_at}


def _issue_response(number: int = 1, state: str = "open") -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "state": state,
        "body": "",
    }


def _search_response(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items}


# --- 設定状態の区別 --------------------------------------------------------------


def test_disabled_config_skips_without_any_call(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    fake = _FakeUrlopen([])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)
    disabled = config.model_copy(update={"issue_creation_enabled": False})

    incident_github_issue_service.process_incident_issue(
        _notice(), disabled, _NOW, repo_owner="owner", repo_name="repo", github_secret_arn=aws_env
    )

    assert fake.requests == []
    assert tracker.get_incident_state(_FP) is None


def test_missing_secret_arn_is_configuration_error(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    fake = _FakeUrlopen([])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    incident_github_issue_service.process_incident_issue(
        _notice(), config, _NOW, repo_owner="owner", repo_name="repo", github_secret_arn=None
    )

    assert fake.requests == []
    state = tracker.get_incident_state(_FP)
    assert state["github_issue_create_status"] == "CONFIGURATION_ERROR"


@pytest.mark.parametrize(("repo_owner", "repo_name"), [(None, "repo"), ("owner", None)])
def test_missing_repo_owner_or_name_is_configuration_error(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config, repo_owner, repo_name
) -> None:
    fake = _FakeUrlopen([])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    incident_github_issue_service.process_incident_issue(
        _notice(),
        config,
        _NOW,
        repo_owner=repo_owner,
        repo_name=repo_name,
        github_secret_arn=aws_env,
    )

    assert fake.requests == []
    state = tracker.get_incident_state(_FP)
    assert state["github_issue_create_status"] == "CONFIGURATION_ERROR"


def test_malformed_secret_is_configuration_error(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    secretsmanager = boto3.client("secretsmanager", region_name=_REGION)
    secretsmanager.create_secret(Name="bad-secret", SecretString="not json")
    bad_arn = secretsmanager.describe_secret(SecretId="bad-secret")["ARN"]

    incident_github_issue_service.process_incident_issue(
        _notice(), config, _NOW, repo_owner="owner", repo_name="repo", github_secret_arn=bad_arn
    )

    state = tracker.get_incident_state(_FP)
    assert state["github_issue_create_status"] == "CONFIGURATION_ERROR"


# --- 新規Issue作成 ----------------------------------------------------------------


def test_creates_new_issue_for_first_occurrence(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    fake = _FakeUrlopen([_token_response(), _issue_response(42)])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    incident_github_issue_service.process_incident_issue(
        _notice(), config, _NOW, repo_owner="owner", repo_name="repo", github_secret_arn=aws_env
    )

    state = tracker.get_incident_state(_FP)
    assert state["github_issue_create_status"] == "CREATED"
    assert state["github_issue_number"] == 42


def test_github_api_error_marks_issue_creation_failed(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    import urllib.error

    fake = _FakeUrlopen([_token_response(), urllib.error.URLError("boom")])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    incident_github_issue_service.process_incident_issue(
        _notice(), config, _NOW, repo_owner="owner", repo_name="repo", github_secret_arn=aws_env
    )

    state = tracker.get_incident_state(_FP)
    assert state["github_issue_create_status"] == "ISSUE_CREATION_FAILED"
    assert "github_issue_claimed_at" not in state  # 次occurrenceで即座に再claim可能


def test_unexpected_exception_is_swallowed_and_never_propagates(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    """★ 最重要要件の反証: process_incident_issue()自体が予期しない例外(バグ等)を
    投げても、呼び出し元(incident_notifier_handler.py)へは一切伝播しない。"""

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("unexpected bug in github client construction")

    monkeypatch.setattr(incident_github_issue_service, "_process", _boom)

    incident_github_issue_service.process_incident_issue(
        _notice(), config, _NOW, repo_owner="owner", repo_name="repo", github_secret_arn=aws_env
    )
    # 例外が伝播せずここへ到達すれば成功。


# --- 再発: 既存OPEN Issueへコメント追記 -------------------------------------------


def test_recurrence_with_open_issue_posts_a_comment(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    fake = _FakeUrlopen([_token_response(), _issue_response(42)])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)
    incident_github_issue_service.process_incident_issue(
        _notice(occurrence_count=1),
        config,
        _NOW,
        repo_owner="owner",
        repo_name="repo",
        github_secret_arn=aws_env,
    )

    second_now = _NOW + dt.timedelta(hours=1)
    fake2 = _FakeUrlopen([_token_response(second_now), _issue_response(42, state="open"), None])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake2)
    incident_github_issue_service.process_incident_issue(
        _notice(occurrence_count=2),
        config,
        second_now,
        repo_owner="owner",
        repo_name="repo",
        github_secret_arn=aws_env,
    )

    state = tracker.get_incident_state(_FP)
    assert state["github_issue_number"] == 42  # 新規Issueは作られていない
    assert state["last_commented_occurrence_count"] == 2
    # リクエストは token取得 -> get_issue -> create_comment の3件
    assert len(fake2.requests) == 3


def test_recurrence_for_the_same_occurrence_is_not_double_posted(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    """★ 反証: Lambda retryで同一occurrenceが2回呼ばれても、コメントは1回だけ。"""
    fake = _FakeUrlopen([_token_response(), _issue_response(42)])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)
    incident_github_issue_service.process_incident_issue(
        _notice(occurrence_count=1),
        config,
        _NOW,
        repo_owner="owner",
        repo_name="repo",
        github_secret_arn=aws_env,
    )

    fake2 = _FakeUrlopen([_token_response(), _issue_response(42, state="open"), None])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake2)
    incident_github_issue_service.process_incident_issue(
        _notice(occurrence_count=2),
        config,
        _NOW,
        repo_owner="owner",
        repo_name="repo",
        github_secret_arn=aws_env,
    )

    # 同じoccurrence_count=2が2回目に呼ばれる(retry相当)。
    fake3 = _FakeUrlopen([_token_response(), _issue_response(42, state="open")])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake3)
    incident_github_issue_service.process_incident_issue(
        _notice(occurrence_count=2),
        config,
        _NOW,
        repo_owner="owner",
        repo_name="repo",
        github_secret_arn=aws_env,
    )

    # get_issueだけ呼ばれ、create_commentは呼ばれない(token取得+get_issueの2件のみ)。
    assert len(fake3.requests) == 2


# --- 再発: 既存CLOSED Issueは新規Issue(reopenしない) -----------------------------


def test_recurrence_with_closed_issue_creates_a_new_issue_with_reference(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    fake = _FakeUrlopen([_token_response(), _issue_response(42)])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)
    incident_github_issue_service.process_incident_issue(
        _notice(occurrence_count=1),
        config,
        _NOW,
        repo_owner="owner",
        repo_name="repo",
        github_secret_arn=aws_env,
    )

    second_now = _NOW + dt.timedelta(hours=1)
    fake2 = _FakeUrlopen(
        [_token_response(second_now), _issue_response(42, state="closed"), _issue_response(99)]
    )
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake2)
    incident_github_issue_service.process_incident_issue(
        _notice(occurrence_count=2),
        config,
        second_now,
        repo_owner="owner",
        repo_name="repo",
        github_secret_arn=aws_env,
    )

    state = tracker.get_incident_state(_FP)
    assert state["github_issue_number"] == 99
    assert state["previous_github_issue_number"] == 42
    create_issue_request = fake2.requests[-1]
    body = json.loads(create_issue_request.data.decode("utf-8"))["body"]
    assert "Previous issue: #42" in body


# --- stale claim reconciliation ---------------------------------------------------


def test_stale_claim_reconciliation_finds_existing_issue_and_does_not_create_a_duplicate(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    """Issue作成成功後、IncidentState保存前にcrashしたケースの復旧。"""
    tracker.try_claim_new_github_issue_creation(
        _FP, _NOW, config.github_issue_claim_timeout_minutes
    )

    marker = issue_marker(_FP)
    fake = _FakeUrlopen(
        [
            _token_response(),
            _search_response([{"number": 7, "state": "open", "body": marker, "html_url": "x"}]),
        ]
    )
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    incident_github_issue_service.process_incident_issue(
        _notice(),
        config,
        _NOW + dt.timedelta(minutes=config.github_issue_claim_timeout_minutes + 1),
        repo_owner="owner",
        repo_name="repo",
        github_secret_arn=aws_env,
    )

    state = tracker.get_incident_state(_FP)
    assert state["github_issue_create_status"] == "CREATED"
    assert state["github_issue_number"] == 7
    # create_issueは呼ばれていない(token取得+searchの2件のみ)。
    assert len(fake.requests) == 2


def test_stale_claim_reconciliation_creates_when_not_found_on_github(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    tracker.try_claim_new_github_issue_creation(
        _FP, _NOW, config.github_issue_claim_timeout_minutes
    )

    fake = _FakeUrlopen([_token_response(), _search_response([]), _issue_response(55)])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    incident_github_issue_service.process_incident_issue(
        _notice(),
        config,
        _NOW + dt.timedelta(minutes=config.github_issue_claim_timeout_minutes + 1),
        repo_owner="owner",
        repo_name="repo",
        github_secret_arn=aws_env,
    )

    state = tracker.get_incident_state(_FP)
    assert state["github_issue_create_status"] == "CREATED"
    assert state["github_issue_number"] == 55


def test_claim_not_yet_stale_does_not_reconcile_or_create(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    """★ 反証: claimがまだ有効(未失効)の間は、他実行は何もしない(GitHub APIを
    一切呼ばない)。"""
    tracker.try_claim_new_github_issue_creation(
        _FP, _NOW, config.github_issue_claim_timeout_minutes
    )

    fake = _FakeUrlopen([])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    incident_github_issue_service.process_incident_issue(
        _notice(),
        config,
        _NOW + dt.timedelta(minutes=1),  # timeout=10分。まだ失効していない
        repo_owner="owner",
        repo_name="repo",
        github_secret_arn=aws_env,
    )

    assert fake.requests == []
    state = tracker.get_incident_state(_FP)
    assert state["github_issue_create_status"] == "CREATING"
