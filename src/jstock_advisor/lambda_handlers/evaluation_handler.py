"""定点評価Lambda(schedule.yaml point_in_time_evaluation、平日18:00)。

CLIの`jstock evaluation run --source real`と同じロジックをEventBridge
Scheduler経由で自動実行する薄いアダプタ。通知は行わない(CLIと同様)。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.services.provider_factory import build_real_provider_bundle
from jstock_advisor.services.recommendation_evaluation_service import (
    RecommendationEvaluationService,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    calendar = BusinessCalendar.from_config(config.holiday_calendar)
    providers = build_real_provider_bundle(now, config)
    service = RecommendationEvaluationService(
        market_data_provider=providers.market_data, config=config, business_calendar=calendar
    )

    outcome = service.run_due_evaluations(now)
    logger.info(
        "evaluation_handler done: evaluated=%d skipped=%d",
        len(outcome.evaluated),
        len(outcome.skipped_due_to_data_error),
    )
    return {
        "evaluated": len(outcome.evaluated),
        "skipped_due_to_data_error": len(outcome.skipped_due_to_data_error),
    }
