"""週次改善レビューLambda(schedule.yaml weekly_review、月曜19:00)。

振り返り機能改修により、従来の全期間合算レポート自動送信(ReviewReportService)は
廃止した(`jstock review report`CLIは手動閲覧用に維持)。代わりに、前週に確定した
7暦日評価を分析し、十分な証拠がある改善候補のみGitHub Issueを自動起票、Issue作成
成功時のみLINE通知する(WeeklyImprovementReviewService、要求仕様1〜21節)。
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.services.weekly_improvement_review_service import (
    WeeklyImprovementReviewService,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _split_github_repository() -> tuple[str | None, str | None]:
    """GITHUB_REPOSITORY環境変数("owner/repo"形式)からowner/repoを取り出す。
    未設定・形式不正の場合は両方Noneを返す(GithubConfigurationErrorとして扱われる)。
    """
    value = os.environ.get("GITHUB_REPOSITORY")
    if not value or "/" not in value:
        return None, None
    owner, _, repo = value.partition("/")
    return owner, repo


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    owner, repo = _split_github_repository()
    service = WeeklyImprovementReviewService(
        config=config,
        line_client=build_line_client_from_env(),
        github_repo_owner=owner,
        github_repo_name=repo,
        github_secret_arn=os.environ.get("GITHUB_APP_SECRET_ARN"),
    )

    outcome = service.run(now)
    logger.info(
        "weekly_review_handler done: review_week=%s joined=%d/%d candidates=%d "
        "issue_eligible=%d github_statuses=%s notified=%d",
        outcome.review_week,
        outcome.joined_count,
        outcome.total_evaluation_results,
        outcome.candidates_detected,
        outcome.issue_eligible_candidates,
        outcome.github_statuses,
        outcome.notified_new_issue_count,
    )
    return {
        "review_week": outcome.review_week,
        "total_evaluation_results": outcome.total_evaluation_results,
        "joined_count": outcome.joined_count,
        "candidates_detected": outcome.candidates_detected,
        "issue_eligible_candidates": outcome.issue_eligible_candidates,
        "notified_new_issue_count": outcome.notified_new_issue_count,
    }
