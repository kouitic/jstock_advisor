"""incidentのGitHub Issue自動起票・コメント追記を行うサービス(Issue #508。
#132 X-9)。

`infrastructure.aws.incident_state_tracker`による原子的な状態遷移を主たる冪等制御と
し、GitHub側の実在確認(reconciliation)はstale claim検出時の復旧・二重チェック専用に
使う(通常時の主用途にはしない。`services/github_issue_service.py`〔週次改善レビュー〕
と同じ設計)。

★最重要要件(#508 Phase A設計): `process_incident_issue()`はいかなる例外も外へ
伝播させない。呼び出し元(`lambda_handlers/incident_notifier_handler.py`)のLINE
通知経路は、GitHub Issue作成の成否に一切影響されない。

LINEのclaim/dedup判定(#502/#503。`incident_state_tracker.try_claim()`)の結果
(CLAIMED_*/SUPPRESSED_*のいずれ)に関わらず、fingerprintごとに毎回呼び出してよい
(`github_issue_create_status`が既にCREATEDで対象IssueがOPENならコメント追記、
CLOSEDなら新規Issue、いずれでもなければ新規作成を試みる。実行済みの処理は
tracker側の状態で自然にスキップされる)。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from jstock_advisor.config.models import IncidentNotificationConfig
from jstock_advisor.domain.notification.incident_github_issue_message import (
    IncidentIssueNotice,
    build_incident_comment_body,
    build_incident_issue_body,
    build_incident_issue_title,
    comment_marker,
    issue_marker,
)
from jstock_advisor.infrastructure.aws import incident_state_tracker as tracker
from jstock_advisor.infrastructure.github.client import (
    GithubApiError,
    GithubConfigurationError,
    GithubIssueClient,
    load_credentials_from_secrets_manager,
)

logger = logging.getLogger(__name__)

# 既存の週次改善レビュー機能(rule-improvement/auto-generated)とラベル・重複判定・
# close運用を混在させないための専用search label(#132付録の論点)。
_SEARCH_LABEL = "production-incident"


def process_incident_issue(
    notice: IncidentIssueNotice,
    config: IncidentNotificationConfig,
    now: dt.datetime,
    *,
    repo_owner: str | None,
    repo_name: str | None,
    github_secret_arn: str | None,
) -> None:
    """呼び出し元のLINE通知経路を一切失敗させない(★最重要要件)。例外はここで
    握りつぶし、ログにのみ残す。
    """
    try:
        _process(notice, config, now, repo_owner, repo_name, github_secret_arn)
    except Exception:
        logger.exception(
            "incident_github_issue_service failed unexpectedly fingerprint=%s",
            notice.fingerprint,
        )


def _process(
    notice: IncidentIssueNotice,
    config: IncidentNotificationConfig,
    now: dt.datetime,
    repo_owner: str | None,
    repo_name: str | None,
    github_secret_arn: str | None,
) -> None:
    if not config.issue_creation_enabled:
        return  # 正常なスキップ(既定false)。運用エラー通知は送らない。

    if not github_secret_arn or not repo_owner or not repo_name:
        # リポジトリ・secret未設定のままGitHub APIを呼ばない(infra配線前の既定状態)。
        tracker.mark_github_issue_configuration_error(notice.fingerprint)
        return

    try:
        credentials = load_credentials_from_secrets_manager(github_secret_arn)
    except GithubConfigurationError:
        tracker.mark_github_issue_configuration_error(notice.fingerprint)
        return

    client = GithubIssueClient(repo_owner, repo_name, credentials)
    try:
        _process_with_client(client, notice, config, now)
    except GithubConfigurationError:
        # private_keyのPEM形式不正等、Client実行時に判明する設定不備。
        tracker.mark_github_issue_configuration_error(notice.fingerprint)
    except GithubApiError:
        tracker.mark_github_issue_creation_failed(notice.fingerprint)


def _process_with_client(
    client: GithubIssueClient,
    notice: IncidentIssueNotice,
    config: IncidentNotificationConfig,
    now: dt.datetime,
) -> None:
    state = tracker.get_incident_state(notice.fingerprint)
    existing_issue_number = state.get("github_issue_number") if state else None

    if existing_issue_number is not None:
        issue = client.get_issue(existing_issue_number, now)
        if issue.state == "closed":
            # #508決定事項(U-5): reopenしない。旧番号参照付きの新規Issueを作成する。
            _create_new_issue(
                client, notice, config, now, previous_issue_number=existing_issue_number
            )
            return
        _post_comment(client, notice, existing_issue_number, config, now, state or {})
        return

    _create_new_issue(client, notice, config, now, previous_issue_number=None)


def _create_new_issue(
    client: GithubIssueClient,
    notice: IncidentIssueNotice,
    config: IncidentNotificationConfig,
    now: dt.datetime,
    *,
    previous_issue_number: int | None,
) -> None:
    claimed = tracker.try_claim_new_github_issue_creation(
        notice.fingerprint, now, config.github_issue_claim_timeout_minutes
    )
    if not claimed:
        _reconcile_stale_issue_creation(client, notice, config, now, previous_issue_number)
        return

    title = build_incident_issue_title(notice)
    body = build_incident_issue_body(notice, previous_issue_number=previous_issue_number)
    issue = client.create_issue(title, body, config.issue_labels, now)
    tracker.mark_github_issue_created(
        notice.fingerprint, issue.number, previous_issue_number=previous_issue_number
    )


def _reconcile_stale_issue_creation(
    client: GithubIssueClient,
    notice: IncidentIssueNotice,
    config: IncidentNotificationConfig,
    now: dt.datetime,
    previous_issue_number: int | None,
) -> None:
    state = tracker.get_incident_state(notice.fingerprint)
    if (
        state is None
        or state.get("github_issue_create_status")
        != tracker.IncidentGithubIssueStatus.CREATING.value
    ):
        return  # 他実行が既に別状態(CREATED/FAILED等)へ進めた。何もしない。

    expires_raw = state.get("github_issue_claim_expires_at")
    claimed_raw = state.get("github_issue_claimed_at")
    if not expires_raw or not claimed_raw:
        return  # 他実行が処理中

    expires_at = dt.datetime.fromisoformat(expires_raw)
    if now < expires_at:
        return  # 他実行が処理中(未失効)

    # stale: 先にGitHub側の実在確認(二重Issue作成を防ぐ)。
    marker = issue_marker(notice.fingerprint)
    found = client.search_open_issue_by_marker(marker, _SEARCH_LABEL, now)
    if found is not None:
        tracker.mark_github_issue_created(
            notice.fingerprint, found.number, previous_issue_number=previous_issue_number
        )
        return

    if not tracker.try_reclaim_stale_github_issue_creation(
        notice.fingerprint, claimed_raw, now, config.github_issue_claim_timeout_minutes
    ):
        return  # 他実行に先を越された

    title = build_incident_issue_title(notice)
    body = build_incident_issue_body(notice, previous_issue_number=previous_issue_number)
    issue = client.create_issue(title, body, config.issue_labels, now)
    tracker.mark_github_issue_created(
        notice.fingerprint, issue.number, previous_issue_number=previous_issue_number
    )


def _post_comment(
    client: GithubIssueClient,
    notice: IncidentIssueNotice,
    issue_number: int,
    config: IncidentNotificationConfig,
    now: dt.datetime,
    state: dict[str, Any],
) -> None:
    if state.get("last_commented_occurrence_count") == notice.occurrence_count:
        return  # 今回のoccurrenceは既に完了

    if state.get("comment_claim_occurrence_count") == notice.occurrence_count:
        _reconcile_stale_comment(client, notice, issue_number, config, now, state)
        return

    claimed = tracker.try_claim_new_github_comment(
        notice.fingerprint, notice.occurrence_count, now, config.github_issue_claim_timeout_minutes
    )
    if not claimed:
        return  # 他実行が処理中、または既に完了

    body = build_incident_comment_body(notice)
    client.create_comment(issue_number, body, now)
    tracker.mark_github_comment_posted(notice.fingerprint, notice.occurrence_count)


def _reconcile_stale_comment(
    client: GithubIssueClient,
    notice: IncidentIssueNotice,
    issue_number: int,
    config: IncidentNotificationConfig,
    now: dt.datetime,
    state: dict[str, Any],
) -> None:
    expires_raw = state.get("comment_claim_expires_at")
    if not expires_raw:
        return
    expires_at = dt.datetime.fromisoformat(expires_raw)
    if now < expires_at:
        return  # 他実行が処理中(未失効)

    marker = comment_marker(notice.fingerprint, notice.occurrence_count)
    if client.find_comment_by_marker(issue_number, marker, now):
        tracker.mark_github_comment_posted(notice.fingerprint, notice.occurrence_count)
        return

    if not tracker.try_reclaim_stale_github_comment(
        notice.fingerprint,
        notice.occurrence_count,
        expires_raw,
        now,
        config.github_issue_claim_timeout_minutes,
    ):
        return  # 他実行に先を越された

    body = build_incident_comment_body(notice)
    client.create_comment(issue_number, body, now)
    tracker.mark_github_comment_posted(notice.fingerprint, notice.occurrence_count)
