"""月次レビューLambda(schedule.yaml monthly_review、第1土曜10:00)。

振り返り機能改修: 「ユーザーにアクションが必要な場合のみLINE通知する」という
方針(週次改善レビュー、weekly_review_handler.py)と矛盾するため、従来の
全期間合算レポートの自動LINE送信(ReviewReportService.send_report)は廃止した。
月次の戦略レビュー機能そのものの新規構築は今回のスコープ外(要求仕様3節)のため、
Lambda・スケジュール自体は残しつつ、内部記録(ログ)のみを行いLINEは送信しない。
手動で全期間合算レポートを見たい場合は`jstock review report`CLIを使うこと。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from jstock_advisor.lambda_handlers._scheduling import is_first_saturday_of_month

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    is_monthly_review_day = is_first_saturday_of_month(now.date())
    logger.info(
        "monthly_review_handler: LINE通知は行わない(振り返り機能改修)。"
        "is_monthly_review_day=%s",
        is_monthly_review_day,
    )
    return {"skipped": True, "is_monthly_review_day": is_monthly_review_day}
