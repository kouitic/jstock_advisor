"""ウォッチリスト自動追加(候補ユニバース本格対応)のDispatcher Lambda(1節)。

EventBridge週次トリガーで起動する。候補ユニバースの取得(Downloader)・確定
(Collector)・進捗行の作成・SQSへのdispatchのみを行う。銘柄ごとの評価は
WatchlistWorkerFunction(SQSトリガー、watchlist_worker_handler.py)が行う。

処理順序(1節):
0. dispatch leaseの取得(18節「対策1」、同一batch_idの多重実行排除)
1. Downloader(6節: 週次起動時に毎回実行し初回キャッシュ作成フローと統一)→
   CandidateUniverseProvider→WatchlistCandidateCollectorで候補確定
2. BatchRunsTableへtotalを設定
3. 進捗行の差分作成(18節「対策2」)+件数照合(不一致ならDISPATCH_FAILED、13節)
4. SendMessageBatchで10件ずつSQSへ投入(部分失敗は再送、上限後は直接FAILED確定)
5. dispatch_completed=trueへ更新し、try_finalize_if_ready(11節)を呼ぶ
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
import uuid
from typing import Any

import boto3

from jstock_advisor.config.loader import load_config
from jstock_advisor.infrastructure.aws.batch_tracker import (
    CandidateProgressRecord,
    create_missing_candidate_progress_rows,
    mark_candidate_dispatched,
    mark_dispatch_completed,
    mark_dispatch_failed,
    query_all_candidate_progress,
    record_dispatch_send_failure,
    set_watchlist_batch_total,
    try_acquire_dispatch_lease,
)
from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.interfaces.candidate_universe import CandidateUniverseError
from jstock_advisor.services.candidate_universe_downloader import (
    refresh_candidate_universe_cache,
)
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.provider_factory import (
    build_candidate_universe_provider,
    build_real_provider_bundle,
)
from jstock_advisor.services.screening_data_provider import StockSnapshotScreeningDataProvider
from jstock_advisor.services.watchlist_batch_finalizer import maybe_finalize
from jstock_advisor.services.watchlist_candidate_collector import WatchlistCandidateCollector
from jstock_advisor.services.watchlist_screening_audit import record_batch_audit

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 3節: WatchlistDispatcherFunctionのLambda Timeout(300秒)+60秒安全余裕(18節)。
_DISPATCH_LEASE_SECONDS = 360

_SQS_SEND_BATCH_SIZE = 10
_SQS_SEND_MAX_RETRIES = 3
_SQS_SEND_BASE_DELAY_SECONDS = 1.0
_SQS_SEND_MAX_DELAY_SECONDS = 10.0


def _chunked(
    values: list[CandidateProgressRecord], size: int
) -> list[list[CandidateProgressRecord]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def _send_batch_with_retry(
    sqs_client: Any, queue_url: str, entries: list[dict[str, str]]
) -> dict[str, bool]:
    """1節ステップ4: SendMessageBatchのSuccessful/Failedを必ず確認し、Failedのみ
    指数バックオフ(基準1秒、上限10秒)で最大3回再送する。"""
    results: dict[str, bool] = {}
    pending = {entry["Id"]: entry for entry in entries}
    attempt = 0
    while pending:
        response = sqs_client.send_message_batch(QueueUrl=queue_url, Entries=list(pending.values()))
        for successful in response.get("Successful", []):
            results[successful["Id"]] = True
            pending.pop(successful["Id"], None)
        if not pending:
            break
        attempt += 1
        if attempt > _SQS_SEND_MAX_RETRIES:
            for stock_code in pending:
                results[stock_code] = False
            break
        delay = min(
            _SQS_SEND_MAX_DELAY_SECONDS, _SQS_SEND_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
        )
        time.sleep(delay)
    return results


def _build_notification_service(config: Any) -> LineNotificationService:
    return LineNotificationService(
        line_client=build_line_client_from_env(),
        notification_log_repository=NotificationLogRepository(),
        recommendation_repository=RecommendationRepository(),
        config=config,
    )


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    wc = config.watchlist_screening
    cu = wc.candidate_universe

    if not (wc.enabled and wc.weekly_schedule_enabled):
        logger.info(
            "watchlist dispatcher skipped enabled=%s weekly_schedule_enabled=%s",
            wc.enabled,
            wc.weekly_schedule_enabled,
        )
        return {"skipped": True}

    batch_id = f"watchlist-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    owner_id = getattr(context, "aws_request_id", None) or uuid.uuid4().hex

    if not try_acquire_dispatch_lease(
        batch_id, owner_id, now, _DISPATCH_LEASE_SECONDS, wc.batch_record_ttl_hours
    ):
        # 通常のスケジュール実行では新規batch_idのため実質的に発生しないが、手動での
        # 同一batch_id再起動(18節「案B」)が既存の実行と競合した場合にここへ入る。
        logger.info("watchlist dispatcher: dispatch lease not acquired batch_id=%s", batch_id)
        return {"skipped": "lease_not_acquired"}

    if cu.provider == "jpx":
        # 6節: 週次Dispatcherの通常起動時にDownloaderも実行する(初回キャッシュ
        # 作成フローの統一)。取得・検証に失敗しても既存キャッシュで処理継続する。
        assert cu.jpx_listed_issues_url is not None
        assert cu.jpx_400_weight_url is not None
        outcomes = refresh_candidate_universe_cache(
            cu.jpx_listed_issues_url, cu.jpx_400_weight_url, cu.target_market_segments, now
        )
        for outcome in outcomes:
            if not outcome.promoted:
                logger.warning(
                    "candidate universe download/validation failed source=%s reason=%s "
                    "(既存キャッシュで処理継続)",
                    outcome.source,
                    outcome.reason,
                )

    providers = build_real_provider_bundle(now, config)
    universe_provider = build_candidate_universe_provider(config, now)
    screening_data_provider = StockSnapshotScreeningDataProvider(providers, config)
    collector = WatchlistCandidateCollector(
        universe_provider, screening_data_provider, staged_rollout=wc.staged_rollout
    )

    try:
        collector_result = collector.collect_target_codes()
    except CandidateUniverseError:
        logger.exception("watchlist candidate universe load failed batch_id=%s", batch_id)
        record_batch_audit(
            execution_mode="scheduled",
            universe_provider=cu.provider,
            screening_policies=[wc.screening_policy],
            output_values={"execution_result": "universe_load_failed"},
            now=now,
            batch_id=batch_id,
        )
        return {"error": "universe_load_failed"}

    total = len(collector_result.stock_codes)
    if total == 0:
        logger.info("watchlist dispatcher: no candidates to evaluate batch_id=%s", batch_id)
        record_batch_audit(
            execution_mode="scheduled",
            universe_provider=cu.provider,
            screening_policies=[wc.screening_policy],
            output_values={
                "execution_result": "no_candidates",
                "universe_count": collector_result.universe_count,
                "staged_rollout_excluded_count": collector_result.staged_rollout_excluded_count,
                "holding_excluded_count": collector_result.holding_excluded_count,
                "watchlist_excluded_count": collector_result.watchlist_excluded_count,
            },
            now=now,
            batch_id=batch_id,
        )
        return {"dispatched": 0}

    set_watchlist_batch_total(batch_id, total, wc.candidate_progress_ttl_hours, now)
    create_missing_candidate_progress_rows(
        batch_id, collector_result.stock_codes, now, wc.candidate_progress_ttl_hours
    )

    progress_rows = query_all_candidate_progress(batch_id, consistent_read=True)
    if len(progress_rows) != total:
        logger.error(
            "watchlist dispatcher: progress row count mismatch batch_id=%s expected=%d actual=%d",
            batch_id,
            total,
            len(progress_rows),
        )
        mark_dispatch_failed(batch_id, now)
        record_batch_audit(
            execution_mode="scheduled",
            universe_provider=cu.provider,
            screening_policies=[wc.screening_policy],
            output_values={
                "execution_result": "dispatch_failed_row_count_mismatch",
                "expected_total": total,
                "actual_progress_row_count": len(progress_rows),
            },
            now=now,
            batch_id=batch_id,
        )
        return {"error": "dispatch_failed_row_count_mismatch"}

    queue_url = os.environ["WATCHLIST_SCREENING_QUEUE_URL"]
    sqs = boto3.client("sqs")
    notification_service = _build_notification_service(config)

    to_dispatch = [row for row in progress_rows if not row.dispatched]
    for chunk in _chunked(to_dispatch, _SQS_SEND_BATCH_SIZE):
        entries = [
            {
                "Id": row.stock_code,
                "MessageBody": json.dumps({"batch_id": batch_id, "stock_code": row.stock_code}),
            }
            for row in chunk
        ]
        results = _send_batch_with_retry(sqs, queue_url, entries)
        for stock_code, success in results.items():
            if success:
                mark_candidate_dispatched(batch_id, stock_code, now)
            elif record_dispatch_send_failure(batch_id, stock_code, now):
                # この銘柄が最後の1件だった場合に対応するため都度finalizeを試みる(1節)。
                maybe_finalize(batch_id, now, providers, config, notification_service)

    mark_dispatch_completed(batch_id, now)
    maybe_finalize(batch_id, now, providers, config, notification_service)

    logger.info(
        "watchlist dispatcher completed batch_id=%s dispatched=%d universe=%d "
        "staged_rollout_excluded=%d holding_excluded=%d watchlist_excluded=%d",
        batch_id,
        total,
        collector_result.universe_count,
        collector_result.staged_rollout_excluded_count,
        collector_result.holding_excluded_count,
        collector_result.watchlist_excluded_count,
    )
    return {"dispatched": total, "batch_id": batch_id}
