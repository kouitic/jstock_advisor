"""ウォッチリスト自動追加(候補ユニバース本格対応)のBatch Reconciler Lambda(2/17節)。

EventBridge毎時トリガー。`batch_processing_timeout_hours`を超えて`DISPATCHING`/
`RUNNING`のまま放置されたバッチのタイムアウト検知・終端確定、および
`TIMEOUT_FINALIZING`/`TIMEOUT_FINALIZE_FAILED`バッチの途中再開を行う。

処理方針(2節):
- `DISPATCHING`でタイムアウト: `DISPATCH_FAILED`へ(候補リスト自体が未確定のため
  finalize系の処理は一切行わない)。
- `RUNNING`: まず`try_finalize_if_ready`(`maybe_finalize`経由)で救済を試みる
  (実際には全件完了しているが、最後の完了主体のfinalize呼び出し自体が
  クラッシュ等で失敗していたケースを、タイムアウト扱いにする前に正規の完了として
  救済する)。救済できずタイムアウトしていれば`TIMEOUT_FINALIZING`へ。
- `TIMEOUT_FINALIZING`/`TIMEOUT_FINALIZE_FAILED`: 17節の再計算方式(案C)で
  `completed`を補正しながら、未完了行を件数上限まで`FAILED`確定する。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.infrastructure.aws.batch_tracker import (
    WatchlistBatchStatus,
    get_watchlist_batch,
    list_watchlist_batches_by_status,
    mark_dispatch_failed,
    mark_finalizing_stuck_as_failed,
    run_timeout_finalization_pass,
    set_timeout_finalize_completed_count,
    transition_timeout_finalizing_to_failed,
    transition_timeout_finalizing_to_timed_out,
    try_acquire_timeout_finalization,
)
from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.provider_factory import build_real_provider_bundle
from jstock_advisor.services.watchlist_batch_finalizer import (
    compute_batch_metrics,
    maybe_finalize,
    retry_finalize,
)
from jstock_advisor.services.watchlist_data_cache import build_cached_provider_bundle
from jstock_advisor.services.watchlist_screening_audit import record_batch_audit

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_RECONCILE_TARGET_STATUSES = [
    WatchlistBatchStatus.DISPATCHING,
    WatchlistBatchStatus.RUNNING,
    # 運用ハードニング第2弾2節: finalize処理中の4段階すべてをスタック検知の対象に
    # する(旧FINALIZING単一状態を細分化)。
    WatchlistBatchStatus.FINALIZE_PREPARING,
    WatchlistBatchStatus.WATCHLIST_WRITE_COMPLETED,
    WatchlistBatchStatus.NOTIFICATION_PENDING,
    WatchlistBatchStatus.NOTIFICATION_SENT,
    WatchlistBatchStatus.FINALIZE_FAILED,
    WatchlistBatchStatus.TIMEOUT_FINALIZING,
    WatchlistBatchStatus.TIMEOUT_FINALIZE_FAILED,
]

# 運用ハードニング第2弾2節: finalize処理中とみなす状態一覧(スタック検知の対象)。
_FINALIZE_IN_PROGRESS_STATUSES = frozenset(
    {
        WatchlistBatchStatus.FINALIZE_PREPARING.value,
        WatchlistBatchStatus.WATCHLIST_WRITE_COMPLETED.value,
        WatchlistBatchStatus.NOTIFICATION_PENDING.value,
        WatchlistBatchStatus.NOTIFICATION_SENT.value,
    }
)


def _build_notification_service(config: AppConfig) -> LineNotificationService:
    return LineNotificationService(
        line_client=build_line_client_from_env(),
        notification_log_repository=NotificationLogRepository(),
        recommendation_repository=RecommendationRepository(),
        config=config,
    )


def _is_timed_out(batch_item: dict[str, Any], timeout_hours: int, now: dt.datetime) -> bool:
    started_at_raw = batch_item.get("started_at")
    if not started_at_raw:
        return False
    started_at = dt.datetime.fromisoformat(started_at_raw)
    return (now - started_at).total_seconds() / 3600 > timeout_hours


def _process_timeout_finalizing(
    batch_id: str,
    now: dt.datetime,
    max_rows_per_run: int,
    config: AppConfig,
) -> None:
    """17節ステップ1〜10。"""
    result = run_timeout_finalization_pass(batch_id, now, max_rows_per_run)
    if not set_timeout_finalize_completed_count(batch_id, result.terminal_count, now):
        # 他のReconciler実行/主体との競合でstatusが既に変わっていた(冪等スキップ)。
        return

    if result.terminal_count > result.total:
        logger.error(
            "watchlist reconciler: terminal_count exceeds total (data inconsistency) "
            "batch_id=%s terminal_count=%d total=%d",
            batch_id,
            result.terminal_count,
            result.total,
        )
        transition_timeout_finalizing_to_failed(
            batch_id, now, f"terminal_count({result.terminal_count}) > total({result.total})"
        )
        return

    if result.terminal_count < result.total:
        logger.info(
            "watchlist reconciler: timeout finalization in progress batch_id=%s "
            "terminal_count=%d total=%d newly_failed=%d",
            batch_id,
            result.terminal_count,
            result.total,
            result.newly_failed_count,
        )
        return

    # terminal_count == total: 14節「TIMED_OUT時は部分結果を自動登録・通知しない」。
    batch_item = get_watchlist_batch(batch_id) or {}
    started_at_raw = batch_item.get("started_at")
    started_at = dt.datetime.fromisoformat(started_at_raw) if started_at_raw else now
    metrics = compute_batch_metrics(result.all_records)
    completion_rate = (metrics["processed_count"] / result.total) if result.total else 0.0

    record_batch_audit(
        execution_mode="scheduled",
        universe_provider=config.watchlist_screening.candidate_universe.provider,
        screening_policies=[config.watchlist_screening.screening_policy],
        output_values={
            "execution_result": "TIMED_OUT",
            "started_at": started_at.isoformat(),
            "completed_at": now.isoformat(),
            "duration_seconds": (now - started_at).total_seconds(),
            "evaluation_completed_count": metrics["processed_count"],
            "evaluation_total_count": result.total,
            "completion_rate": completion_rate,
            **metrics,
        },
        now=now,
        batch_id=batch_id,
    )
    transition_timeout_finalizing_to_timed_out(batch_id, now)
    logger.warning(
        "watchlist reconciler: batch timed out batch_id=%s completion_rate=%.1f%%",
        batch_id,
        completion_rate * 100,
    )


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    wc = config.watchlist_screening
    providers: ProviderBundle = build_cached_provider_bundle(
        build_real_provider_bundle(now, config), config, now
    )
    notification_service = _build_notification_service(config)

    candidates = list_watchlist_batches_by_status(_RECONCILE_TARGET_STATUSES)

    dispatch_failed = 0
    rescued = 0
    finalizing_marked_stuck = 0
    finalize_retried = 0
    finalize_retry_exhausted = 0
    to_process_timeout: list[str] = []

    for batch_item in candidates:
        batch_id = batch_item["batch_id"]
        status = batch_item["status"]

        if status == WatchlistBatchStatus.DISPATCHING.value:
            timed_out = _is_timed_out(batch_item, wc.batch_processing_timeout_hours, now)
            if timed_out and mark_dispatch_failed(batch_id, now):
                dispatch_failed += 1
                logger.warning("watchlist reconciler: DISPATCH_FAILED batch_id=%s", batch_id)
            continue

        if status == WatchlistBatchStatus.RUNNING.value:
            if maybe_finalize(batch_id, now, providers, config, notification_service):
                rescued += 1
                continue
            if not _is_timed_out(batch_item, wc.batch_processing_timeout_hours, now):
                continue
            if try_acquire_timeout_finalization(batch_id):
                to_process_timeout.append(batch_id)
            continue

        if status in _FINALIZE_IN_PROGRESS_STATUSES:
            # 通常のRUNNING→FINALIZE_PREPARING遷移後、finalize処理中の4段階
            # (FINALIZE_PREPARING/WATCHLIST_WRITE_COMPLETED/NOTIFICATION_PENDING/
            # NOTIFICATION_SENT)のいずれかでLambdaが異常終了して二度と進まなく
            # なったケース(運用ハードニング5節・第2弾2節)。閾値未満なら正常に
            # 進行中の可能性があるため何もしない。
            if mark_finalizing_stuck_as_failed(
                batch_id, now, wc.finalizing_stuck_threshold_minutes
            ):
                finalizing_marked_stuck += 1
                logger.warning(
                    "watchlist reconciler: finalize stuck (status=%s), marked "
                    "FINALIZE_FAILED batch_id=%s",
                    status,
                    batch_id,
                )
            continue

        if status == WatchlistBatchStatus.FINALIZE_FAILED.value:
            attempt_count = int(batch_item.get("finalize_attempt_count", 0) or 0)
            if attempt_count >= wc.max_finalize_retry_attempts:
                finalize_retry_exhausted += 1
                logger.warning(
                    "watchlist reconciler: FINALIZE_FAILED retry attempts exhausted "
                    "batch_id=%s attempt_count=%d (manual intervention required, see CLI)",
                    batch_id,
                    attempt_count,
                )
                continue
            try:
                if retry_finalize(batch_id, now, providers, config, notification_service):
                    finalize_retried += 1
            except Exception:  # noqa: BLE001 - 1バッチの想定外エラーで他バッチの処理を止めない
                logger.exception(
                    "watchlist reconciler: retry_finalize unexpected error batch_id=%s", batch_id
                )
            continue

        if status == WatchlistBatchStatus.TIMEOUT_FINALIZE_FAILED.value:
            if try_acquire_timeout_finalization(batch_id):
                to_process_timeout.append(batch_id)
            continue

        # status == TIMEOUT_FINALIZING(前回Reconciler実行からの継続)。
        to_process_timeout.append(batch_id)

    timeout_processed = 0
    for batch_id in to_process_timeout:
        try:
            _process_timeout_finalizing(batch_id, now, wc.max_timeout_finalize_rows_per_run, config)
            timeout_processed += 1
        except Exception as exc:  # noqa: BLE001 - 1バッチの想定外エラーで他バッチの処理を止めない
            logger.exception("watchlist reconciler: unexpected error batch_id=%s", batch_id)
            transition_timeout_finalizing_to_failed(batch_id, now, str(exc))

    logger.info(
        "watchlist reconciler completed: candidates=%d dispatch_failed=%d rescued=%d "
        "finalizing_marked_stuck=%d finalize_retried=%d finalize_retry_exhausted=%d "
        "timeout_processed=%d",
        len(candidates),
        dispatch_failed,
        rescued,
        finalizing_marked_stuck,
        finalize_retried,
        finalize_retry_exhausted,
        timeout_processed,
    )
    return {
        "candidates": len(candidates),
        "dispatch_failed": dispatch_failed,
        "rescued": rescued,
        "finalizing_marked_stuck": finalizing_marked_stuck,
        "finalize_retried": finalize_retried,
        "finalize_retry_exhausted": finalize_retry_exhausted,
        "timeout_processed": timeout_processed,
    }
