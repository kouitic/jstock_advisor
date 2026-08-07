"""週次改善候補からGitHub Issueを自動起票・コメント追記するサービス(振り返り機能改修)。

DynamoDB(infrastructure.aws.improvement_task_tracker)による原子的な状態遷移を
主たる冪等制御とし、GitHub側の実在確認(reconciliation)はstale claim検出時の
復旧・二重チェック専用に使う(通常時の主用途にはしない)。GitHub呼び出し全体は
本モジュール内で例外を捕捉し、週次処理全体を失敗させない(呼び出し側は戻り値の
ImprovementTaskStatusだけを見ればよい)。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from jstock_advisor.config.models import ReviewImprovementConfig
from jstock_advisor.domain.entities.enums import ImprovementTaskStatus
from jstock_advisor.domain.entities.improvement import (
    PROBLEM_CATEGORY_EVALUATION_CRITERIA_UNDEFINED,
    ImprovementCandidate,
)
from jstock_advisor.infrastructure.aws import improvement_task_tracker as tracker
from jstock_advisor.infrastructure.github.client import (
    GithubApiError,
    GithubConfigurationError,
    GithubIssueClient,
    load_credentials_from_secrets_manager,
)

_LABEL_FOR_SEARCH = "auto-generated"


def _issue_marker(candidate_key: str) -> str:
    return f"<!-- improvement_candidate_key: {candidate_key} -->"


def _comment_marker(candidate_key: str, review_week: str) -> str:
    return (
        f"<!-- improvement_candidate_key: {candidate_key} -->\n"
        f"<!-- review_week: {review_week} -->"
    )


def _fmt_pct(value: float | None) -> str:
    return "評価対象外" if value is None else f"{value:.1f}%"


def _fmt_points(value: float | None) -> str:
    return "-" if value is None else f"{value:+.1f}pt"


def _build_issue_title(candidate: ImprovementCandidate) -> str:
    segment = f" × {candidate.segment_key}" if candidate.segment_key else ""
    type_label = f"{candidate.recommendation_type.value}{segment}"
    if candidate.problem_category == PROBLEM_CATEGORY_EVALUATION_CRITERIA_UNDEFINED:
        return f"[Rule Improvement] {type_label}判定の評価定義が未整備です"
    return f"[Rule Improvement] {type_label}判定の成績悪化"


def _build_issue_body(candidate: ImprovementCandidate, previous_issue_number: int | None) -> str:
    lines: list[str] = ["## 概要"]
    if candidate.problem_category == PROBLEM_CATEGORY_EVALUATION_CRITERIA_UNDEFINED:
        lines.append(
            f"{candidate.recommendation_type.value}判定は、推奨後7暦日時点の自動評価で"
            "「対象外(INCONCLUSIVE)」となる件数が継続しています。現在の評価ロジック"
            "(domain/evaluation_rules.py)はENTRY/EXIT系種別のみ成功/失敗を判定でき、"
            "この種別に対する評価定義がまだ存在しません。"
        )
    else:
        lines.append(
            f"{candidate.recommendation_type.value}判定(rule_version="
            f"{candidate.rule_version})に成績悪化を検出しました。"
        )
    if previous_issue_number is not None:
        lines.append(f"\nPrevious issue: #{previous_issue_number}")

    lines += [
        "",
        "## 対象期間",
        f"{candidate.evaluation_period_start.isoformat()} ～ "
        f"{candidate.evaluation_period_end.isoformat()}(review_week={candidate.review_week})",
        "",
        "## 成績",
        f"RecommendationType: {candidate.recommendation_type.value}",
        f"rule_version: {candidate.rule_version}",
        f"評価件数(sample_count): {candidate.sample_count}件",
        f"うち評価対象件数(conclusive_count): {candidate.conclusive_count}件",
        f"成功率: {_fmt_pct(candidate.success_rate_pct)}",
        f"平均リターン: {_fmt_pct(candidate.average_return_pct)}",
        f"平均超過リターン: {_fmt_pct(candidate.average_excess_return_pct)}",
        f"前週比成功率変化: {_fmt_points(candidate.success_rate_change_points)}"
        f"(連続悪化{candidate.consecutive_bad_weeks}週)",
        "",
        "## 検出した問題",
    ]
    lines += [f"- {code}" for code in candidate.reason_codes] or ["- (該当なし)"]
    if candidate.evidence:
        lines += ["", "## 根拠"]
        lines += [f"- {e}" for e in candidate.evidence]
    lines += [
        "",
        "## 改善仮説",
        "本Issueの成績データをもとに、閾値・条件の見直しが必要かご確認ください"
        "(自動生成された仮説であり、バックテストしていない期待改善値は含みません)。",
        "",
        "## 推奨アクション",
        f"{candidate.recommended_action.value}",
        "",
        "## 受入条件",
        "- 現行ルールとの比較結果を提示すること",
        "- 評価母数(sample_count/conclusive_count)を明記すること",
        "- 他RecommendationTypeへのデグレが無いことを確認すること",
        "- pytest / Ruff / mypy が全てPASSすること",
        "",
        _issue_marker(candidate.candidate_key),
    ]
    return "\n".join(lines)


def _build_comment_body(candidate: ImprovementCandidate, review_week: str) -> str:
    lines = [
        f"### {review_week}週次レビュー",
        f"対象件数: {candidate.sample_count}件(評価対象{candidate.conclusive_count}件)",
        f"成功率: {_fmt_pct(candidate.success_rate_pct)}",
        f"平均超過リターン: {_fmt_pct(candidate.average_excess_return_pct)}",
        f"前週比: {_fmt_points(candidate.success_rate_change_points)}",
        "",
        _comment_marker(candidate.candidate_key, review_week),
    ]
    return "\n".join(lines)


def process_candidate(
    candidate: ImprovementCandidate,
    review_week: str,
    now: dt.datetime,
    config: ReviewImprovementConfig,
    repo_owner: str,
    repo_name: str,
    github_secret_arn: str | None,
) -> ImprovementTaskStatus:
    """1件のImprovementCandidateについて、GitHub Issue作成/コメント追記の
    要否を判定し実行する(要求仕様11〜16節、決定事項12〜15)。呼び出し前提として、
    候補自体は既に「Issue化条件」を満たしていること(週次レビューサービア側で判定)。
    """
    tracker.ensure_task_exists(
        candidate.candidate_key,
        candidate.recommendation_type,
        candidate.rule_version,
        candidate.segment_key,
        candidate.priority,
        now,
    )

    if not config.issue_creation_enabled:
        tracker.mark_skipped_not_configured(candidate.candidate_key, now)
        return ImprovementTaskStatus.SKIPPED_NOT_CONFIGURED

    if not github_secret_arn or not repo_owner or not repo_name:
        # リポジトリ未設定(owner/repoが空)のままGitHub APIを呼ばない。
        tracker.mark_configuration_error(candidate.candidate_key, now)
        return ImprovementTaskStatus.CONFIGURATION_ERROR

    try:
        credentials = load_credentials_from_secrets_manager(github_secret_arn)
    except GithubConfigurationError:
        tracker.mark_configuration_error(candidate.candidate_key, now)
        return ImprovementTaskStatus.CONFIGURATION_ERROR

    client = GithubIssueClient(repo_owner, repo_name, credentials)

    try:
        return _process_with_client(client, candidate, review_week, now, config)
    except GithubConfigurationError:
        # private_keyのPEM形式不正等、GitHub Client実行時に判明する設定不備
        # (Secret読込時点では検出できない)もCONFIGURATION_ERRORへ分類する。
        tracker.mark_configuration_error(candidate.candidate_key, now)
        return ImprovementTaskStatus.CONFIGURATION_ERROR
    except GithubApiError:
        tracker.mark_issue_creation_failed(candidate.candidate_key, now)
        return ImprovementTaskStatus.ISSUE_CREATION_FAILED


def _process_with_client(
    client: GithubIssueClient,
    candidate: ImprovementCandidate,
    review_week: str,
    now: dt.datetime,
    config: ReviewImprovementConfig,
) -> ImprovementTaskStatus:
    task = tracker.get_improvement_task(candidate.candidate_key)
    existing_issue_number = task.get("github_issue_number") if task else None

    if existing_issue_number is not None:
        issue = client.get_issue(existing_issue_number, now)
        if issue.state == "closed":
            return _create_new_issue(
                client, candidate, now, config, previous_issue_number=existing_issue_number
            )
        return _post_weekly_comment(
            client, candidate, existing_issue_number, review_week, now, config
        )

    if candidate.is_current_rule_version is not True:
        # 過去rule_version、または現在版が判定不能なCandidateは新規Issue化しない
        # (決定事項10・12)。Candidateとしての保存は既にweekly_improvement_review_
        # service側で完了している。
        return ImprovementTaskStatus.CANDIDATE

    return _create_new_issue(client, candidate, now, config, previous_issue_number=None)


def _create_new_issue(
    client: GithubIssueClient,
    candidate: ImprovementCandidate,
    now: dt.datetime,
    config: ReviewImprovementConfig,
    *,
    previous_issue_number: int | None,
) -> ImprovementTaskStatus:
    claimed = tracker.try_claim_new_issue_creation(
        candidate.candidate_key, now, config.github_issue_claim_timeout_minutes
    )
    if not claimed:
        return _reconcile_stale_issue_creation(
            client, candidate, now, config, previous_issue_number
        )

    title = _build_issue_title(candidate)
    body = _build_issue_body(candidate, previous_issue_number)
    issue = client.create_issue(title, body, config.issue_labels, now)
    tracker.mark_issue_created(
        candidate.candidate_key,
        issue.number,
        issue.html_url,
        now,
        previous_issue_number=previous_issue_number,
    )
    return ImprovementTaskStatus.ISSUE_CREATED


def _reconcile_stale_issue_creation(
    client: GithubIssueClient,
    candidate: ImprovementCandidate,
    now: dt.datetime,
    config: ReviewImprovementConfig,
    previous_issue_number: int | None,
) -> ImprovementTaskStatus:
    task = tracker.get_improvement_task(candidate.candidate_key)
    if task is None or task.get("status") != ImprovementTaskStatus.ISSUE_CREATING.value:
        return ImprovementTaskStatus.CANDIDATE

    expires_raw = task.get("issue_claim_expires_at")
    claimed_raw = task.get("issue_claimed_at")
    if not expires_raw or not claimed_raw:
        return ImprovementTaskStatus.ISSUE_CREATING
    expires_at = dt.datetime.fromisoformat(expires_raw)
    if now < expires_at:
        return ImprovementTaskStatus.ISSUE_CREATING  # 他実行が処理中(未失効)

    # stale: 先にGitHub側の実在確認(決定事項14: 期限切れでも即座に再claimしない)
    marker = _issue_marker(candidate.candidate_key)
    found = client.search_open_issue_by_marker(marker, _LABEL_FOR_SEARCH, now)
    if found is not None:
        tracker.mark_issue_created(
            candidate.candidate_key,
            found.number,
            found.html_url,
            now,
            previous_issue_number=previous_issue_number,
        )
        return ImprovementTaskStatus.ISSUE_CREATED

    if not tracker.try_reclaim_stale_issue_creation(
        candidate.candidate_key, claimed_raw, now, config.github_issue_claim_timeout_minutes
    ):
        return ImprovementTaskStatus.ISSUE_CREATING  # 他実行に先を越された

    title = _build_issue_title(candidate)
    body = _build_issue_body(candidate, previous_issue_number)
    issue = client.create_issue(title, body, config.issue_labels, now)
    tracker.mark_issue_created(
        candidate.candidate_key,
        issue.number,
        issue.html_url,
        now,
        previous_issue_number=previous_issue_number,
    )
    return ImprovementTaskStatus.ISSUE_CREATED


def _post_weekly_comment(
    client: GithubIssueClient,
    candidate: ImprovementCandidate,
    issue_number: int,
    review_week: str,
    now: dt.datetime,
    config: ReviewImprovementConfig,
) -> ImprovementTaskStatus:
    task = tracker.get_improvement_task(candidate.candidate_key)
    if task is not None and task.get("last_commented_review_week") == review_week:
        return ImprovementTaskStatus.ISSUE_CREATED  # 今週分は既に完了

    if task is not None and task.get("comment_claim_review_week") == review_week:
        return _reconcile_stale_comment(
            client, candidate, issue_number, review_week, now, config, task
        )

    claimed = tracker.try_claim_new_comment(
        candidate.candidate_key, review_week, now, config.github_issue_claim_timeout_minutes
    )
    if not claimed:
        return ImprovementTaskStatus.ISSUE_CREATED  # 他実行が処理中、または既に完了

    body = _build_comment_body(candidate, review_week)
    client.create_comment(issue_number, body, now)
    tracker.mark_comment_posted(candidate.candidate_key, review_week, now)
    return ImprovementTaskStatus.ISSUE_CREATED


def _reconcile_stale_comment(
    client: GithubIssueClient,
    candidate: ImprovementCandidate,
    issue_number: int,
    review_week: str,
    now: dt.datetime,
    config: ReviewImprovementConfig,
    task: dict[str, Any],
) -> ImprovementTaskStatus:
    expires_raw = task.get("comment_claim_expires_at")
    if not expires_raw:
        return ImprovementTaskStatus.ISSUE_CREATED
    expires_at = dt.datetime.fromisoformat(expires_raw)
    if now < expires_at:
        return ImprovementTaskStatus.ISSUE_CREATED  # 他実行が処理中(未失効)

    marker = _comment_marker(candidate.candidate_key, review_week)
    if client.find_comment_by_marker(issue_number, marker, now):
        tracker.mark_comment_posted(candidate.candidate_key, review_week, now)
        return ImprovementTaskStatus.ISSUE_CREATED

    if not tracker.try_reclaim_stale_comment(
        candidate.candidate_key,
        review_week,
        expires_raw,
        now,
        config.github_issue_claim_timeout_minutes,
    ):
        return ImprovementTaskStatus.ISSUE_CREATED  # 他実行に先を越された

    body = _build_comment_body(candidate, review_week)
    client.create_comment(issue_number, body, now)
    tracker.mark_comment_posted(candidate.candidate_key, review_week, now)
    return ImprovementTaskStatus.ISSUE_CREATED
