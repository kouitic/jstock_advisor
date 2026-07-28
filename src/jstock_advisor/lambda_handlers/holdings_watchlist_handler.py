"""保有銘柄・ウォッチリスト分析Lambda(schedule.yaml daily_holdings_watchlist_analysis、平日16:30)。

CLIの`jstock analyze holdings --source real --notify`および
`jstock analyze watchlist --source real --notify`と同じロジックを
EventBridge Scheduler経由で自動実行する薄いアダプタ。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.buy_signal_service import BuySignalService
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.provider_factory import build_real_provider_bundle
from jstock_advisor.services.sell_signal_service import SellSignalService
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot
from jstock_advisor.services.watchlist_service import WatchlistService

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _analyze_one_holding(
    holding: Holding,
    now: dt.datetime,
    providers: ProviderBundle,
    config: AppConfig,
    profit_service: ProfitTakingService,
    sell_service: SellSignalService,
    recommendation_repo: RecommendationRepository,
    notification_service: LineNotificationService,
) -> tuple[bool, bool]:
    """1銘柄を判定・通知する。戻り値は(推奨が生成されたか, 実際に通知を送信したか)。

    sell_signal/profit_takingは同一銘柄のデータを必要とするため、
    stock_snapshotを一度だけ取得して両方に渡す(要求仕様: 銘柄あたりの
    実データ取得を最小化し、Lambdaタイムアウトを避けるための最適化)。
    """
    snapshot, error = build_stock_snapshot(providers, holding.stock_code, now, config)
    if snapshot is None:
        logger.warning("data_error stock_code=%s error=%s", holding.stock_code, error)
        notification_service.notify_data_error(
            holding.stock_code, error or "データ取得エラー", now, stock_name=holding.stock_name
        )
        return False, False

    sell_outcome = sell_service.analyze(holding, now, snapshot=snapshot)
    if sell_outcome.recommendation is not None:
        recommendation_repo.save(sell_outcome.recommendation)
        notified = notification_service.notify_recommendation(sell_outcome.recommendation, now)
        return True, notified

    pt_outcome = profit_service.analyze(holding, now, snapshot=snapshot)
    if pt_outcome.recommendation is not None:
        recommendation_repo.save(pt_outcome.recommendation)
        notified = notification_service.notify_recommendation(pt_outcome.recommendation, now)
        return True, notified

    return False, False


def _analyze_holdings(
    now: dt.datetime,
    providers: ProviderBundle,
    config: AppConfig,
    recommendation_repo: RecommendationRepository,
    notification_service: LineNotificationService,
) -> dict[str, int]:
    profit_service = ProfitTakingService(providers=providers, config=config)
    sell_service = SellSignalService(providers=providers, config=config)
    holdings = PortfolioService().list_holdings()

    recommended = 0
    notified = 0
    failed = 0
    for holding in holdings:
        try:
            was_recommended, was_notified = _analyze_one_holding(
                holding,
                now,
                providers,
                config,
                profit_service,
                sell_service,
                recommendation_repo,
                notification_service,
            )
            recommended += int(was_recommended)
            notified += int(was_notified)
        except Exception:  # noqa: BLE001 - 1銘柄の想定外エラーが他銘柄の処理を止めないようにする
            failed += 1
            logger.exception(
                "holding analysis failed unexpectedly stock_code=%s", holding.stock_code
            )

    return {
        "scanned": len(holdings),
        "recommended": recommended,
        "notified": notified,
        "failed": failed,
    }


def _analyze_watchlist(
    now: dt.datetime,
    providers: ProviderBundle,
    config: AppConfig,
    calendar: BusinessCalendar,
    recommendation_repo: RecommendationRepository,
    notification_service: LineNotificationService,
) -> dict[str, int]:
    service = BuySignalService(providers=providers, config=config, business_calendar=calendar)
    items = WatchlistService().list_items()

    recommended = 0
    notified = 0
    failed = 0
    for item in items:
        try:
            outcome = service.analyze(
                item.stock_code, now, recommendation_type=RecommendationType.WATCH_BUY
            )
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
            recommended += 1
            recommendation_repo.save(outcome.recommendation)
            if notification_service.notify_recommendation(outcome.recommendation, now):
                notified += 1
        except Exception:  # noqa: BLE001 - 1銘柄の想定外エラーが他銘柄の処理を止めないようにする
            failed += 1
            logger.exception(
                "watchlist analysis failed unexpectedly stock_code=%s", item.stock_code
            )

    return {
        "scanned": len(items),
        "recommended": recommended,
        "notified": notified,
        "failed": failed,
    }


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    calendar = BusinessCalendar.from_config(config.holiday_calendar)
    providers = build_real_provider_bundle(now, config)
    recommendation_repo = RecommendationRepository()
    notification_service = LineNotificationService(
        line_client=build_line_client_from_env(),
        notification_log_repository=NotificationLogRepository(),
        recommendation_repository=recommendation_repo,
        config=config,
    )

    holdings_result = _analyze_holdings(
        now, providers, config, recommendation_repo, notification_service
    )
    watchlist_result = _analyze_watchlist(
        now, providers, config, calendar, recommendation_repo, notification_service
    )

    logger.info(
        "holdings_watchlist_handler done: holdings=%s watchlist=%s",
        holdings_result,
        watchlist_result,
    )
    return {"holdings": holdings_result, "watchlist": watchlist_result}
