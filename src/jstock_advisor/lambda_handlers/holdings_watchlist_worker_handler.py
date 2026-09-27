"""保有銘柄SQS worker Lambda(Issue #533。#319 Phase 2)。

`HOLDINGS_WATCHLIST_SQS_DISPATCH_ENABLED=true`時、`holdings_watchlist_handler.py`
が`HoldingsWatchlistQueue`(infra/template.yaml。#532で追加済み)へ送信した
holding単位のメッセージを受け取り、既存の非同期再帰呼び出し経路
(`holdings_watchlist_handler.handler()`のtask=="holding"分岐)と等価な処理を行う。

worker抽出方式は`buy_candidate_worker_handler.py`と同じくb-1(HUMAN_DECISION。
#533 issuecomment)を採用した。

`BatchSize`は`HoldingsWatchlistSqsBatchSize`(既定1。infra/template.yaml)のため、
1回の呼び出しにつき`event["Records"]`は通常1件だが、複数件が来た場合も
順に処理する(watchlist_worker_handler.pyと同型)。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.owner import log_ref
from jstock_advisor.infrastructure.line.client import build_line_client_for_run
from jstock_advisor.infrastructure.local_repository.notification_claim_repository import (
    NotificationClaimRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers._execution_mode import resolve_execution_context
from jstock_advisor.lambda_handlers.holdings_watchlist_handler import _process_single_holding
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.provider_factory import build_real_provider_bundle
from jstock_advisor.services.rule_version_service import RuleVersionService

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    processed = [_process_one(json.loads(record["body"])) for record in event.get("Records", [])]
    return {"processed": len(processed)}


def _process_one(body: dict[str, Any]) -> dict[str, Any]:
    execution_context = resolve_execution_context(body)
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    providers = build_real_provider_bundle(now, config)
    recommendation_repo = RecommendationRepository.for_execution_context(execution_context)
    # holdings_watchlist_handler.handler()のtask=="holding"分岐と同じ規約
    # (Issue #211 / #70 F-B3): 既定False(fail-close)。
    trade_detection_confirmed = body.get("trade_detection_confirmed", False)
    notification_service = LineNotificationService(
        line_client=build_line_client_for_run(dry_run=execution_context.is_dry_run),
        notification_log_repository=NotificationLogRepository(),
        notification_claim_repository=NotificationClaimRepository(),
        recommendation_repository=recommendation_repo,
        config=config,
        execution_context=execution_context,
        trade_detection_confirmed=trade_detection_confirmed,
    )
    rule_version_service = RuleVersionService()
    result = _process_single_holding(
        body["holding_id"],
        body.get("batch_id"),
        now,
        providers,
        config,
        recommendation_repo,
        notification_service,
        rule_version_service,
        execution_context,
    )
    logger.info(
        "holdings_watchlist_worker_handler single holding done holding_ref=%s "
        "recommended=%s notified=%s found=%s failed=%s "
        "evaluation_status=%s notification_status=%s",
        log_ref(body["holding_id"]),
        result.get("recommended"),
        result.get("notified"),
        result.get("found"),
        result.get("failed"),
        result.get("evaluation_status"),
        result.get("notification_status"),
    )
    return result
