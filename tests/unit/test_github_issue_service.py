"""services/github_issue_service.pyのテスト(振り返り機能改修)。

DynamoDB(improvement_task_tracker)はmotoの実テーブル、GitHub APIは
urllib.request.urlopenのスタブ化で検証する(実APIへは接続しない)。
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
from jstock_advisor.domain.entities.enums import (
    ImprovementAction,
    ImprovementPriority,
    ImprovementTaskStatus,
    RecommendationType,
)
from jstock_advisor.domain.entities.improvement import (
    PROBLEM_CATEGORY_EVALUATION_CRITERIA_UNDEFINED,
    PROBLEM_CATEGORY_PERFORMANCE_DEGRADED,
    ImprovementCandidate,
)
from jstock_advisor.infrastructure.aws import improvement_task_tracker as tracker
from jstock_advisor.infrastructure.github import client as github_client_module
from jstock_advisor.services import github_issue_service

_REGION = "ap-northeast-1"
_NOW = dt.datetime(2026, 8, 10, 9, 0, tzinfo=dt.UTC)
_SECRET_NAME = "github-app"


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
    with mock_aws():
        dynamo = boto3.client("dynamodb", region_name=_REGION)
        dynamo.create_table(
            TableName="jstock-improvement_tasks",
            KeySchema=[{"AttributeName": "candidate_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "candidate_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        secretsmanager = boto3.client("secretsmanager", region_name=_REGION)
        secretsmanager.create_secret(
            Name=_SECRET_NAME,
            SecretString=json.dumps(
                {
                    "app_id": "1",
                    "installation_id": "2",
                    "private_key": _generate_private_key_pem(),
                }
            ),
        )
        secret_arn = secretsmanager.describe_secret(SecretId=_SECRET_NAME)["ARN"]
        yield secret_arn


@pytest.fixture
def config():
    cfg = load_config()
    return cfg.review_improvement.model_copy(
        update={"issue_creation_enabled": True, "github_issue_claim_timeout_minutes": 10}
    )


def _candidate(
    is_current: bool | None = True,
    problem_category: str = PROBLEM_CATEGORY_PERFORMANCE_DEGRADED,
    rule_version: str = "v11",
) -> ImprovementCandidate:
    return ImprovementCandidate(
        candidate_id=f"BUY|{rule_version}|ALL|{problem_category}|2026-W32",
        candidate_key=f"BUY|{rule_version}|ALL|{problem_category}",
        recommendation_type=RecommendationType.BUY,
        rule_version=rule_version,
        review_week="2026-W32",
        evaluation_period_start=dt.date(2026, 8, 3),
        evaluation_period_end=dt.date(2026, 8, 9),
        sample_count=30,
        conclusive_count=30,
        success_rate_pct=45.0,
        average_return_pct=1.0,
        average_excess_return_pct=-1.5,
        consecutive_bad_weeks=2,
        priority=ImprovementPriority.B,
        problem_category=problem_category,
        reason_codes=("SUCCESS_RATE_LOW", "WEEK_OVER_WEEK_DROP"),
        recommended_action=ImprovementAction.ADJUST_THRESHOLD,
        is_current_rule_version=is_current,
    )


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


# --- 設定状態の区別 ----------------------------------------------------


def test_disabled_config_skips_without_github_calls(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    fake = _FakeUrlopen([])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)
    disabled_config = config.model_copy(update={"issue_creation_enabled": False})

    status = github_issue_service.process_candidate(
        _candidate(), "2026-W32", _NOW, disabled_config, "owner", "repo", aws_env
    )

    assert status == ImprovementTaskStatus.SKIPPED_NOT_CONFIGURED
    assert fake.requests == []


def test_missing_secret_arn_is_configuration_error(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    fake = _FakeUrlopen([])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    status = github_issue_service.process_candidate(
        _candidate(), "2026-W32", _NOW, config, "owner", "repo", None
    )

    assert status == ImprovementTaskStatus.CONFIGURATION_ERROR
    assert fake.requests == []


def test_malformed_secret_is_configuration_error(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    secretsmanager = boto3.client("secretsmanager", region_name=_REGION)
    secretsmanager.create_secret(Name="bad-secret", SecretString="not json")
    bad_arn = secretsmanager.describe_secret(SecretId="bad-secret")["ARN"]

    status = github_issue_service.process_candidate(
        _candidate(), "2026-W32", _NOW, config, "owner", "repo", bad_arn
    )

    assert status == ImprovementTaskStatus.CONFIGURATION_ERROR


# --- 新規Issue作成 -------------------------------------------------------


def test_creates_new_issue_for_current_rule_version(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    fake = _FakeUrlopen([_token_response(), _issue_response(42)])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    status = github_issue_service.process_candidate(
        _candidate(is_current=True), "2026-W32", _NOW, config, "owner", "repo", aws_env
    )

    assert status == ImprovementTaskStatus.ISSUE_CREATED
    task = tracker.get_improvement_task(_candidate().candidate_key)
    assert task is not None
    assert task["github_issue_number"] == 42


def test_does_not_create_issue_for_past_rule_version(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    fake = _FakeUrlopen([_token_response()])  # 認証は行うがIssue作成は呼ばれない想定
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    status = github_issue_service.process_candidate(
        _candidate(is_current=False), "2026-W32", _NOW, config, "owner", "repo", aws_env
    )

    assert status == ImprovementTaskStatus.CANDIDATE
    create_calls = [r for r in fake.requests if r.full_url.endswith("/repos/owner/repo/issues")]
    assert create_calls == []


def test_does_not_create_issue_when_current_rule_version_unknown(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    fake = _FakeUrlopen([_token_response()])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    status = github_issue_service.process_candidate(
        _candidate(is_current=None), "2026-W32", _NOW, config, "owner", "repo", aws_env
    )
    assert status == ImprovementTaskStatus.CANDIDATE


def test_github_api_failure_marks_issue_creation_failed(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    import urllib.error

    http_error = urllib.error.HTTPError(
        url="https://api.github.com/repos/owner/repo/issues",
        code=500,
        msg="boom",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    monkeypatch.setattr(http_error, "read", lambda: b"{}")
    fake = _FakeUrlopen([_token_response(), http_error])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    status = github_issue_service.process_candidate(
        _candidate(), "2026-W32", _NOW, config, "owner", "repo", aws_env
    )
    assert status == ImprovementTaskStatus.ISSUE_CREATION_FAILED


def test_evaluation_criteria_undefined_candidate_can_create_issue_on_first_week(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    fake = _FakeUrlopen([_token_response(), _issue_response(9)])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)
    candidate = _candidate(
        is_current=True, problem_category=PROBLEM_CATEGORY_EVALUATION_CRITERIA_UNDEFINED
    )

    status = github_issue_service.process_candidate(
        candidate, "2026-W32", _NOW, config, "owner", "repo", aws_env
    )
    assert status == ImprovementTaskStatus.ISSUE_CREATED


# --- 既存Issueへのコメント / Closed再発 -----------------------------------


def test_posts_comment_to_existing_open_issue(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    candidate = _candidate()
    tracker.ensure_task_exists(
        candidate.candidate_key,
        candidate.recommendation_type,
        candidate.rule_version,
        candidate.segment_key,
        candidate.priority,
        _NOW,
    )
    tracker.mark_issue_created(
        candidate.candidate_key, 100, "https://github.com/o/r/issues/100", _NOW
    )

    later = _NOW + dt.timedelta(days=7)
    fake = _FakeUrlopen([_token_response(later), _issue_response(100, state="open"), None])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    status = github_issue_service.process_candidate(
        candidate, "2026-W33", later, config, "owner", "repo", aws_env
    )

    assert status == ImprovementTaskStatus.ISSUE_CREATED
    task = tracker.get_improvement_task(candidate.candidate_key)
    assert task is not None
    assert task["last_commented_review_week"] == "2026-W33"


def test_does_not_repost_comment_for_same_week(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    candidate = _candidate()
    tracker.ensure_task_exists(
        candidate.candidate_key,
        candidate.recommendation_type,
        candidate.rule_version,
        candidate.segment_key,
        candidate.priority,
        _NOW,
    )
    tracker.mark_issue_created(
        candidate.candidate_key, 100, "https://github.com/o/r/issues/100", _NOW
    )
    tracker.mark_comment_posted(candidate.candidate_key, "2026-W33", _NOW)

    fake = _FakeUrlopen([_token_response(), _issue_response(100, state="open")])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    status = github_issue_service.process_candidate(
        candidate, "2026-W33", _NOW + dt.timedelta(hours=1), config, "owner", "repo", aws_env
    )

    assert status == ImprovementTaskStatus.ISSUE_CREATED
    comment_calls = [r for r in fake.requests if "/comments" in r.full_url]
    assert comment_calls == []


def test_closed_issue_triggers_new_issue_with_previous_reference(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    candidate = _candidate()
    tracker.ensure_task_exists(
        candidate.candidate_key,
        candidate.recommendation_type,
        candidate.rule_version,
        candidate.segment_key,
        candidate.priority,
        _NOW,
    )
    tracker.mark_issue_created(
        candidate.candidate_key, 100, "https://github.com/o/r/issues/100", _NOW
    )

    later = _NOW + dt.timedelta(days=14)
    fake = _FakeUrlopen(
        [_token_response(later), _issue_response(100, state="closed"), _issue_response(200)]
    )
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    status = github_issue_service.process_candidate(
        candidate, "2026-W34", later, config, "owner", "repo", aws_env
    )

    assert status == ImprovementTaskStatus.ISSUE_CREATED
    task = tracker.get_improvement_task(candidate.candidate_key)
    assert task is not None
    assert task["github_issue_number"] == 200
    assert task["previous_github_issue_number"] == 100
    create_request = fake.requests[-1]
    payload = json.loads(create_request.data)
    assert "Previous issue: #100" in payload["body"]


# --- stale claim復旧(先にGitHub照合) -------------------------------------


def test_stale_issue_creating_recovers_from_github_without_duplicate(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    candidate = _candidate()
    tracker.ensure_task_exists(
        candidate.candidate_key,
        candidate.recommendation_type,
        candidate.rule_version,
        candidate.segment_key,
        candidate.priority,
        _NOW,
    )
    tracker.try_claim_new_issue_creation(candidate.candidate_key, _NOW, 10)
    much_later = _NOW + dt.timedelta(minutes=30)

    search_response = {
        "items": [
            {
                "number": 55,
                "html_url": "https://github.com/o/r/issues/55",
                "state": "open",
                "body": f"<!-- improvement_candidate_key: {candidate.candidate_key} -->",
            }
        ]
    }
    fake = _FakeUrlopen([_token_response(), search_response])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    status = github_issue_service.process_candidate(
        candidate, "2026-W32", much_later, config, "owner", "repo", aws_env
    )

    assert status == ImprovementTaskStatus.ISSUE_CREATED
    task = tracker.get_improvement_task(candidate.candidate_key)
    assert task is not None
    assert task["github_issue_number"] == 55
    create_calls = [
        r
        for r in fake.requests
        if r.get_method() == "POST" and "issues" in r.full_url and "search" not in r.full_url
    ]
    assert create_calls == []  # 新規Issueは作成されていない


def test_stale_issue_creating_reclaims_and_creates_when_not_found(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    candidate = _candidate()
    tracker.ensure_task_exists(
        candidate.candidate_key,
        candidate.recommendation_type,
        candidate.rule_version,
        candidate.segment_key,
        candidate.priority,
        _NOW,
    )
    tracker.try_claim_new_issue_creation(candidate.candidate_key, _NOW, 10)
    much_later = _NOW + dt.timedelta(minutes=30)

    fake = _FakeUrlopen([_token_response(), {"items": []}, _issue_response(77)])
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    status = github_issue_service.process_candidate(
        candidate, "2026-W32", much_later, config, "owner", "repo", aws_env
    )

    assert status == ImprovementTaskStatus.ISSUE_CREATED
    task = tracker.get_improvement_task(candidate.candidate_key)
    assert task is not None
    assert task["github_issue_number"] == 77


def test_active_issue_creating_claim_skips_without_github_calls_beyond_auth(
    monkeypatch: pytest.MonkeyPatch, aws_env: str, config
) -> None:
    candidate = _candidate()
    tracker.ensure_task_exists(
        candidate.candidate_key,
        candidate.recommendation_type,
        candidate.rule_version,
        candidate.segment_key,
        candidate.priority,
        _NOW,
    )
    tracker.try_claim_new_issue_creation(candidate.candidate_key, _NOW, 10)
    still_within_timeout = _NOW + dt.timedelta(minutes=5)

    fake = _FakeUrlopen([])  # 未失効のためGitHub API呼び出しは一切発生しないはず
    monkeypatch.setattr(github_client_module.urllib.request, "urlopen", fake)

    status = github_issue_service.process_candidate(
        candidate, "2026-W32", still_within_timeout, config, "owner", "repo", aws_env
    )
    assert status == ImprovementTaskStatus.ISSUE_CREATING
