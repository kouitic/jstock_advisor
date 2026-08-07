"""GitHub App認証によるGitHub Issues REST APIクライアント(振り返り機能改修)。

GitHub Appの秘密鍵(private_key)からJWTを生成し、インストールアクセストークンを
取得したうえでIssues APIを呼ぶ。LINEクライアント(infrastructure/line/client.py)と
同様にurllib.requestを使い、このリポジトリでは新規に依存を増やさない(PyJWTのみ、
JWT署名専用に追加)。token・private_keyはいかなる例外メッセージ・ログにも含めない。

設定不備(Secret ARN未設定・不存在・AccessDenied・JSON不正・必須項目欠損)は
GithubConfigurationErrorとして、API呼び出し自体の失敗(5xx・タイムアウト・
レート制限等)はGithubApiErrorとして区別する(呼び出し側のgithub_issue_service.pyが
ImprovementTaskStatus.CONFIGURATION_ERROR/ISSUE_CREATION_FAILEDを使い分けるため)。
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import boto3
import jwt
from botocore.exceptions import ClientError

_API_BASE = "https://api.github.com"
# GitHub App JWTの有効期限は最大10分。処理時間のマージンを見て9分30秒とする。
_JWT_TTL_SECONDS = 570
# クロックスキュー対策(GitHub推奨: iatを60秒過去にする)。
_JWT_CLOCK_SKEW_SECONDS = 60
# インストールアクセストークンの実際の失効時刻より手前で切り上げる安全マージン。
_TOKEN_EXPIRY_MARGIN_MINUTES = 2


class GithubConfigurationError(Exception):
    """issue_creation_enabled=trueなのにGitHub認証情報が不備・取得失敗している状態。

    運用設定の問題であり、GitHub API自体の障害(GithubApiError)とは区別する。
    """


class GithubApiError(Exception):
    """設定は正常だがGitHub API呼び出し自体が失敗した場合(5xx・タイムアウト・
    レート制限等)。"""


@dataclass(frozen=True)
class GithubAppCredentials:
    app_id: str
    installation_id: str
    private_key: str


@dataclass(frozen=True)
class GithubIssue:
    number: int
    html_url: str
    state: str  # "open" | "closed"
    body: str | None


def load_credentials_from_secrets_manager(secret_arn: str) -> GithubAppCredentials:
    """Secret ARNからGitHub App認証情報を実行時に取得する(振り返り機能改修
    決定事項15: private_keyをLambda環境変数へ直接展開せず、ARNのみを渡し
    secretsmanager:GetSecretValueで取得する)。"""
    client = boto3.client("secretsmanager")
    try:
        response = client.get_secret_value(SecretId=secret_arn)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "unknown")
        raise GithubConfigurationError(f"GitHub Secretsの取得に失敗しました: {code}") from e

    secret_string = response.get("SecretString")
    if not secret_string:
        raise GithubConfigurationError("GitHub SecretsにSecretStringがありません")
    try:
        payload = json.loads(secret_string)
    except json.JSONDecodeError as e:
        raise GithubConfigurationError("GitHub SecretsのJSON形式が不正です") from e
    if not isinstance(payload, dict):
        raise GithubConfigurationError("GitHub Secretsの形式が不正です(辞書ではありません)")

    missing = [key for key in ("app_id", "installation_id", "private_key") if not payload.get(key)]
    if missing:
        raise GithubConfigurationError(f"GitHub Secretsに必須項目が不足しています: {missing}")

    return GithubAppCredentials(
        app_id=str(payload["app_id"]),
        installation_id=str(payload["installation_id"]),
        private_key=str(payload["private_key"]),
    )


class GithubIssueClient:
    def __init__(self, owner: str, repo: str, credentials: GithubAppCredentials) -> None:
        self._owner = owner
        self._repo = repo
        self._credentials = credentials
        self._installation_token: str | None = None
        self._installation_token_expires_at: dt.datetime | None = None

    def _generate_app_jwt(self, now: dt.datetime) -> str:
        payload = {
            "iat": int(now.timestamp()) - _JWT_CLOCK_SKEW_SECONDS,
            "exp": int(now.timestamp()) + _JWT_TTL_SECONDS,
            "iss": self._credentials.app_id,
        }
        try:
            return jwt.encode(payload, self._credentials.private_key, algorithm="RS256")
        except (ValueError, TypeError, jwt.exceptions.PyJWTError) as e:
            # private_keyがPEM形式として不正な場合(GitHub App設定不備)。
            raise GithubConfigurationError("GitHub App private_keyの形式が不正です") from e

    def _installation_token_valid(self, now: dt.datetime) -> bool:
        return (
            self._installation_token is not None
            and self._installation_token_expires_at is not None
            and now < self._installation_token_expires_at
        )

    def _get_installation_token(self, now: dt.datetime) -> str:
        if self._installation_token_valid(now):
            assert self._installation_token is not None
            return self._installation_token

        app_jwt = self._generate_app_jwt(now)
        data = self._request(
            "POST",
            f"/app/installations/{self._credentials.installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}"},
        )
        token = data["token"]
        expires_at = dt.datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        self._installation_token = token
        self._installation_token_expires_at = expires_at - dt.timedelta(
            minutes=_TOKEN_EXPIRY_MARGIN_MINUTES
        )
        return str(token)

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> Any:
        url = f"{_API_BASE}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        request_headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            **(headers or {}),
        }
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
                body = response.read().decode("utf-8")
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise GithubApiError(f"GitHub API呼び出しに失敗しました: {e.code} {detail}") from e
        except urllib.error.URLError as e:
            raise GithubApiError(f"GitHub APIへ接続できませんでした: {e.reason}") from e

    def _authed_request(
        self,
        method: str,
        path: str,
        now: dt.datetime,
        *,
        json_body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> Any:
        token = self._get_installation_token(now)
        return self._request(
            method,
            path,
            headers={"Authorization": f"Bearer {token}"},
            json_body=json_body,
            query=query,
        )

    @staticmethod
    def _to_issue(data: dict[str, Any]) -> GithubIssue:
        return GithubIssue(
            number=data["number"],
            html_url=data["html_url"],
            state=data["state"],
            body=data.get("body"),
        )

    def get_issue(self, issue_number: int, now: dt.datetime) -> GithubIssue:
        data = self._authed_request(
            "GET", f"/repos/{self._owner}/{self._repo}/issues/{issue_number}", now
        )
        return self._to_issue(data)

    def search_open_issue_by_marker(
        self, marker: str, label: str, now: dt.datetime
    ) -> GithubIssue | None:
        """stale claim復旧・reconciliation専用。ラベル+本文中のHTMLコメント
        マーカー(<!-- improvement_candidate_key: XXX -->)で既存Issueを検索する
        (通常時の主たる冪等制御はDynamoDB側が担う、このメソッドはあくまで
        「GitHubには作成済みだがDynamoDB更新だけ失敗した」場合の復旧用)。

        Closed Issueは復旧対象に含めない(Closed後の再発は新規Issue作成の扱いと
        するため、決定事項12参照)。検索クエリ自体もis:openで絞り込んだうえで、
        Search APIのインデックス反映遅延を考慮し、返ってきた各itemのstateも
        念のため確認する。
        """
        query = {
            "q": f'repo:{self._owner}/{self._repo} is:issue is:open label:{label} "{marker}"'
        }
        data = self._authed_request("GET", "/search/issues", now, query=query)
        for item in data.get("items", []):
            if item.get("state") == "open" and marker in (item.get("body") or ""):
                return self._to_issue(item)
        return None

    def create_issue(
        self, title: str, body: str, labels: list[str], now: dt.datetime
    ) -> GithubIssue:
        data = self._authed_request(
            "POST",
            f"/repos/{self._owner}/{self._repo}/issues",
            now,
            json_body={"title": title, "body": body, "labels": labels},
        )
        return self._to_issue(data)

    def create_comment(self, issue_number: int, body: str, now: dt.datetime) -> None:
        self._authed_request(
            "POST",
            f"/repos/{self._owner}/{self._repo}/issues/{issue_number}/comments",
            now,
            json_body={"body": body},
        )

    def find_comment_by_marker(
        self, issue_number: int, marker: str, now: dt.datetime
    ) -> bool:
        """stale comment claim復旧専用。指定Issueのコメント一覧からマーカーが
        既に投稿されているかを確認する。

        長期間運用されるIssueはコメント数がper_page(100)を超えうるため、
        マーカーが見つかるまで次ページを取得する。見つかり次第即座に打ち切り、
        GitHub API呼び出し回数を最小限にする。
        """
        page = 1
        while True:
            data = self._authed_request(
                "GET",
                f"/repos/{self._owner}/{self._repo}/issues/{issue_number}/comments",
                now,
                query={"per_page": "100", "page": str(page)},
            )
            if any(marker in (item.get("body") or "") for item in data):
                return True
            if len(data) < 100:
                return False
            page += 1
