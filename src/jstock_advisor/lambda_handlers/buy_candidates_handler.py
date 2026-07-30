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

【優先度付け通知(2026-07仕様追加)】買いシグナルが成立した銘柄をすべて即座に
通知すると、ウォッチリストの規模によっては1回の実行で大量のLINE通知が
一斉送信されてしまう。これを避けるため、各ワーカーは買いシグナル成立時点では
即座に送信せず、買い候補スコア(Recommendation.total_score)とともに
バッチトラッカーへ登録するだけに留める。全銘柄の処理が完了した時点(最後の
ワーカーが検知)で、スコア上位
config.notification.buy_candidate_max_notifications_per_run件のみを実際に
送信する。

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
from jstock_advisor.domain.entities.enums import NotificationStatus
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


def _encode_ranking_entry(score: float, stock_code: str, recommendation_id: str) -> str:
    d = _RANKING_ENTRY_DELIMITER
    return f"{score}{d}{stock_code}{d}{recommendation_id}"


def _decode_ranking_entry(entry: str) -> tuple[float, str, str]:
    score_raw, stock_code, recommendation_id = entry.split(_RANKING_ENTRY_DELIMITER, 2)
    return float(score_raw), stock_code, recommendation_id


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
        elif outcome.recommendation is None:
            category = "hold"
            result = {"stock_code": stock_code, "recommended": False, "notified": False}
        else:
            recommendation_repo.save(outcome.recommendation)
            eligibility = notification_service.evaluate_notification_status(
                outcome.recommendation, now
            )
            if eligibility.data_quality_blocked:
                # 要手動確認・データ品質アラートは優先度付けの対象外とし、
                # evaluate_notification_status内で即時に処理済み(要求仕様§11・§17)。
                category = "review"
                result = {"stock_code": stock_code, "recommended": True, "notified": False}
            elif eligibility.status == NotificationStatus.SENT:
                # 実際の送信は行わず、スコアとともにランキング候補として登録するだけに
                # 留める(全銘柄処理完了後、上位N件のみ送信する)。
                category = "candidate_not_ranked"
                ranking_entry = _encode_ranking_entry(
                    outcome.recommendation.total_score or 0.0,
                    stock_code,
                    outcome.recommendation.recommendation_id,
                )
                result = {"stock_code": stock_code, "recommended": True, "notified": False}
            else:
                category = "suppressed"
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
    """全銘柄の処理完了を検知したワーカーが1回だけ呼ぶ。買い候補スコア上位N件のみ
    実際にLINE送信し、件数内訳を調整したうえでバッチサマリーを送信する。
    """
    max_notifications = config.notification.buy_candidate_max_notifications_per_run
    candidates = sorted(
        (_decode_ranking_entry(entry) for entry in progress.ranking_entries),
        key=lambda c: c[0],
        reverse=True,
    )
    winners = candidates[:max_notifications]

    for _score, stock_code, recommendation_id in winners:
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

    total_candidates = progress.category_counts.get("candidate_not_ranked", 0)
    adjusted_counts = dict(progress.category_counts)
    adjusted_counts["sent"] = progress.category_counts.get("sent", 0) + len(winners)
    adjusted_counts["candidate_not_ranked"] = total_candidates - len(winners)

    notification_service.notify_batch_summary(
        _PROCESS_NAME,
        progress.total,
        adjusted_counts,
        now,
        data_insufficient_stock_codes=progress.data_insufficient_stock_codes,
        failed_stock_codes=progress.failed_stock_codes,
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
