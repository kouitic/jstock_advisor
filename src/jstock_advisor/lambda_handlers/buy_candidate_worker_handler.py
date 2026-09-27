"""買い候補SQS worker Lambda(Issue #533。#319 Phase 2)。

`BUY_CANDIDATE_SQS_DISPATCH_ENABLED=true`時、`buy_candidates_handler.py`が
`BuyCandidateQueue`(infra/template.yaml。#532で追加済み)へ送信した銘柄単位の
メッセージを受け取り、既存の非同期再帰呼び出し経路(`buy_candidates_handler.
handler()`のtask=="buy_candidate"分岐)と等価な処理を行う。

worker抽出方式はHUMAN_DECISION(#533 issuecomment)によりb-1(新規worker moduleが
既存のprivate関数`_process_single_candidate()`をそのままimportする。diff最小)を
採用した。`_process_single_candidate()`自体が、バッチ完了検知時の
`_finalize_batch()`呼び出しを内包しているため、本moduleはそれを別途呼ばない。

`BatchSize`は`BuyCandidateSqsBatchSize`(既定1。infra/template.yaml)のため、
1回の呼び出しにつき`event["Records"]`は通常1件だが、複数件が来た場合も
順に処理する(watchlist_worker_handler.pyと同型)。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import CandidateSource
from jstock_advisor.infrastructure.line.client import build_line_client_for_run
from jstock_advisor.infrastructure.local_repository.buy_candidate_evaluation_record_repository import (  # noqa: E501
    BuyCandidateEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.latest_buy_candidate_batch_pointer_repository import (  # noqa: E501
    LatestBuyCandidateBatchPointerRepository,
)
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
from jstock_advisor.lambda_handlers.buy_candidates_handler import _process_single_candidate
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.provider_factory import build_real_provider_bundle

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    processed = [_process_one(json.loads(record["body"])) for record in event.get("Records", [])]
    return {"processed": len(processed)}


def _process_one(body: dict[str, Any]) -> dict[str, Any]:
    execution_context = resolve_execution_context(body)
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    calendar = BusinessCalendar.from_config(config.holiday_calendar)
    providers = build_real_provider_bundle(now, config)
    recommendation_repo = RecommendationRepository.for_execution_context(execution_context)
    evaluation_record_repo = BuyCandidateEvaluationRecordRepository()
    latest_batch_pointer_repo = LatestBuyCandidateBatchPointerRepository()
    # buy_candidates_handler.handler()のtask=="buy_candidate"分岐と同じ規約
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
    average_acquisition_price = (
        Decimal(body["average_acquisition_price"])
        if body.get("average_acquisition_price") is not None
        else None
    )
    result = _process_single_candidate(
        body["stock_code"],
        CandidateSource(body["source"]),
        body.get("holding_quantity"),
        average_acquisition_price,
        body.get("batch_id"),
        now,
        providers,
        config,
        calendar,
        recommendation_repo,
        notification_service,
        execution_context,
        evaluation_record_repo,
        latest_batch_pointer_repo,
    )
    logger.info(
        "buy_candidate_worker_handler single candidate done stock_code=%s "
        "recommended=%s notified=%s failed=%s batch_id=%s",
        result.get("stock_code"),
        result.get("recommended"),
        result.get("notified"),
        result.get("failed"),
        body.get("batch_id"),
    )
    return result
