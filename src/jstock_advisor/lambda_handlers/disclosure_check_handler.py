"""適時開示チェックLambda(schedule.yaml disclosure_check、平日数回)。

CLIの`jstock analyze disclosure-check --source real --notify`と同じロジックを
EventBridge Scheduler経由で自動実行する薄いアダプタ。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.disclosure_check_service import DisclosureCheckService
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.provider_factory import build_real_provider_bundle

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    providers = build_real_provider_bundle(now, config)
    service = DisclosureCheckService(disclosure_provider=providers.disclosure, config=config)
    notification_service = LineNotificationService(
        line_client=build_line_client_from_env(),
        notification_log_repository=NotificationLogRepository(),
        recommendation_repository=RecommendationRepository(),
        config=config,
    )

    alerts = service.check_holdings(now)
    notified = 0
    for alert in alerts:
        if notification_service.notify_disclosure_risk(
            stock_code=alert.stock_code,
            disclosure_title=alert.disclosure.title,
            disclosure_summary=alert.disclosure.summary,
            matched_keywords=alert.matched_keywords,
            published_at=alert.disclosure.published_at,
            now=now,
        ):
            notified += 1

    logger.info("disclosure_check_handler done: alerts=%d notified=%d", len(alerts), notified)
    return {"alerts": len(alerts), "notified": notified}
