"""incidentのGitHub Issue自動起票・コメント追記を行うサービス(Issue #508。
#132 X-9)。

`infrastructure.aws.incident_state_tracker`による原子的な状態遷移を主たる冪等制御と
し、GitHub側の実在確認(reconciliation)はstale claim検出時の復旧・二重チェック専用に
使う(通常時の主用途にはしない。`services/github_issue_service.py`〔週次改善レビュー〕
と同じ設計)。

★最重要要件(#508 Phase A設計): `process_incident_issue()`はいかなる例外も外へ
伝播させない。呼び出し元(`lambda_handlers/incident_notifier_handler.py`)のLINE
通知経路は、GitHub Issue作成の成否に一切影響されない。

## 起票の抑止(Issue #926。#876 の大量起票への構造的な対策)

新規Issueの作成には、層1(上限の未決定・同じoccurrence・前回作成からの最小間隔の事前確認)、層2
(fingerprintあたり・全体の上限。JSTの日付の窓)、層3(kill switch。停止中は新規作成もコメントも
GitHubを一切呼ばない)を挟む。純粋な判定は`domain/notification/incident_issue_guard.py`、上限値は
`config/incident_issue_limits.yaml`(出荷値は全項目null = 未決定 = 新規Issueを作らない)、
原子的な消費・
停止・通知の確保は`infrastructure/aws/incident_state_tracker.py`。制御項目の読取に失敗したら
GitHub書込をしない(fail-closed)。LINEの経路には何も要求せず、自動停止の通知が要るかだけを返す。

LINEのclaim/dedup判定(#502/#503。`incident_state_tracker.try_claim()`)の結果
(CLAIMED_*/SUPPRESSED_*のいずれ)に関わらず、fingerprintごとに毎回呼び出してよい
(`github_issue_create_status`が既にCREATEDで対象IssueがOPENならコメント追記、
CLOSEDなら新規Issue、いずれでもなければ新規作成を試みる。実行済みの処理は
tracker側の状態で自然にスキップされる)。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, cast

from jstock_advisor.config.models import IncidentNotificationConfig
from jstock_advisor.domain.notification import incident_issue_guard as guard
from jstock_advisor.domain.notification.incident_github_issue_message import (
    IncidentIssueNotice,
    build_incident_comment_body,
    build_incident_issue_body,
    build_incident_issue_title,
    comment_marker,
    issue_marker,
)
from jstock_advisor.domain.notification.incident_issue_limits import (
    IncidentIssueLimits,
    load_incident_issue_limits,
)
from jstock_advisor.infrastructure.aws import incident_state_tracker as tracker
from jstock_advisor.infrastructure.github.client import (
    GithubApiError,
    GithubConfigurationError,
    GithubIssueClient,
    load_credentials_from_secrets_manager,
)

logger = logging.getLogger(__name__)
# 抑止(#926)の INFO は、識別子(fingerprint のハッシュ)と理由の列挙値だけを出す。
logger.setLevel(logging.INFO)

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
    limits: IncidentIssueLimits | None = None,
) -> guard.IssueGuardOutcome:
    """呼び出し元のLINE通知経路を一切失敗させない(★最重要要件)。例外はここで
    握りつぶし、ログにのみ残す。

    `limits`を渡さない場合は、専用の設定(`config/incident_issue_limits.yaml`)を読む。戻り値は、
    自動停止のLINE通知が要るか(`IssueGuardOutcome`)だけで、LINEの経路には他に何も要求しない。
    """
    try:
        resolved = limits if limits is not None else load_incident_issue_limits()
        return _process(notice, config, now, repo_owner, repo_name, github_secret_arn, resolved)
    except Exception:
        logger.exception(
            "incident_github_issue_service failed unexpectedly fingerprint=%s",
            notice.fingerprint,
        )
        return guard.IssueGuardOutcome()


def _auto_stop_notification_due(control: dict[str, Any] | None) -> bool:
    """自動停止にされていて、LINEへまだ通知していない(通知の再試行を含む)。手動の停止は通知しない。"""
    return (
        control is not None
        and control.get("kill_switch_reason") == guard.KILL_SWITCH_REASON_AUTO
        and control.get("kill_switch_notified_at") is None
    )


def _process(
    notice: IncidentIssueNotice,
    config: IncidentNotificationConfig,
    now: dt.datetime,
    repo_owner: str | None,
    repo_name: str | None,
    github_secret_arn: str | None,
    limits: IncidentIssueLimits,
) -> guard.IssueGuardOutcome:
    if not config.issue_creation_enabled:
        return guard.IssueGuardOutcome()  # 正常なスキップ(既定false)。運用エラー通知は送らない。

    # 層3: kill switch。停止中は新規作成もコメントもGitHubを一切呼ばない。読取に失敗したら
    # 書込をしない(fail-closed。LINEの経路は不変)。
    try:
        control = tracker.read_github_issue_control()
    except Exception:
        logger.warning(
            "incident_github_issue_service kill switch unreadable; skipping (fail-closed)"
        )
        return guard.IssueGuardOutcome()
    if tracker.is_kill_switch_on(control):
        return guard.IssueGuardOutcome(
            auto_stop_notification_due=_auto_stop_notification_due(control)
        )

    if not github_secret_arn or not repo_owner or not repo_name:
        # リポジトリ・secret未設定のままGitHub APIを呼ばない(infra配線前の既定状態)。
        tracker.mark_github_issue_configuration_error(notice.fingerprint)
        return guard.IssueGuardOutcome()

    try:
        credentials = load_credentials_from_secrets_manager(github_secret_arn)
    except GithubConfigurationError:
        tracker.mark_github_issue_configuration_error(notice.fingerprint)
        return guard.IssueGuardOutcome()

    client = GithubIssueClient(repo_owner, repo_name, credentials)
    try:
        return _process_with_client(client, notice, config, now, limits)
    except GithubConfigurationError:
        # private_keyのPEM形式不正等、Client実行時に判明する設定不備。
        tracker.mark_github_issue_configuration_error(notice.fingerprint)
    except GithubApiError:
        tracker.mark_github_issue_creation_failed(notice.fingerprint)
    return guard.IssueGuardOutcome()


def _process_with_client(
    client: GithubIssueClient,
    notice: IncidentIssueNotice,
    config: IncidentNotificationConfig,
    now: dt.datetime,
    limits: IncidentIssueLimits,
) -> guard.IssueGuardOutcome:
    state = tracker.get_incident_state(notice.fingerprint)
    existing_issue_number = state.get("github_issue_number") if state else None

    if existing_issue_number is not None:
        issue = client.get_issue(existing_issue_number, now)
        if issue.state == "closed":
            # #508決定事項(U-5): reopenしない。旧番号参照付きの新規Issueを作成する。
            # (#926: 新規作成は、occurrence・最小間隔・上限・kill switchの抑止を通ったときだけ)
            return _create_new_issue(
                client,
                notice,
                config,
                now,
                limits,
                state,
                previous_issue_number=existing_issue_number,
            )
        _post_comment(client, notice, existing_issue_number, config, now, state or {})
        return guard.IssueGuardOutcome()

    return _create_new_issue(client, notice, config, now, limits, state, previous_issue_number=None)


def _create_new_issue(
    client: GithubIssueClient,
    notice: IncidentIssueNotice,
    config: IncidentNotificationConfig,
    now: dt.datetime,
    limits: IncidentIssueLimits,
    state: dict[str, Any] | None,
    *,
    previous_issue_number: int | None,
) -> guard.IssueGuardOutcome:
    fingerprint = notice.fingerprint

    # 層1: 事前確認(読取だけ)。作らない理由があれば、claimせずに数えて終える。
    denied = guard.precheck_new_issue(limits, state, notice.occurrence_count, now)
    if denied is not None:
        tracker.record_github_issue_suppressed(fingerprint, denied, now)
        logger.info(
            "incident_github_issue_service new issue suppressed reason=%s fingerprint=%s",
            denied.value,
            fingerprint,
        )
        return guard.IssueGuardOutcome()

    claimed = tracker.try_claim_new_github_issue_creation(
        fingerprint, now, config.github_issue_claim_timeout_minutes
    )
    if not claimed:
        return _reconcile_stale_issue_creation(
            client, notice, config, now, limits, previous_issue_number
        )

    return _consume_and_create(
        client, notice, config, now, limits, previous_issue_number, budget_consumed=False
    )


def _consume_and_create(
    client: GithubIssueClient,
    notice: IncidentIssueNotice,
    config: IncidentNotificationConfig,
    now: dt.datetime,
    limits: IncidentIssueLimits,
    previous_issue_number: int | None,
    *,
    budget_consumed: bool,
) -> guard.IssueGuardOutcome:
    """claim を持つ起動が、上限の予算を消費して Issue を作る(層 2・3)。

    `budget_consumed` は、stale 復旧で、元の起動が**今の窓の**予算を消費した印を行に見つけたときだけ
    True にする。claim は予算の消費より先に取られるため、claim の直後(消費の前)に落ちた起動の後の
    復旧は印が無く、消費を通す(上限を超えて起票しない。Issue #926)。
    """
    fingerprint = notice.fingerprint
    window = guard.window_id(now)

    if not budget_consumed:
        # 事前確認を通った = 上限は決定済み(None ではない)。
        per_fingerprint = cast(int, limits.new_issue_limit_per_fingerprint)
        global_limit = cast(int, limits.new_issue_limit_global)
        threshold = cast(int, limits.auto_stop_denied_threshold)
        if not tracker.try_consume_fingerprint_budget(fingerprint, window, per_fingerprint):
            tracker.mark_github_issue_suppressed(
                fingerprint, guard.DenyReason.FINGERPRINT_LIMIT, now
            )
            return guard.IssueGuardOutcome()
        if not tracker.try_consume_global_budget(window, global_limit):
            tracker.refund_fingerprint_budget(fingerprint, window)
            tracker.mark_github_issue_suppressed(fingerprint, guard.DenyReason.GLOBAL_LIMIT, now)
            # 全体の上限を超えた試行の数だけが自動停止の入力(fingerprint単位の抑止は入力にしない)
            if tracker.record_global_denied(window) >= threshold and tracker.try_trip_kill_switch(
                now, guard.KILL_SWITCH_REASON_AUTO
            ):
                return guard.IssueGuardOutcome(auto_stop_notification_due=True)
            return guard.IssueGuardOutcome()

    try:
        if not budget_consumed:
            # 消費したことを行に残す(stale 復旧が二重に消費しないため)
            tracker.mark_github_issue_budget_consumed(fingerprint, window)
        title = build_incident_issue_title(notice)
        body = build_incident_issue_body(notice, previous_issue_number=previous_issue_number)
        issue = client.create_issue(title, body, config.issue_labels, now)
    except Exception:
        # 作成に失敗したら予算を返す(GitHubの障害中に予算だけが減らない)。
        # 呼び出し側が失敗として記録する。
        _release_budgets(fingerprint, window)
        raise
    tracker.mark_github_issue_created(
        fingerprint,
        issue.number,
        previous_issue_number=previous_issue_number,
        created_at=now,
        occurrence_count=notice.occurrence_count,
    )
    return guard.IssueGuardOutcome()


def _release_budgets(fingerprint: str, window: str) -> None:
    """消費した予算を返す。失敗は握る(元の失敗を隠さない。返せなければ多めに消費された側=安全側)。

    先に『消費済み』の印を消す。印が残ったまま返すと、後の stale 復旧が『消費済み』とみなして
    消費せずに起票する(返した分だけ上限を超える)。印を消せなかったときは返さない。
    """
    try:
        tracker.clear_github_issue_budget_window(fingerprint)
    except Exception:
        logger.warning(
            "incident_github_issue_service budget mark clear failed (budgets kept consumed)"
        )
        return
    try:
        tracker.refund_fingerprint_budget(fingerprint, window)
    except Exception:
        logger.warning(
            "incident_github_issue_service fingerprint budget refund failed (kept consumed)"
        )
    try:
        tracker.refund_global_budget(window)
    except Exception:
        logger.warning("incident_github_issue_service global budget refund failed (kept consumed)")


def _reconcile_stale_issue_creation(
    client: GithubIssueClient,
    notice: IncidentIssueNotice,
    config: IncidentNotificationConfig,
    now: dt.datetime,
    limits: IncidentIssueLimits,
    previous_issue_number: int | None,
) -> guard.IssueGuardOutcome:
    state = tracker.get_incident_state(notice.fingerprint)
    if (
        state is None
        or state.get("github_issue_create_status")
        != tracker.IncidentGithubIssueStatus.CREATING.value
    ):
        return (
            guard.IssueGuardOutcome()
        )  # 他実行が既に別状態(CREATED/FAILED等)へ進めた。何もしない。

    expires_raw = state.get("github_issue_claim_expires_at")
    claimed_raw = state.get("github_issue_claimed_at")
    if not expires_raw or not claimed_raw:
        return guard.IssueGuardOutcome()  # 他実行が処理中

    expires_at = dt.datetime.fromisoformat(expires_raw)
    if now < expires_at:
        return guard.IssueGuardOutcome()  # 他実行が処理中(未失効)

    # stale: 先にGitHub側の実在確認(二重Issue作成を防ぐ)。
    marker = issue_marker(notice.fingerprint)
    found = client.search_open_issue_by_marker(marker, _SEARCH_LABEL, now)
    if found is not None:
        tracker.mark_github_issue_created(
            notice.fingerprint, found.number, previous_issue_number=previous_issue_number
        )
        return guard.IssueGuardOutcome()

    if not tracker.try_reclaim_stale_github_issue_creation(
        notice.fingerprint, claimed_raw, now, config.github_issue_claim_timeout_minutes
    ):
        return guard.IssueGuardOutcome()  # 他実行に先を越された

    # 元の起動が今の窓の予算を消費していれば再消費しない。印が無い(消費の前に落ちた)・窓が違うときは
    # 消費を通す。事前確認(最小間隔・同じ occurrence)は、この signal について呼び出し元で通過済み。
    consumed = state.get("github_issue_budget_window") == guard.window_id(now)
    return _consume_and_create(
        client, notice, config, now, limits, previous_issue_number, budget_consumed=consumed
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
