"""買い候補分析Lambda(schedule.yaml daily_buy_candidates_analysis、平日08:00)。

CLIの`jstock analyze buy-candidates --source real --notify`と同じロジックを
EventBridge Scheduler経由で自動実行する薄いアダプタ。

【スコープ上の判断】ローカルCLIでは`--source real`時に対象銘柄コードの指定を
必須としている(市場全体を自動スキャンする実データ取得元が未接続のため)。
自動実行では人間がコードを指定できないため、本ハンドラは便宜的に
ウォッチリスト登録銘柄を走査対象とする(全上場銘柄の自動スクリーニングでは
ない点に注意。真の市場全体スキャンには別途ユニバース管理機能が必要)。

銘柄単位のファンアウト(_fanout.py)を採用しており、通常のスケジュール起動では
銘柄一覧を取得して銘柄ごとに自分自身を非同期再帰呼び出しするだけで即座に戻る。

【購入候補/価格待ちの2ランキング化(2026-07 BUYパイプライン再設計・要求仕様17節)】
「企業として投資候補になり得るか」と「現在の株価で実際に購入すべきか」を分離した
ため、通知ランキングも2種類に分ける。各ワーカーはBuyAction判定が確定した時点で
即座に送信せず、購入候補(STRONG_BUY/BUY/SMALL_ENTRY)と価格待ち
(WATCH_FOR_PRICE/WATCH_BEFORE_EARNINGS)を別々にバッチトラッカーへ登録するだけに
留める。全銘柄の処理が完了した時点(最後のワーカーが検知)で、それぞれの
ランキング上位config.notification.buy_candidate_max_notifications_per_run件のみを
実際に送信する。「優先順位の高いN件」という単一ランキングでの表現は行わない。

個別のデータ取得エラーはLINEへ配信せず、全銘柄の処理が完了した時点で
全体件数・正常件数・異常件数のサマリーを1通だけ送信する
(batch_tracker.pyのDynamoDB原子カウンタで完了を検知する)。
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import uuid
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import BuyAction, NotificationStatus
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.aws.batch_tracker import (
    BatchProgress,
    record_result,
    start_batch,
)
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
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.provider_factory import build_real_provider_bundle
from jstock_advisor.services.watchlist_service import WatchlistService

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_PROCESS_NAME = "買い候補分析"
_RANKING_ENTRY_DELIMITER = "|"
_BUY_PREFIX = "BUY"
_WATCH_PREFIX = "WATCH"

# 購入候補ランキングの第一ソートキー(BuyActionの強さ。数値が大きいほど優先)。
_ACTION_PRIORITY: dict[BuyAction, int] = {
    BuyAction.SMALL_ENTRY: 0,
    BuyAction.BUY: 1,
    BuyAction.STRONG_BUY: 2,
}


def _encode_buy_ranking_entry(recommendation: Recommendation) -> str:
    """購入候補ランキングキー: (action_priority, purchase_attractiveness_score,
    company_quality_score) の降順(要求仕様17節)。
    """
    action_priority = _ACTION_PRIORITY.get(recommendation.buy_action, 0)  # type: ignore[arg-type]
    purchase_score = recommendation.purchase_attractiveness_score or 0.0
    quality_score = recommendation.company_quality_score or 0.0
    return _RANKING_ENTRY_DELIMITER.join(
        [
            _BUY_PREFIX,
            str(action_priority),
            str(purchase_score),
            str(quality_score),
            recommendation.stock_code,
            recommendation.recommendation_id,
        ]
    )


def _encode_watch_ranking_entry(recommendation: Recommendation) -> str:
    """価格待ちランキングキー: (company_quality_score, -distance_to_entry_price_pct)
    の降順(要求仕様17節)。距離が不明な場合は最下位扱いとする。
    """
    quality_score = recommendation.company_quality_score or 0.0
    distance_pct = (
        float(recommendation.current_vs_entry_price_pct)
        if recommendation.current_vs_entry_price_pct is not None
        else 999999.0
    )
    return _RANKING_ENTRY_DELIMITER.join(
        [
            _WATCH_PREFIX,
            str(quality_score),
            str(distance_pct),
            recommendation.stock_code,
            recommendation.recommendation_id,
        ]
    )


def _decode_ranking_entry(entry: str) -> tuple[str, tuple[float, ...], str, str]:
    parts = entry.split(_RANKING_ENTRY_DELIMITER)
    prefix = parts[0]
    sort_key: tuple[float, ...]
    if prefix == _BUY_PREFIX:
        _, action_priority, purchase_score, quality_score, stock_code, recommendation_id = parts
        sort_key = (float(action_priority), float(purchase_score), float(quality_score))
    else:
        _, quality_score, distance_pct, stock_code, recommendation_id = parts
        sort_key = (float(quality_score), -float(distance_pct))
    return prefix, sort_key, stock_code, recommendation_id


def _process_single_candidate(
    stock_code: str,
    batch_id: str | None,
    now: dt.datetime,
    providers: ProviderBundle,
    config: AppConfig,
    calendar: BusinessCalendar,
    recommendation_repo: RecommendationRepository,
    notification_service: LineNotificationService,
) -> dict[str, Any]:
    service = BuySignalService(providers=providers, config=config, business_calendar=calendar)
    category = "failed"
    ranking_entry: str | None = None
    try:
        outcome = service.analyze(stock_code, now)
        if outcome.data_error:
            item = WatchlistService().get_item(stock_code)
            notification_service.notify_data_error(
                stock_code, outcome.data_error, now, stock_name=item.stock_name if item else None
            )
            category = "data_insufficient"
            result = {"stock_code": stock_code, "recommended": False, "notified": False}
        elif outcome.buy_action == BuyAction.EXCLUDED or outcome.recommendation is None:
            # 投資対象スクリーニングで除外(第1段階)。screening_passed=Falseの場合、
            # recommendationは常にNoneとなる想定(buy_signal_service.py参照)。
            category = "hold"
            result = {"stock_code": stock_code, "recommended": False, "notified": False}
        else:
            recommendation_repo.save(outcome.recommendation)
            eligibility = notification_service.evaluate_notification_status(
                outcome.recommendation, now
            )
            if outcome.buy_action == BuyAction.MANUAL_REVIEW or eligibility.data_quality_blocked:
                # 要手動確認・データ品質アラートは優先度付けの対象外とし、
                # evaluate_notification_status内で即時に処理済み(要求仕様§11・§17・§20)。
                category = "review"
                result = {"stock_code": stock_code, "recommended": True, "notified": False}
            elif eligibility.status != NotificationStatus.SENT:
                category = "suppressed"
                result = {"stock_code": stock_code, "recommended": True, "notified": False}
            elif outcome.ranking_group == "buy_candidate":
                # 実際の送信は行わず、ランキング候補として登録するだけに留める
                # (全銘柄処理完了後、購入候補ランキング上位N件のみ送信する)。
                category = "candidate_not_ranked"
                ranking_entry = _encode_buy_ranking_entry(outcome.recommendation)
                result = {"stock_code": stock_code, "recommended": True, "notified": False}
            elif outcome.ranking_group == "watch_price":
                category = "watch_not_ranked"
                ranking_entry = _encode_watch_ranking_entry(outcome.recommendation)
                result = {"stock_code": stock_code, "recommended": True, "notified": False}
            else:
                # NOT_ATTRACTIVE等、購入候補にも価格待ちにも属さない場合は
                # 通知せず保有継続(hold)相当として扱う。
                category = "hold"
                result = {"stock_code": stock_code, "recommended": True, "notified": False}
    except Exception:  # noqa: BLE001 - 1銘柄の想定外エラーで再帰呼び出し全体を落とさない
        logger.exception("buy candidate analysis failed unexpectedly stock_code=%s", stock_code)
        result = {"stock_code": stock_code, "recommended": False, "notified": False, "failed": True}

    if batch_id is not None:
        needs_code = category in ("data_insufficient", "failed")
        stock_code_for_category = stock_code if needs_code else None
        progress = record_result(
            batch_id, category, stock_code=stock_code_for_category, ranking_entry=ranking_entry
        )
        if progress is not None and progress.is_complete:
            _finalize_batch(progress, config, now, recommendation_repo, notification_service)
    return result


def _finalize_batch(
    progress: BatchProgress,
    config: AppConfig,
    now: dt.datetime,
    recommendation_repo: RecommendationRepository,
    notification_service: LineNotificationService,
) -> None:
    """全銘柄の処理完了を検知したワーカーが1回だけ呼ぶ。購入候補ランキング・
    価格待ちランキングそれぞれの上位N件のみ実際にLINE送信し、件数内訳を
    調整したうえでバッチサマリーを送信する。
    """
    max_notifications = config.notification.buy_candidate_max_notifications_per_run

    buy_entries: list[tuple[tuple[float, ...], str, str]] = []
    watch_entries: list[tuple[tuple[float, ...], str, str]] = []
    for entry in progress.ranking_entries:
        prefix, sort_key, stock_code, recommendation_id = _decode_ranking_entry(entry)
        target = buy_entries if prefix == _BUY_PREFIX else watch_entries
        target.append((sort_key, stock_code, recommendation_id))

    buy_entries.sort(key=lambda item: item[0], reverse=True)
    watch_entries.sort(key=lambda item: item[0], reverse=True)

    buy_winners = buy_entries[:max_notifications]
    watch_winners = watch_entries[:max_notifications]

    for _sort_key, stock_code, recommendation_id in (*buy_winners, *watch_winners):
        recommendation = recommendation_repo.get(recommendation_id)
        if recommendation is None:
            logger.warning(
                "buy_candidates_handler: recommendation not found for ranking winner "
                "stock_code=%s recommendation_id=%s",
                stock_code,
                recommendation_id,
            )
            continue
        notification_service.send_recommendation_notification(recommendation, now)

    total_buy_candidates = progress.category_counts.get("candidate_not_ranked", 0)
    total_watch_candidates = progress.category_counts.get("watch_not_ranked", 0)
    adjusted_counts = dict(progress.category_counts)
    adjusted_counts["sent"] = (
        progress.category_counts.get("sent", 0) + len(buy_winners) + len(watch_winners)
    )
    adjusted_counts["candidate_not_ranked"] = total_buy_candidates - len(buy_winners)
    adjusted_counts["watch_not_ranked"] = total_watch_candidates - len(watch_winners)

    notification_service.notify_batch_summary(
        _PROCESS_NAME,
        progress.total,
        adjusted_counts,
        now,
        data_insufficient_stock_codes=progress.data_insufficient_stock_codes,
        failed_stock_codes=progress.failed_stock_codes,
        buy_candidates_sent_count=len(buy_winners),
    )


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

    if event.get("task") == "buy_candidate":
        result = _process_single_candidate(
            event["stock_code"],
            event.get("batch_id"),
            now,
            providers,
            config,
            calendar,
            recommendation_repo,
            notification_service,
        )
        logger.info("buy_candidates_handler single candidate done: %s", result)
        return result

    # 通常のスケジュール起動(ディスパッチのみ行い、銘柄ごとの実処理は非同期の
    # 自己再帰呼び出しに委ねる)
    function_name = resolve_function_name(context, os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""))
    items = WatchlistService().list_items()
    batch_id = f"buy-candidates-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    start_batch(batch_id, len(items), now)

    for item in items:
        dispatch_async(
            function_name,
            {"task": "buy_candidate", "stock_code": item.stock_code, "batch_id": batch_id},
        )

    logger.info("buy_candidates_handler dispatched: scanned=%d batch_id=%s", len(items), batch_id)
    return {"dispatched": len(items)}
