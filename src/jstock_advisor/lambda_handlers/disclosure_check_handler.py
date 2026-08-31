"""適時開示チェックLambda(schedule.yaml disclosure_check、平日数回)。

CLIの`jstock analyze disclosure-check --source real --notify`と同じロジックを
EventBridge Scheduler経由で自動実行する薄いアダプタ。

Issue #109: 本handlerは以前`event`を一切読まず、`execution_mode`を黙殺していた。
そのため`{"execution_mode": "VALIDATION"}`で手動起動しても既定のNORMALとして
扱われ、**実LINE送信・本番NotificationLog書き込みが行われる**状態だった
(ExecutionContextの既定がNORMALであるため、注入し忘れが「黙って本番実行」へ
倒れるfail-openな既定)。buy/holdingsと同じく`resolve_execution_context(event)`を
経由し、`LineNotificationService`へcontextを注入する。

VALIDATION時の抑止(外部LINE push・NotificationLog・NotificationClaim)は
LineNotificationService側に既に実装済みであり、本handlerはcontextを渡すだけで
その契約に乗る(#109ではservice側のsemanticsを変更しない)。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.infrastructure.line.client import build_line_client_from_env
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
from jstock_advisor.services.disclosure_check_service import DisclosureCheckService
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.provider_factory import build_real_provider_bundle

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    # Issue #109: 不正なexecution_mode/notification_modeは他の一切の処理より前に
    # 例外とする(buy/holdingsと同じ順序)。EventBridge Schedulerはmodeを渡さない
    # ため(infra/template.yamlのScheduleV2にInput指定なし)、通常の自然実行では
    # ExecutionContext.normal()となり従来どおりNORMAL + SENDで動作する。
    execution_context = resolve_execution_context(event)

    now = dt.datetime.now(dt.UTC)
    config = load_config()
    providers = build_real_provider_bundle(now, config)
    service = DisclosureCheckService(disclosure_provider=providers.disclosure, config=config)
    notification_service = LineNotificationService(
        line_client=build_line_client_from_env(),
        notification_log_repository=NotificationLogRepository(),
        # LINE通知dedupの原子化(Issue #17): NORMAL実行の送信決定を原子的に
        # 一意化するclaimリポジトリ(VALIDATION/DRY_RUNでは使用されない)。
        notification_claim_repository=NotificationClaimRepository(),
        recommendation_repository=RecommendationRepository(),
        config=config,
        # Issue #109: VALIDATION時の外部LINE push抑止(_push()のis_dry_run)、
        # NotificationClaim抑止(_claims_enabled())、NotificationLog抑止
        # (notify_disclosure_risk()内のis_validationガード)はいずれもservice側に
        # 実装済み。ここで注入しなければ既定のNORMALとなり全て素通りする。
        execution_context=execution_context,
    )

    if execution_context.is_validation:
        # 通知検証モード機能(2026-08追加)の他handlerと同じく、VALIDATION実行で
        # あることと解決後のnotification_modeをCloudWatch Logsへ明示する。
        logger.info(
            "VALIDATION MODE START execution_mode=VALIDATION notification_mode=%s "
            "event_notification_mode=%r is_dry_run=%s",
            execution_context.notification_mode.value,
            event.get("notification_mode"),
            execution_context.is_dry_run,
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
            stock_name=alert.stock_name,
        ):
            notified += 1

    logger.info("disclosure_check_handler done: alerts=%d notified=%d", len(alerts), notified)
    return {"alerts": len(alerts), "notified": notified}
