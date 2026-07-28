"""買い候補分析Lambda(schedule.yaml daily_buy_candidates_analysis、平日08:00)。

CLIの`jstock analyze buy-candidates --source real --notify`と同じロジックを
EventBridge Scheduler経由で自動実行する薄いアダプタ。

【スコープ上の判断】ローカルCLIでは`--source real`時に対象銘柄コードの指定を
必須としている(市場全体を自動スキャンする実データ取得元が未接続のため)。
自動実行では人間がコードを指定できないため、本ハンドラは便宜的に
ウォッチリスト登録銘柄を走査対象とする(全上場銘柄の自動スクリーニングでは
ない点に注意。真の市場全体スキャンには別途ユニバース管理機能が必要)。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.buy_signal_service import BuySignalService
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.provider_factory import build_real_provider_bundle
from jstock_advisor.services.watchlist_service import WatchlistService

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    calendar = BusinessCalendar.from_config(config.holiday_calendar)
    providers = build_real_provider_bundle(now, config)
    service = BuySignalService(providers=providers, config=config, business_calendar=calendar)
    recommendation_repo = RecommendationRepository()
    notification_service = LineNotificationService(
        line_client=build_line_client_from_env(),
        notification_log_repository=NotificationLogRepository(),
        recommendation_repository=recommendation_repo,
        config=config,
    )

    items = WatchlistService().list_items()
    processed = 0
    notified = 0
    failed = 0
    for item in items:
        try:
            outcome = service.analyze(item.stock_code, now)
            if outcome.data_error:
                logger.warning(
                    "data_error stock_code=%s error=%s", item.stock_code, outcome.data_error
                )
                notification_service.notify_data_error(
                    item.stock_code, outcome.data_error, now, stock_name=item.stock_name
                )
                continue
            if outcome.recommendation is None:
                continue
            processed += 1
            recommendation_repo.save(outcome.recommendation)
            if notification_service.notify_recommendation(outcome.recommendation, now):
                notified += 1
        except Exception:  # noqa: BLE001 - 1銘柄の想定外エラーが他銘柄の処理を止めないようにする
            failed += 1
            logger.exception(
                "buy candidate analysis failed unexpectedly stock_code=%s", item.stock_code
            )

    logger.info(
        "buy_candidates_handler done: scanned=%d recommended=%d notified=%d failed=%d",
        len(items),
        processed,
        notified,
        failed,
    )
    return {"scanned": len(items), "recommended": processed, "notified": notified, "failed": failed}
