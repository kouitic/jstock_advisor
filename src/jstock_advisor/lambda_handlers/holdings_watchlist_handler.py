"""保有銘柄・ウォッチリスト分析Lambda(schedule.yaml daily_holdings_watchlist_analysis、平日16:30)。

CLIの`jstock analyze holdings --source real --notify`および
`jstock analyze watchlist --source real --notify`と同じロジックを
EventBridge Scheduler経由で自動実行する薄いアダプタ。

銘柄単位のファンアウト(_fanout.py)を採用しており、通常のスケジュール起動では
銘柄一覧を取得して銘柄ごとに自分自身を非同期再帰呼び出しするだけで即座に戻る。
実際のデータ取得・判定・通知は、"task"付きで再帰呼び出しされた各インスタンスが
1銘柄のみを担当して行う。
"""

from __future__ import annotations

import datetime as dt
import logging
import os
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
from jstock_advisor.lambda_handlers._fanout import dispatch_async, resolve_function_name
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
    stock_snapshotを一度だけ取得して両方に渡す(実データ取得の重複を避ける)。
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


def _process_single_holding(
    stock_code: str,
    now: dt.datetime,
    providers: ProviderBundle,
    config: AppConfig,
    recommendation_repo: RecommendationRepository,
    notification_service: LineNotificationService,
) -> dict[str, Any]:
    holding = PortfolioService().get_holding(stock_code)
    if holding is None:
        logger.warning("dispatched holding not found stock_code=%s", stock_code)
        return {"stock_code": stock_code, "recommended": False, "notified": False, "found": False}

    profit_service = ProfitTakingService(providers=providers, config=config)
    sell_service = SellSignalService(providers=providers, config=config)
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
    except Exception:  # noqa: BLE001 - 1銘柄の想定外エラーで再帰呼び出し全体を落とさない
        logger.exception("holding analysis failed unexpectedly stock_code=%s", stock_code)
        return {"stock_code": stock_code, "recommended": False, "notified": False, "failed": True}
    return {"stock_code": stock_code, "recommended": was_recommended, "notified": was_notified}


def _process_single_watchlist_item(
    stock_code: str,
    now: dt.datetime,
    providers: ProviderBundle,
    config: AppConfig,
    calendar: BusinessCalendar,
    recommendation_repo: RecommendationRepository,
    notification_service: LineNotificationService,
) -> dict[str, Any]:
    item = WatchlistService().get_item(stock_code)
    if item is None:
        logger.warning("dispatched watchlist item not found stock_code=%s", stock_code)
        return {"stock_code": stock_code, "recommended": False, "notified": False, "found": False}

    service = BuySignalService(providers=providers, config=config, business_calendar=calendar)
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
            return {"stock_code": stock_code, "recommended": False, "notified": False}
        if outcome.recommendation is None:
            return {"stock_code": stock_code, "recommended": False, "notified": False}
        recommendation_repo.save(outcome.recommendation)
        notified = notification_service.notify_recommendation(outcome.recommendation, now)
        return {"stock_code": stock_code, "recommended": True, "notified": notified}
    except Exception:  # noqa: BLE001 - 1銘柄の想定外エラーで再帰呼び出し全体を落とさない
        logger.exception("watchlist analysis failed unexpectedly stock_code=%s", stock_code)
        return {"stock_code": stock_code, "recommended": False, "notified": False, "failed": True}


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    providers = build_real_provider_bundle(now, config)
    recommendation_repo = RecommendationRepository()
    notification_service = LineNotificationService(
        line_client=build_line_client_from_env(),
        notification_log_repository=NotificationLogRepository(),
        recommendation_repository=recommendation_repo,
        config=config,
    )

    task = event.get("task")
    if task == "holding":
        result = _process_single_holding(
            event["stock_code"], now, providers, config, recommendation_repo, notification_service
        )
        logger.info("holdings_watchlist_handler single holding done: %s", result)
        return result

    if task == "watchlist":
        calendar = BusinessCalendar.from_config(config.holiday_calendar)
        result = _process_single_watchlist_item(
            event["stock_code"],
            now,
            providers,
            config,
            calendar,
            recommendation_repo,
            notification_service,
        )
        logger.info("holdings_watchlist_handler single watchlist item done: %s", result)
        return result

    # 通常のスケジュール起動(ディスパッチのみ行い、銘柄ごとの実処理は非同期の
    # 自己再帰呼び出しに委ねる。全銘柄を直列処理するとLambdaの最大タイムアウト
    # (900秒)を超えうるため)
    function_name = resolve_function_name(context, os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""))
    holdings = PortfolioService().list_holdings()
    for holding in holdings:
        dispatch_async(function_name, {"task": "holding", "stock_code": holding.stock_code})
    items = WatchlistService().list_items()
    for item in items:
        dispatch_async(function_name, {"task": "watchlist", "stock_code": item.stock_code})

    logger.info(
        "holdings_watchlist_handler dispatched: holdings=%d watchlist=%d", len(holdings), len(items)
    )
    return {"dispatched_holdings": len(holdings), "dispatched_watchlist": len(items)}
