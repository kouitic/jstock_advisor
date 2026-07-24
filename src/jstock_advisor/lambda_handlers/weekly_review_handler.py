"""週次レビューLambda(schedule.yaml weekly_review、土曜09:00)。

CLIの`jstock review report --notify`と同じロジック(全ホライズン合算)を
EventBridge Scheduler経由で自動実行する薄いアダプタ。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.services.review_report_service import ReviewReportService

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    service = ReviewReportService(line_client=build_line_client_from_env())
    text = service.send_report(now=now)
    logger.info("weekly_review_handler done")
    return {"report_length": len(text)}
