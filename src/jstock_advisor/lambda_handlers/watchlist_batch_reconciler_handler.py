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
- `NOTIFICATION_FAILED`(運用ハードニング第3弾1節): LINE送信のみが例外で
  失敗した状態。`notification_failure_count`が上限未満なら`retry_notification`で
  通知のみを再試行する(ウォッチリスト書込みは再実行されない)。
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.enums import ExecutionMode
from jstock_advisor.infrastructure.aws.batch_tracker import (
    BatchFamily,
    UnknownWatchlistJobTypeError,
    WatchlistBatchStatus,
    WatchlistJobType,
    get_completion_batch,
    get_watchlist_batch,
    list_stale_maintenance_triggers,
    list_watchlist_batches_by_status,
    mark_dispatch_failed,
    mark_finalizing_stuck_as_failed,
    resolve_watchlist_job_type,
    run_timeout_finalization_pass,
    set_timeout_finalize_completed_count,
    transition_timeout_finalizing_to_failed,
    transition_timeout_finalizing_to_timed_out,
    try_acquire_timeout_finalization,
)
from jstock_advisor.infrastructure.aws.watchlist_rotation_dispatch_lease import (
    release_rotation_dispatch_lease,
)
from jstock_advisor.infrastructure.aws.watchlist_rotation_state import DEFAULT_ROTATION_ID
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
from jstock_advisor.lambda_handlers._fanout import dispatch_async
from jstock_advisor.lambda_handlers._finalize_recovery import build_finalize_only_payload
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.provider_factory import build_real_provider_bundle
from jstock_advisor.services.watchlist_batch_finalizer import (
    MAINTENANCE_UNIVERSE_PROVIDER,
    MaintenanceTriggerOutcome,
    compute_batch_metrics,
    maybe_finalize,
    maybe_finalize_maintenance,
    maybe_trigger_maintenance,
    retry_finalize,
    retry_notification,
)
from jstock_advisor.services.watchlist_data_cache import build_cached_provider_bundle
from jstock_advisor.services.watchlist_screening_audit import record_batch_audit

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Medium修正(2026-08再レビュー・再々レビュー): maintenance_trigger_retriedへ
# 計上してよいのは「実際にLambda invoke()を試行した」ケースのみ(handler()内で
# 使用)。CONFIGURATION_ERROR(環境変数未設定によりinvoke()呼び出し自体に
# 到達しない)はここに含めない(invoke未試行のため、別カウンタで区別する)。
_MAINTENANCE_RETRY_ATTEMPTED_OUTCOMES = frozenset(
    {
        MaintenanceTriggerOutcome.TRIGGERED,
        MaintenanceTriggerOutcome.INVOKE_FAILED,
    }
)

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
    # 運用ハードニング第3弾1節: 通知送信のみが例外で失敗した状態
    # (finalize全体はFINALIZE_FAILEDにならない、通知のみ再試行する)。
    WatchlistBatchStatus.NOTIFICATION_FAILED,
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
        # LINE通知dedupの原子化(Issue #17): NORMAL実行の送信決定を原子的に
        # 一意化するclaimリポジトリ(VALIDATION/DRY_RUNでは使用されない)。
        notification_claim_repository=NotificationClaimRepository(),
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

    # Issue #56: maintenance batchもRUNNING救済に失敗すればここへ到達する。
    # ADD用のcandidate_universe.providerで記録すると、メンテナンス実行が
    # 「新規追加バッチのタイムアウト」として監査に残り意味が食い違う。
    # (TIMED_OUTは14節どおり部分結果の登録・通知を行わないため誤りは監査の
    #  意味論に限られるが、job_typeに追随させる。)
    universe_provider = (
        MAINTENANCE_UNIVERSE_PROVIDER
        if batch_item.get("job_type") == WatchlistJobType.WATCHLIST_MAINTENANCE.value
        else config.watchlist_screening.candidate_universe.provider
    )

    record_batch_audit(
        execution_mode="scheduled",
        universe_provider=universe_provider,
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
    # 本番検証2026-08対応: TIMED_OUTは_finish_batch()/_maybe_commit_rotation()の
    # finalize経路を使わないため(モジュールdocstring参照)、rotation dispatch
    # leaseはここで明示的に解放する(未解放のままだとlease_expires_atの自然
    # 失効まで次のNEW_CANDIDATE_SCREENING dispatchがブロックされ続ける)。
    release_rotation_dispatch_lease(DEFAULT_ROTATION_ID, batch_id)
    logger.warning(
        "watchlist reconciler: batch timed out batch_id=%s completion_rate=%.1f%%",
        batch_id,
        completion_rate * 100,
    )


_COMPLETION_RECOVERY_FUNCTION_ENV = {
    BatchFamily.BUY_CANDIDATES: "BUY_CANDIDATES_FUNCTION_NAME",
    BatchFamily.HOLDINGS_WATCHLIST: "HOLDINGS_WATCHLIST_FUNCTION_NAME",
}


def _handle_completion_recovery_candidate(
    batch_item: dict[str, Any], now: dt.datetime
) -> bool | None:
    """buy/holdingsのfinalize recovery候補を処理する(Issue #57 Phase B2)。

    戻り値:
      None  … このバッチはbuy/holdings familyではない(=呼び出し側は
              既存のwatchlist経路をそのまま続行する)
      True  … finalize-only invokeを発行した
      False … buy/holdings familyだが今回は何もしなかった

    **marker不在は None を返す。** 既存のwatchlist batchには`batch_family`が
    無いため、marker不在を一律skipするとwatchlist recoveryを壊す。
    一方、**未知のfamily値はfail-close**(False)とし、既存経路へは流さない。

    reconcilerは**gateを取得しない**。`try_acquire_completion_finalize()`は
    invoke先のhandlerだけが実行する(invokeに失敗したときにgateを占有して
    しまわないため。取得回数も消費しない)。
    """
    raw_family = batch_item.get("batch_family")
    if raw_family is None:
        return None
    batch_id = batch_item["batch_id"]
    try:
        family = BatchFamily(raw_family)
    except ValueError:
        logger.error(
            "watchlist reconciler: unknown batch_family=%r batch_id=%s "
            "(fail-close: neither completion recovery nor watchlist path)",
            raw_family,
            batch_id,
        )
        return False

    record = get_completion_batch(batch_id)
    if record is None:
        logger.warning(
            "watchlist reconciler: completion batch record unavailable batch_id=%s", batch_id
        )
        return False
    if record.is_finalized:
        return False
    if not record.progress.is_complete:
        # 全銘柄の処理が終わっていない=通常進行中。recoveryの対象ではない。
        return False
    if record.execution_context is None or record.execution_context.mode != ExecutionMode.NORMAL:
        # VALIDATIONおよびcontext不明はfail-close(自動re-driveしない)。
        return False
    if record.attempts_exhausted:
        logger.error(
            "watchlist reconciler: finalize recovery exhausted batch_id=%s "
            "batch_family=%s attempt_count=%d reason=FINALIZE_RETRY_EXHAUSTED",
            batch_id,
            family.value,
            record.attempt_count,
        )
        return False

    function_name = os.environ.get(_COMPLETION_RECOVERY_FUNCTION_ENV[family], "")
    if not function_name:
        logger.error(
            "watchlist reconciler: completion recovery target function not configured "
            "batch_id=%s batch_family=%s",
            batch_id,
            family.value,
        )
        return False
    try:
        dispatch_async(function_name, build_finalize_only_payload(record))
    except Exception:  # noqa: BLE001 - 1バッチのinvoke失敗で他バッチの処理を止めない
        # invoke自体の失敗ではgateを取得していないため、attempt_countも増えない。
        # 次回の毎時実行で再試行できる(invoke試行回数とgate取得回数は別物)。
        logger.exception(
            "watchlist reconciler: finalize recovery invoke failed batch_id=%s batch_family=%s",
            batch_id,
            family.value,
        )
        return False
    logger.info(
        "watchlist reconciler: finalize recovery invoked batch_id=%s batch_family=%s "
        "attempt_count=%d",
        batch_id,
        family.value,
        record.attempt_count,
    )
    return True


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
    notification_retried = 0
    notification_retry_exhausted = 0
    completion_recovery_invoked = 0
    completion_recovery_skipped = 0
    to_process_timeout: list[str] = []

    for batch_item in candidates:
        batch_id = batch_item["batch_id"]
        status = batch_item["status"]

        # Issue #57 Phase B2: buy/holdingsのfinalize recovery。
        # `list_watchlist_batches_by_status()`はstatusだけで絞るfull scanのため、
        # buy/holdingsの項目(status=RUNNING固定)もここへ届く。従来はstarted_at
        # 不在によりタイムアウト判定が成立せず無害にskipされていたが、B2からは
        # **family markerで積極識別して専用分岐へ隔離**する。
        # **watchlistの既存status分岐へ流してはならない**(#56と同型の
        # 「種別を確認せず既定経路へ流す」誤終端を再生産しないため)。
        family_outcome = _handle_completion_recovery_candidate(batch_item, now)
        if family_outcome is not None:
            if family_outcome:
                completion_recovery_invoked += 1
            else:
                completion_recovery_skipped += 1
            continue

        if status == WatchlistBatchStatus.DISPATCHING.value:
            timed_out = _is_timed_out(batch_item, wc.batch_processing_timeout_hours, now)
            if timed_out and mark_dispatch_failed(batch_id, now):
                dispatch_failed += 1
                # 本番検証2026-08対応: Dispatcher Lambdaが候補選択・SQS投入の
                # 途中で異常終了しDISPATCHINGのまま放置された場合、rotation
                # dispatch leaseはfinalize経路(_maybe_commit_rotation)に到達
                # しないため明示的に解放する。
                release_rotation_dispatch_lease(DEFAULT_ROTATION_ID, batch_id)
                logger.warning("watchlist reconciler: DISPATCH_FAILED batch_id=%s", batch_id)
            continue

        if status == WatchlistBatchStatus.RUNNING.value:
            # Issue #56: 全件完了しているのにfinalize呼び出し自体が失敗した
            # ケースの救済。job_typeを見ずに常にADD用finalizerを呼ぶと、
            # WATCHLIST_MAINTENANCEバッチがメンテナンス業務(自動削除・
            # 連続非該当カウント更新・監視スコア更新)を一切実行しないまま
            # COMPLETED(終端)になり、二度と実行されない。
            try:
                job_type = resolve_watchlist_job_type(
                    batch_item.get("job_type"),
                    default=WatchlistJobType.NEW_CANDIDATE_SCREENING,
                )
            except UnknownWatchlistJobTypeError:
                # 未知値は暗黙にどちらかへ倒さずfail-closeする(救済しない)。
                # タイムアウト経路は後続のReconciler実行が引き続き担う。
                logger.error(
                    "watchlist reconciler: unknown job_type=%r batch_id=%s (rescue skipped)",
                    batch_item.get("job_type"),
                    batch_id,
                )
                continue
            rescued_now = (
                maybe_finalize_maintenance(batch_id, now, config)
                if job_type is WatchlistJobType.WATCHLIST_MAINTENANCE
                else maybe_finalize(batch_id, now, providers, config, notification_service)
            )
            if rescued_now:
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

        if status == WatchlistBatchStatus.NOTIFICATION_FAILED.value:
            # 運用ハードニング第3弾1節: LINE送信のみが例外で失敗した状態。
            # finalize全体(ウォッチリスト追加結果)は既に確定・保持されているため、
            # 通知のみを再試行する(finalize_attempt_countとは独立した
            # notification_failure_countで上限を判定する)。
            notification_attempt_count = int(batch_item.get("notification_failure_count", 0) or 0)
            if notification_attempt_count >= wc.max_notification_retry_attempts:
                notification_retry_exhausted += 1
                logger.warning(
                    "watchlist reconciler: NOTIFICATION_FAILED retry attempts exhausted "
                    "batch_id=%s notification_failure_count=%d "
                    "(should already be COMPLETED_WITH_NOTIFICATION_FAILURE; manual "
                    "intervention required if not)",
                    batch_id,
                    notification_attempt_count,
                )
                continue
            try:
                if retry_notification(batch_id, now, providers, config, notification_service):
                    notification_retried += 1
            except Exception:  # noqa: BLE001 - 1バッチの想定外エラーで他バッチの処理を止めない
                logger.exception(
                    "watchlist reconciler: retry_notification unexpected error batch_id=%s",
                    batch_id,
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

    # 平日毎日起動化(2026-08)対応・Medium修正(2026-08再レビュー): WATCHLIST_
    # MAINTENANCE後続起動のinvoke失敗等でmaintenance_trigger_status=TRIGGERING
    # のままlease失効した親バッチを再試行する(maybe_trigger_maintenanceの
    # ConditionExpressionがlease失効時のみ再取得を許すため、初回呼び出しと
    # 同じ関数を再度呼ぶだけでよい)。対象はCOMPLETED等の終端状態のバッチのため、
    # _RECONCILE_TARGET_STATUSESとは別のスキャンで拾う。
    #
    # maintenance_trigger_retriedは、戻り値(MaintenanceTriggerOutcome)を見て
    # 「実際にLambda invoke()を試行した(=TRIGGERED/INVOKE_FAILED)」ケースの
    # みを数える。CONFIGURATION_ERROR(lease再取得には成功したが、環境変数
    # 未設定によりinvoke()呼び出し自体に到達しない設定不備)はinvoke未試行
    # のため、retriedには含めずmaintenance_trigger_retry_configuration_error
    # へ個別に計上する(再々レビュー修正: 従来はCONFIGURATION_ERRORもretried
    # に含めていたが、「実際にinvokeを試行した件数」という定義と矛盾していた)。
    # 他主体が先にleaseを再取得済みだった場合(SKIPPED_LEASE_UNAVAILABLE)や
    # そもそも対象外だった場合(NOT_APPLICABLE、ABORTED等)は「再試行を試みて
    # すらいない」ため、誤解を招かないようretriedへは加算しない
    # (maintenance_trigger_retry_skippedへ計上する)。新規の永続DynamoDB
    # カウンタは追加せず、このReconciler実行1回分のin-memory集計のみで、
    # ログ・戻り値(運用監視・Issue #8観測用)に残す。
    maintenance_trigger_retried = 0
    maintenance_trigger_retry_failed = 0
    maintenance_trigger_retry_skipped = 0
    maintenance_trigger_retry_configuration_error = 0
    for batch_item in list_stale_maintenance_triggers(now):
        batch_id = batch_item["batch_id"]
        try:
            final_status = WatchlistBatchStatus(batch_item.get("status", ""))
            outcome = maybe_trigger_maintenance(batch_id, batch_item, now, config, final_status)
            if outcome in _MAINTENANCE_RETRY_ATTEMPTED_OUTCOMES:
                maintenance_trigger_retried += 1
                if outcome is MaintenanceTriggerOutcome.INVOKE_FAILED:
                    maintenance_trigger_retry_failed += 1
            elif outcome is MaintenanceTriggerOutcome.CONFIGURATION_ERROR:
                maintenance_trigger_retry_configuration_error += 1
            else:
                maintenance_trigger_retry_skipped += 1
        except Exception:  # noqa: BLE001 - 1バッチの想定外エラーで他バッチの処理を止めない
            logger.exception(
                "watchlist reconciler: maintenance trigger retry unexpected error batch_id=%s",
                batch_id,
            )

    logger.info(
        "watchlist reconciler completed: candidates=%d dispatch_failed=%d rescued=%d "
        "finalizing_marked_stuck=%d finalize_retried=%d finalize_retry_exhausted=%d "
        "notification_retried=%d notification_retry_exhausted=%d timeout_processed=%d "
        "maintenance_trigger_retried=%d maintenance_trigger_retry_failed=%d "
        "maintenance_trigger_retry_skipped=%d maintenance_trigger_retry_configuration_error=%d "
        "completion_recovery_invoked=%d completion_recovery_skipped=%d",
        len(candidates),
        dispatch_failed,
        rescued,
        finalizing_marked_stuck,
        finalize_retried,
        finalize_retry_exhausted,
        notification_retried,
        notification_retry_exhausted,
        timeout_processed,
        maintenance_trigger_retried,
        maintenance_trigger_retry_failed,
        maintenance_trigger_retry_skipped,
        maintenance_trigger_retry_configuration_error,
        completion_recovery_invoked,
        completion_recovery_skipped,
    )
    return {
        "candidates": len(candidates),
        "dispatch_failed": dispatch_failed,
        "rescued": rescued,
        "finalizing_marked_stuck": finalizing_marked_stuck,
        "finalize_retried": finalize_retried,
        "finalize_retry_exhausted": finalize_retry_exhausted,
        "notification_retried": notification_retried,
        "notification_retry_exhausted": notification_retry_exhausted,
        "timeout_processed": timeout_processed,
        "maintenance_trigger_retried": maintenance_trigger_retried,
        "completion_recovery_invoked": completion_recovery_invoked,
        "completion_recovery_skipped": completion_recovery_skipped,
        "maintenance_trigger_retry_failed": maintenance_trigger_retry_failed,
        "maintenance_trigger_retry_skipped": maintenance_trigger_retry_skipped,
        "maintenance_trigger_retry_configuration_error": (
            maintenance_trigger_retry_configuration_error
        ),
    }
