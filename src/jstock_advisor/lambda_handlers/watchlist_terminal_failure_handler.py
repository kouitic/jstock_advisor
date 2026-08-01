"""ウォッチリスト自動追加(候補ユニバース本格対応)のTerminal Failure Handler(4節)。

メインキュー(WatchlistScreeningQueue)でmaxReceiveCount(3回)を使い果たした
メッセージは、SQSのRedrivePolicyによりTerminalFailureQueueへ移動する。この
Lambdaはそのトリガーとして起動し、メッセージを消費・削除しながら該当銘柄を
FAILED確定する(4節: TerminalFailureQueueは"作業用キュー"、自動消費してFAILED
確定する。監視対象はこのHandlerの実行回数(Invocations)自体)。

Handler自体が失敗した場合(例外送出によりメッセージが削除されない)は、
TerminalFailureQueue自身のRedrivePolicy(maxReceiveCount 3回)により、真正の
DLQ(WatchlistTerminalFailureDLQ、何も自動消費しない)へ移動する。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.infrastructure.aws.batch_tracker import record_terminal_failure
from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.provider_factory import build_real_provider_bundle
from jstock_advisor.services.watchlist_batch_finalizer import maybe_finalize
from jstock_advisor.services.watchlist_data_cache import build_cached_provider_bundle

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _build_notification_service(config: AppConfig) -> LineNotificationService:
    return LineNotificationService(
        line_client=build_line_client_from_env(),
        notification_log_repository=NotificationLogRepository(),
        recommendation_repository=RecommendationRepository(),
        config=config,
    )


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    providers = build_cached_provider_bundle(build_real_provider_bundle(now, config), config, now)
    notification_service = _build_notification_service(config)

    processed: list[dict[str, str]] = []
    for record in event.get("Records", []):
        body = json.loads(record["body"])
        batch_id = body["batch_id"]
        stock_code = body["stock_code"]

        if record_terminal_failure(batch_id, stock_code, now):
            maybe_finalize(batch_id, now, providers, config, notification_service)
        else:
            # 既に他の主体(Worker/Reconciler)が終端状態へ確定済み(冪等スキップ)。
            logger.info(
                "watchlist terminal failure handler: already terminal batch_id=%s stock_code=%s",
                batch_id,
                stock_code,
            )
        processed.append({"batch_id": batch_id, "stock_code": stock_code})

    logger.info("watchlist terminal failure handler processed %d messages", len(processed))
    return {"processed": processed}
