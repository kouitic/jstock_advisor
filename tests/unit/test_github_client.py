"""infrastructure/github/client.pyのテスト(振り返り機能改修)。

実際のGitHub APIへは接続しない。urllib.request.urlopenをスタブ化する
(test_market_data_yfinance_impl.pyのmonkeypatch.setattr(module.yf, ...)パターンを
urllib向けに踏襲)。
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.error
from typing import Any

import boto3
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from moto import mock_aws

from jstock_advisor.infrastructure.github import client as module
from jstock_advisor.infrastructure.github.client import (
    GithubApiError,
    GithubAppCredentials,
    GithubConfigurationError,
    GithubIssueClient,
    load_credentials_from_secrets_manager,
)

_NOW = dt.datetime(2026, 8, 10, 9, 0, tzinfo=dt.UTC)
_REGION = "ap-northeast-1"


def _generate_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=NoEncryption(),
    )
    return pem.decode("utf-8")


@pytest.fixture
def credentials() -> GithubAppCredentials:
    return GithubAppCredentials(
        app_id="12345", installation_id="67890", private_key=_generate_private_key_pem()
    )


class _FakeResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _FakeUrlopen:
    """呼び出しごとにキューから応答を返す。各呼び出しのrequestを記録する。"""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: int = 15) -> _FakeResponse:
        self.requests.append(request)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return _FakeResponse(response)


def _install_token_response(token: str = "ghs_dummy") -> dict[str, Any]:
    expires_at = (_NOW + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"token": token, "expires_at": expires_at}


def _issue_payload(
    number: int = 1, state: str = "open", body: str | None = "body"
) -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "state": state,
        "body": body,
    }


# --- load_credentials_from_secrets_manager ---------------------------------


@pytest.fixture
def secretsmanager(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        yield boto3.client("secretsmanager", region_name=_REGION)


def test_load_credentials_success(secretsmanager) -> None:
    secretsmanager.create_secret(
        Name="github-app",
        SecretString=json.dumps(
            {"app_id": "1", "installation_id": "2", "private_key": "pem-data"}
        ),
    )
    arn = secretsmanager.describe_secret(SecretId="github-app")["ARN"]

    creds = load_credentials_from_secrets_manager(arn)
    assert creds.app_id == "1"
    assert creds.installation_id == "2"
    assert creds.private_key == "pem-data"


def test_load_credentials_missing_secret_raises_configuration_error(secretsmanager) -> None:
    with pytest.raises(GithubConfigurationError):
        load_credentials_from_secrets_manager(
            "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:not-there-xyz"
        )


def test_load_credentials_malformed_json_raises_configuration_error(secretsmanager) -> None:
    secretsmanager.create_secret(Name="github-app-bad", SecretString="not json")
    arn = secretsmanager.describe_secret(SecretId="github-app-bad")["ARN"]

    with pytest.raises(GithubConfigurationError):
        load_credentials_from_secrets_manager(arn)


def test_load_credentials_missing_required_key_raises_configuration_error(secretsmanager) -> None:
    secretsmanager.create_secret(
        Name="github-app-incomplete",
        SecretString=json.dumps({"app_id": "1", "installation_id": "2"}),
    )
    arn = secretsmanager.describe_secret(SecretId="github-app-incomplete")["ARN"]

    with pytest.raises(GithubConfigurationError):
        load_credentials_from_secrets_manager(arn)


# --- JWT生成 ------------------------------------------------------------


def test_generate_app_jwt_is_verifiable_with_public_key(credentials: GithubAppCredentials) -> None:
    client = GithubIssueClient("owner", "repo", credentials)
    token = client._generate_app_jwt(_NOW)  # noqa: SLF001

    # _NOWはテスト用の固定日時であり実際のウォールクロックとは無関係のため、
    # iat/exp(時刻ベース)のクレーム検証は無効化し、署名検証とissクレームのみ確認する。
    decoded = jwt.decode(
        token,
        key=_public_key_pem(credentials.private_key),
        algorithms=["RS256"],
        options={"verify_iat": False, "verify_exp": False},
    )
    assert decoded["iss"] == "12345"


def _public_key_pem(private_key_pem: str) -> str:
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
        load_pem_private_key,
    )

    key = load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    public_key = key.public_key()
    return public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode("utf-8")


def test_generate_app_jwt_rejects_invalid_private_key() -> None:
    bad_credentials = GithubAppCredentials(app_id="1", installation_id="2", private_key="not-a-key")
    client = GithubIssueClient("owner", "repo", bad_credentials)
    with pytest.raises(GithubConfigurationError):
        client._generate_app_jwt(_NOW)  # noqa: SLF001


# --- installation token caching --------------------------------------------


def test_installation_token_is_reused_within_expiry(
    monkeypatch: pytest.MonkeyPatch, credentials: GithubAppCredentials
) -> None:
    fake = _FakeUrlopen(
        [
            _install_token_response(),
            _issue_payload(1),
            _issue_payload(2),
        ]
    )
    monkeypatch.setattr(module.urllib.request, "urlopen", fake)
    client = GithubIssueClient("owner", "repo", credentials)

    client.get_issue(1, _NOW)
    client.get_issue(2, _NOW + dt.timedelta(minutes=5))

    token_requests = [r for r in fake.requests if "access_tokens" in r.full_url]
    assert len(token_requests) == 1


def test_installation_token_is_refetched_after_expiry(
    monkeypatch: pytest.MonkeyPatch, credentials: GithubAppCredentials
) -> None:
    fake = _FakeUrlopen(
        [
            _install_token_response(),
            _issue_payload(1),
            _install_token_response(),
            _issue_payload(2),
        ]
    )
    monkeypatch.setattr(module.urllib.request, "urlopen", fake)
    client = GithubIssueClient("owner", "repo", credentials)

    client.get_issue(1, _NOW)
    client.get_issue(2, _NOW + dt.timedelta(hours=2))

    token_requests = [r for r in fake.requests if "access_tokens" in r.full_url]
    assert len(token_requests) == 2


# --- Issue操作 ---------------------------------------------------------


def test_create_issue_sends_expected_payload(
    monkeypatch: pytest.MonkeyPatch, credentials: GithubAppCredentials
) -> None:
    fake = _FakeUrlopen([_install_token_response(), _issue_payload(42)])
    monkeypatch.setattr(module.urllib.request, "urlopen", fake)
    client = GithubIssueClient("owner", "repo", credentials)

    issue = client.create_issue("title", "body", ["rule-improvement"], _NOW)

    assert issue.number == 42
    create_request = fake.requests[-1]
    payload = json.loads(create_request.data)
    assert payload == {"title": "title", "body": "body", "labels": ["rule-improvement"]}
    assert create_request.get_method() == "POST"


def test_get_issue_parses_response(
    monkeypatch: pytest.MonkeyPatch, credentials: GithubAppCredentials
) -> None:
    fake = _FakeUrlopen([_install_token_response(), _issue_payload(7, state="closed")])
    monkeypatch.setattr(module.urllib.request, "urlopen", fake)
    client = GithubIssueClient("owner", "repo", credentials)

    issue = client.get_issue(7, _NOW)
    assert issue.state == "closed"


def test_search_open_issue_by_marker_finds_matching_body(
    monkeypatch: pytest.MonkeyPatch, credentials: GithubAppCredentials
) -> None:
    marker = "<!-- improvement_candidate_key: BUY|v1|ALL|PERFORMANCE_DEGRADED -->"
    search_response = {
        "items": [
            _issue_payload(1, body="unrelated"),
            _issue_payload(2, body=f"some text {marker} more text"),
        ]
    }
    fake = _FakeUrlopen([_install_token_response(), search_response])
    monkeypatch.setattr(module.urllib.request, "urlopen", fake)
    client = GithubIssueClient("owner", "repo", credentials)

    found = client.search_open_issue_by_marker(marker, "auto-generated", _NOW)
    assert found is not None
    assert found.number == 2


def test_search_open_issue_by_marker_returns_none_when_not_found(
    monkeypatch: pytest.MonkeyPatch, credentials: GithubAppCredentials
) -> None:
    fake = _FakeUrlopen([_install_token_response(), {"items": []}])
    monkeypatch.setattr(module.urllib.request, "urlopen", fake)
    client = GithubIssueClient("owner", "repo", credentials)

    assert client.search_open_issue_by_marker("marker", "auto-generated", _NOW) is None


def test_search_open_issue_by_marker_excludes_closed_only(
    monkeypatch: pytest.MonkeyPatch, credentials: GithubAppCredentials
) -> None:
    """検索APIのインデックス反映遅延等でClosed Issueが結果に混入しても、
    Closed Issueしか無い場合は復旧対象として選ばない(古いClosed Issueへの
    誤ったstale復旧を防ぐ、レビュー指摘②)。"""
    marker = "<!-- improvement_candidate_key: BUY|v1|ALL|PERFORMANCE_DEGRADED -->"
    search_response = {"items": [_issue_payload(1, state="closed", body=f"text {marker}")]}
    fake = _FakeUrlopen([_install_token_response(), search_response])
    monkeypatch.setattr(module.urllib.request, "urlopen", fake)
    client = GithubIssueClient("owner", "repo", credentials)

    assert client.search_open_issue_by_marker(marker, "auto-generated", _NOW) is None

    search_request = fake.requests[-1]
    assert "is%3Aopen" in search_request.full_url


def test_search_open_issue_by_marker_selects_open_over_closed(
    monkeypatch: pytest.MonkeyPatch, credentials: GithubAppCredentials
) -> None:
    """同一マーカーを含むClosed IssueとOpen Issueが両方結果に含まれる場合、
    必ずOpen Issueの方を選ぶこと(レビュー指摘②)。"""
    marker = "<!-- improvement_candidate_key: BUY|v1|ALL|PERFORMANCE_DEGRADED -->"
    search_response = {
        "items": [
            _issue_payload(1, state="closed", body=f"old text {marker}"),
            _issue_payload(2, state="open", body=f"new text {marker}"),
        ]
    }
    fake = _FakeUrlopen([_install_token_response(), search_response])
    monkeypatch.setattr(module.urllib.request, "urlopen", fake)
    client = GithubIssueClient("owner", "repo", credentials)

    found = client.search_open_issue_by_marker(marker, "auto-generated", _NOW)
    assert found is not None
    assert found.number == 2
    assert found.state == "open"


def test_create_comment_sends_body(
    monkeypatch: pytest.MonkeyPatch, credentials: GithubAppCredentials
) -> None:
    fake = _FakeUrlopen([_install_token_response(), None])
    monkeypatch.setattr(module.urllib.request, "urlopen", fake)
    client = GithubIssueClient("owner", "repo", credentials)

    client.create_comment(5, "comment body", _NOW)

    comment_request = fake.requests[-1]
    assert json.loads(comment_request.data) == {"body": "comment body"}


def test_find_comment_by_marker_true_when_present(
    monkeypatch: pytest.MonkeyPatch, credentials: GithubAppCredentials
) -> None:
    marker = "<!-- review_week: 2026-W32 -->"
    fake = _FakeUrlopen(
        [_install_token_response(), [{"body": "irrelevant"}, {"body": f"text {marker}"}]]
    )
    monkeypatch.setattr(module.urllib.request, "urlopen", fake)
    client = GithubIssueClient("owner", "repo", credentials)

    assert client.find_comment_by_marker(5, marker, _NOW) is True


def test_find_comment_by_marker_false_when_absent(
    monkeypatch: pytest.MonkeyPatch, credentials: GithubAppCredentials
) -> None:
    fake = _FakeUrlopen([_install_token_response(), [{"body": "irrelevant"}]])
    monkeypatch.setattr(module.urllib.request, "urlopen", fake)
    client = GithubIssueClient("owner", "repo", credentials)

    assert client.find_comment_by_marker(5, "marker", _NOW) is False


def test_find_comment_by_marker_searches_next_page(
    monkeypatch: pytest.MonkeyPatch, credentials: GithubAppCredentials
) -> None:
    """コメント数がper_page(100)を超える長期運用Issueでも、マーカーが後続ページに
    しか無い場合を見逃さないこと(レビュー指摘⑤)。"""
    marker = "<!-- review_week: 2026-W32 -->"
    first_page = [{"body": "irrelevant"} for _ in range(100)]
    second_page = [{"body": f"text {marker}"}]
    fake = _FakeUrlopen([_install_token_response(), first_page, second_page])
    monkeypatch.setattr(module.urllib.request, "urlopen", fake)
    client = GithubIssueClient("owner", "repo", credentials)

    assert client.find_comment_by_marker(5, marker, _NOW) is True

    comment_requests = [r for r in fake.requests if "/comments" in r.full_url]
    assert len(comment_requests) == 2


def test_find_comment_by_marker_stops_without_fetching_next_page_once_found(
    monkeypatch: pytest.MonkeyPatch, credentials: GithubAppCredentials
) -> None:
    """1ページ目(100件ちょうど)でマーカーが見つかった場合、2ページ目は取得せず
    即座に打ち切ること(GitHub API呼び出し回数を最小限にする、レビュー指摘⑤)。"""
    marker = "<!-- review_week: 2026-W32 -->"
    first_page = [{"body": "irrelevant"} for _ in range(99)] + [{"body": f"text {marker}"}]
    fake = _FakeUrlopen([_install_token_response(), first_page])
    monkeypatch.setattr(module.urllib.request, "urlopen", fake)
    client = GithubIssueClient("owner", "repo", credentials)

    assert client.find_comment_by_marker(5, marker, _NOW) is True

    comment_requests = [r for r in fake.requests if "/comments" in r.full_url]
    assert len(comment_requests) == 1


# --- エラー処理 ----------------------------------------------------------


def test_http_error_raises_github_api_error(
    monkeypatch: pytest.MonkeyPatch, credentials: GithubAppCredentials
) -> None:
    http_error = urllib.error.HTTPError(
        url="https://api.github.com/repos/owner/repo/issues/1",
        code=500,
        msg="Internal Server Error",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    monkeypatch.setattr(http_error, "read", lambda: b'{"message": "boom"}')
    fake = _FakeUrlopen([_install_token_response(), http_error])
    monkeypatch.setattr(module.urllib.request, "urlopen", fake)
    client = GithubIssueClient("owner", "repo", credentials)

    with pytest.raises(GithubApiError):
        client.get_issue(1, _NOW)
