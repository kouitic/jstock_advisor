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

【購入候補のみをランキング・通知(BUYパイプライン第2次修正2026-07・要求仕様13〜16節)】
「企業として投資候補になり得るか」と「現在の株価で実際に購入すべきか」を分離した
ため、監視継続・購入見送り・要確認・データ不足・対象外はLINE通知しない
(分析結果・監査ログへの記録はBuySignalService.analyze()側で全銘柄について既に
完了している)。各ワーカーはBuyAction判定が確定した時点で、購入候補
(STRONG_BUY/BUY/SMALL_ENTRY)のみをランキング候補としてバッチトラッカーへ登録する
(価格待ちは件数カウントのみ行い、送信対象のランキングには載せない)。全銘柄の
処理が完了した時点(最後のワーカーが検知)で、購入候補ランキング順に1件ずつ
再通知抑止・データ品質チェックを実行し(要求仕様15節)、条件を満たしたものを
config.notification.buy_candidate_max_notifications_per_run件に達するまで、
または全件評価し終えるまで繰り上げながら集める(BUYパイプライン第3次修正
2026-07: 上位候補が抑止された場合に下位の適格候補を繰り上げず送信数が
目標件数を割り込む不具合を修正)。条件を満たしたものだけを1通(長すぎる場合の
み複数通)にまとめて送信する。「優先順位の高いN件」という単一ランキングでの
表現は行わない。

購入候補が1件も無い場合、config.notification.send_empty_summaryがfalseなら
バッチ完了サマリー自体を送信しない(要求仕様16節: 無理に候補を作らず、
何も無い日は通知しない)。

個別のデータ取得エラーは既定でLINEへ配信せず(config.notification.
buy_candidates.notify_data_errorsで制御。BUYパイプライン第3次修正2026-07)、
CloudWatch警告ログとバッチサマリーのdata_insufficient件数にのみ記録する。
全銘柄の処理が完了した時点で全体件数・正常件数・異常件数のサマリーを1通だけ
送信する(batch_tracker.pyのDynamoDB原子カウンタで完了を検知する)。
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
from jstock_advisor.domain.entities.enums import (
    BuyAction,
    NotificationContext,
    NotificationStatus,
)
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

# 購入候補ランキングの第一ソートキー(BuyActionの強さ。数値が大きいほど優先)。
_ACTION_PRIORITY: dict[BuyAction, int] = {
    BuyAction.SMALL_ENTRY: 0,
    BuyAction.BUY: 1,
    BuyAction.STRONG_BUY: 2,
}


def _encode_buy_ranking_entry(recommendation: Recommendation) -> str:
    """購入候補ランキングキー(要求仕様15節): action_priority(BuyActionの強さ)
    → purchase_attractiveness_score → company_quality_score →
    現在値が標準買い価格をどれだけ下回るか、の降順。同点時は銘柄コード昇順で
    決定性を確保する(_finalize_batch側でタプルの最後の要素として比較される)。

    価格待ち(WATCH_FOR_PRICE/WATCH_BEFORE_EARNINGS)はLINE通知対象外のため
    ランキング登録自体を行わない(BUYパイプライン第2次修正2026-07)。
    """
    action_priority = _ACTION_PRIORITY.get(recommendation.buy_action, 0)  # type: ignore[arg-type]
    purchase_score = recommendation.purchase_attractiveness_score or 0.0
    quality_score = recommendation.company_quality_score or 0.0
    standard_price = (
        recommendation.standard_buy_price if recommendation.standard_buy_price is not None else None
    )
    discount_to_standard_pct = (
        float((standard_price - recommendation.price_at_recommendation) / standard_price * 100)
        if standard_price is not None and standard_price > 0
        else 0.0
    )
    return _RANKING_ENTRY_DELIMITER.join(
        [
            str(action_priority),
            str(purchase_score),
            str(quality_score),
            str(discount_to_standard_pct),
            recommendation.stock_code,
            recommendation.recommendation_id,
        ]
    )


def _decode_buy_ranking_entry(entry: str) -> tuple[tuple[float, ...], str, str]:
    action_priority, purchase_score, quality_score, discount_pct, stock_code, recommendation_id = (
        entry.split(_RANKING_ENTRY_DELIMITER)
    )
    sort_key = (
        float(action_priority),
        float(purchase_score),
        float(quality_score),
        float(discount_pct),
    )
    return sort_key, stock_code, recommendation_id


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
            # --- BUYパイプライン第3次修正(2026-07)。要求仕様: 個別のデータ取得
            # エラーは購入候補通知パイプラインからLINE個別送信しない(既定)。
            # CloudWatch警告ログとバッチ完了サマリーのdata_insufficient件数への
            # 集計のみとする。監査ログへの記録はBuySignalService.analyze()側
            # (snapshot is Noneの分岐)で既に完了している。運用上どうしても
            # 個別のLINE通知が必要な場合のみ、config.notification.buy_candidates.
            # notify_data_errorsをtrueにすることで有効化できる。
            item = WatchlistService().get_item(stock_code)
            if config.notification.buy_candidates.notify_data_errors:
                notification_service.notify_data_error(
                    stock_code,
                    outcome.data_error,
                    now,
                    stock_name=item.stock_name if item else None,
                )
            else:
                name_part = f" {item.stock_name}" if item and item.stock_name else ""
                logger.warning(
                    "buy_candidate_data_error stock_code=%s%s message=%s",
                    stock_code,
                    name_part,
                    outcome.data_error,
                )
            category = "data_insufficient"
            result = {"stock_code": stock_code, "recommended": False, "notified": False}
        elif outcome.buy_action == BuyAction.EXCLUDED or outcome.recommendation is None:
            # 投資対象スクリーニングで除外(第1段階)。screening_passed=Falseの場合、
            # recommendationは常にNoneとなる想定(buy_signal_service.py参照)。
            category = "hold"
            result = {"stock_code": stock_code, "recommended": False, "notified": False}
        else:
            # --- BUYパイプライン第2次修正(2026-07)。要求仕様13〜15節 ---
            # ここではLINE通知の可否(再通知抑止・データ品質チェック)を判定しない。
            # 監査ログへの記録はBuySignalService.analyze()側で既に完了しており、
            # 通知対象の判定(evaluate_notification_status)は購入候補ランキング
            # 上位N件が確定した後、_finalize_batchで初めて行う(全銘柄一律に
            # 重い整合性検証・異常値検知を実行しない)。
            recommendation_repo.save(outcome.recommendation)
            if outcome.buy_action == BuyAction.MANUAL_REVIEW:
                category = "review"
                result = {"stock_code": stock_code, "recommended": True, "notified": False}
            elif outcome.ranking_group == "buy_candidate":
                # 実際の送信可否判定は行わず、ランキング候補として登録するだけに
                # 留める(全銘柄処理完了後、購入候補ランキング上位N件のみを対象に
                # 再通知抑止・データ品質チェックを行い送信する)。
                category = "candidate_not_ranked"
                ranking_entry = _encode_buy_ranking_entry(outcome.recommendation)
                result = {"stock_code": stock_code, "recommended": True, "notified": False}
            elif outcome.ranking_group == "watch_price":
                # 価格待ちはLINE通知対象外のため、ランキング登録は行わず件数のみ
                # 集計する(バッチサマリーの内訳表示・監査目的)。
                category = "watch_not_ranked"
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
    """全銘柄の処理完了を検知したワーカーが1回だけ呼ぶ。購入候補ランキング順に
    1件ずつ再通知抑止・データ品質チェックを行い(要求仕様15節ステップ7)、
    条件を満たしたものを送信対象数(既定5件)に達するまで、または全件評価し
    終えるまで繰り上げながら集める(要求仕様15節ステップ8)。価格待ちは
    ランキング・送信の対象外(件数のみバッチサマリーに反映する)。

    --- BUYパイプライン第3次修正(2026-07)で修正 ---
    従来は「上位N件を先に切り出してから適格性を判定する」実装だったため、
    上位N件のうち一部が再送抑止・データ品質チェックで除外されると、下位に
    適格な候補が残っていても繰り上げられず、送信数がN件を割り込んでいた。
    ランキング全件を順番に評価し、適格と判定したものから送信対象へ加える
    (=繰り上げ方式)ことでこれを解消する。通知本文の表示順位は
    (notify_buy_candidates_digest側の実装により)最終的な送信順で1..Nへ
    振り直される。
    """
    max_notifications = config.notification.buy_candidate_max_notifications_per_run

    buy_entries: list[tuple[tuple[float, ...], str, str]] = [
        _decode_buy_ranking_entry(entry) for entry in progress.ranking_entries
    ]
    # 降順ソート。同点時は銘柄コード昇順で決定性を確保する(要求仕様15節)。
    buy_entries.sort(key=lambda item: (tuple(-v for v in item[0]), item[1]))

    eligible_winners: list[Recommendation] = []
    quality_blocked_count = 0
    suppressed_count = 0
    evaluated_count = 0
    for _sort_key, stock_code, recommendation_id in buy_entries:
        recommendation = recommendation_repo.get(recommendation_id)
        if recommendation is None:
            logger.warning(
                "buy_candidates_handler: recommendation not found for ranking winner "
                "stock_code=%s recommendation_id=%s",
                stock_code,
                recommendation_id,
            )
            continue
        evaluated_count += 1
        eligibility = notification_service.evaluate_notification_status(
            recommendation, now, context=NotificationContext.BUY_CANDIDATE_BATCH
        )
        if eligibility.data_quality_blocked:
            # 要手動確認・データ品質アラートはevaluate_notification_status内で
            # 判定済み(BUY_CANDIDATE_BATCHコンテキストのためLINE送信はされない)。
            quality_blocked_count += 1
            continue
        if eligibility.status != NotificationStatus.SENT:
            suppressed_count += 1
            continue
        eligible_winners.append(recommendation)
        if len(eligible_winners) >= max_notifications:
            break

    sent_count = notification_service.notify_buy_candidates_digest(eligible_winners, now)

    total_buy_candidates = progress.category_counts.get("candidate_not_ranked", 0)
    adjusted_counts = dict(progress.category_counts)
    adjusted_counts["sent"] = progress.category_counts.get("sent", 0) + sent_count
    adjusted_counts["review"] = progress.category_counts.get("review", 0) + quality_blocked_count
    adjusted_counts["suppressed"] = (
        progress.category_counts.get("suppressed", 0) + suppressed_count
    )
    adjusted_counts["candidate_not_ranked"] = total_buy_candidates - evaluated_count

    notification_service.notify_batch_summary(
        _PROCESS_NAME,
        progress.total,
        adjusted_counts,
        now,
        data_insufficient_stock_codes=progress.data_insufficient_stock_codes,
        failed_stock_codes=progress.failed_stock_codes,
        buy_candidates_sent_count=sent_count,
        send_empty_summary=config.notification.send_empty_summary,
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
