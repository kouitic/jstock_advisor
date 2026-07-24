"""月次レビューLambda(schedule.yaml monthly_review、第1土曜10:00)。

EventBridge Schedulerは「毎週土曜」のcronで起動する想定とし、当月第1土曜日
でなければ何もせず終了する(当初設計の方針通り、序数判定をLambda側で行う)。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.lambda_handlers._scheduling import is_first_saturday_of_month
from jstock_advisor.services.review_report_service import ReviewReportService

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    if not is_first_saturday_of_month(now.date()):
        logger.info("monthly_review_handler skipped: not first saturday")
        return {"skipped": True}

    service = ReviewReportService(line_client=build_line_client_from_env())
    text = service.send_report(now=now)
    logger.info("monthly_review_handler done")
    return {"skipped": False, "report_length": len(text)}
