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

ウォッチリスト自動運用の改善(2026-08)で、以下2点を追加した:

- 永続ラウンドロビン方式(計画Part A): job_type="NEW_CANDIDATE_SCREENING"
  (既定、event未指定時のデフォルト)の場合、`WatchlistScreeningRotationState`
  の永続カーソルを起点に候補を選択する(rotation.enabled=falseなら従来の
  固定スライス方式へフォールバック)。rotation cursorの実際の前進(commit)は
  ここでは行わず、`watchlist_batch_finalizer._finish_batch()`が業務処理の
  確定を確認した時点でのみ行う(計画Part A-5)。
- 既存Dispatcher/Worker/Queue/Reconciler基盤の共用(計画Part C-7案A):
  job_type="WATCHLIST_MAINTENANCE"(EventBridge Schedulerの追加Input経由で
  指定)の場合、候補ソースをJPXユニバースではなく`WatchlistRepository`の
  AUTO_SCREENING銘柄一覧に切り替える。この場合、段階導入(staged_rollout)・
  ALLOW_FULL_MARKET_SCREENINGゲート・ローテーションはいずれも適用しない
  (対象は既に登録済みの少数銘柄のため無関係)。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any

import boto3

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import WatchlistRegistrationSource
from jstock_advisor.infrastructure.aws.batch_tracker import (
    JOB_TYPE_NEW_CANDIDATE_SCREENING,
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
from jstock_advisor.infrastructure.aws.watchlist_rotation_dispatch_lease import (
    get_rotation_dispatch_lease_status,
    release_rotation_dispatch_lease,
    try_acquire_rotation_dispatch_lease,
)
from jstock_advisor.infrastructure.aws.watchlist_rotation_state import (
    DEFAULT_ROTATION_ID,
    create_rotation_state_if_absent,
)
from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
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
from jstock_advisor.services.screening_data_provider import build_screening_data_provider
from jstock_advisor.services.watchlist_batch_finalizer import (
    maybe_finalize,
    maybe_finalize_maintenance,
)
from jstock_advisor.services.watchlist_candidate_collector import (
    RotationCursor,
    WatchlistCandidateCollector,
)
from jstock_advisor.services.watchlist_data_cache import build_cached_provider_bundle
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


def _compute_universe_signature(eligible_universe_count: int, selected_codes: list[str]) -> str:
    """監査専用(rotation選択ロジックには使わない、計画Part A-6)。ユニバースの
    件数・今回選択された銘柄集合の変化を人間が後から確認するための短いハッシュ。
    """
    basis = f"{eligible_universe_count}:{','.join(sorted(selected_codes))}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _collect_new_candidate_targets(
    config: Any, now: dt.datetime
) -> tuple[list[str], dict[str, Any]]:
    """JOB_TYPE_NEW_CANDIDATE_SCREENING: JPXユニバースからローテーション選択する。

    戻り値は(対象銘柄コード一覧, set_watchlist_batch_totalへ渡す追加kwargs)。
    """
    wc = config.watchlist_screening
    cu = wc.candidate_universe

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

    providers = build_cached_provider_bundle(build_real_provider_bundle(now, config), config, now)
    universe_provider = build_candidate_universe_provider(config, now)
    screening_data_provider = build_screening_data_provider(providers, config)
    collector = WatchlistCandidateCollector(
        universe_provider,
        screening_data_provider,
        staged_rollout=wc.staged_rollout,
        rotation_enabled=wc.rotation.enabled,
    )

    rotation_cursor: RotationCursor | None = None
    rotation_state = None
    if wc.rotation.enabled:
        rotation_state = create_rotation_state_if_absent(now)
        if rotation_state.last_stock_code is not None:
            rotation_cursor = (
                rotation_state.last_market_segment or "",
                rotation_state.last_stock_code,
            )

    collector_result = collector.collect_target_codes(rotation_cursor=rotation_cursor)

    extra_kwargs: dict[str, Any] = {
        "staged_rollout_candidate_limit": wc.staged_rollout.candidate_limit,
        "staged_rollout_market_segment_filter": wc.staged_rollout.market_segment_filter,
        "universe_count": collector_result.universe_count,
        "staged_rollout_excluded_count": collector_result.staged_rollout_excluded_count,
        "eligible_universe_count": collector_result.eligible_universe_count,
        "rotation_cycle": rotation_state.cycle_number if rotation_state is not None else None,
        "rotation_start_key": (
            list(collector_result.rotation_cursor_before)
            if collector_result.rotation_cursor_before is not None
            else None
        ),
        "rotation_end_key": (
            list(collector_result.rotation_cursor_after)
            if collector_result.rotation_cursor_after is not None
            else None
        ),
        "rotation_wrapped": collector_result.rotation_wrapped,
        "universe_signature": _compute_universe_signature(
            collector_result.eligible_universe_count, collector_result.stock_codes
        ),
    }
    logger.info(
        "watchlist dispatcher: staged_rollout applied candidate_limit=%s "
        "market_segment_filter=%s universe_count=%d staged_rollout_excluded=%d "
        "eligible_universe_count=%d rotation_enabled=%s total=%d",
        wc.staged_rollout.candidate_limit,
        wc.staged_rollout.market_segment_filter,
        collector_result.universe_count,
        collector_result.staged_rollout_excluded_count,
        collector_result.eligible_universe_count,
        wc.rotation.enabled,
        len(collector_result.stock_codes),
    )
    return collector_result.stock_codes, extra_kwargs


def _collect_maintenance_targets() -> tuple[list[str], dict[str, Any]]:
    """JOB_TYPE_WATCHLIST_MAINTENANCE: 既存ウォッチリストのAUTO_SCREENING銘柄
    一覧を対象とする(計画Part C-7案A)。JPXユニバース・段階導入・ローテーション
    はいずれも無関係(対象は既に登録済みの少数銘柄のため)。
    """
    watchlist_repo = WatchlistRepository()
    codes = [
        item.stock_code
        for item in watchlist_repo.list_all()
        if item.registration_source == WatchlistRegistrationSource.AUTO_SCREENING
    ]
    return codes, {}


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    wc = config.watchlist_screening
    cu = wc.candidate_universe
    job_type = event.get("job_type", JOB_TYPE_NEW_CANDIDATE_SCREENING)

    if not (wc.enabled and wc.weekly_schedule_enabled):
        logger.info(
            "watchlist dispatcher skipped job_type=%s enabled=%s weekly_schedule_enabled=%s",
            job_type,
            wc.enabled,
            wc.weekly_schedule_enabled,
        )
        return {"skipped": True}

    # 運用ハードニング2節: candidate_limit=null(全件処理)は、運用者が明示的に
    # ALLOW_FULL_MARKET_SCREENING=trueを設定した場合のみ許可する。この時点では
    # dispatch leaseもBatchRunsTable行も未作成のため、SQS投入・LINE通知は発生しない
    # (universe_load_failed/no_candidatesと同じ「開始前に中止する」パターン)。
    # WATCHLIST_MAINTENANCEはJPXユニバースを使わないためこのゲートの対象外。
    if (
        job_type == JOB_TYPE_NEW_CANDIDATE_SCREENING
        and wc.staged_rollout.candidate_limit is None
        and os.environ.get("ALLOW_FULL_MARKET_SCREENING") != "true"
    ):
        logger.error(
            "watchlist dispatcher: full market screening blocked "
            "(candidate_limit=null but ALLOW_FULL_MARKET_SCREENING is not 'true')"
        )
        record_batch_audit(
            execution_mode="scheduled",
            universe_provider=cu.provider,
            screening_policies=[wc.screening_policy],
            output_values={"execution_result": "full_market_screening_blocked"},
            now=now,
        )
        return {"error": "full_market_screening_blocked"}

    batch_prefix = (
        "watchlist" if job_type == JOB_TYPE_NEW_CANDIDATE_SCREENING else "watchlist-maint"
    )
    batch_id = f"{batch_prefix}-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    owner_id = getattr(context, "aws_request_id", None) or uuid.uuid4().hex

    if not try_acquire_dispatch_lease(
        batch_id, owner_id, now, _DISPATCH_LEASE_SECONDS, wc.batch_record_ttl_hours
    ):
        # 通常のスケジュール実行では新規batch_idのため実質的に発生しないが、手動での
        # 同一batch_id再起動(18節「案B」)が既存の実行と競合した場合にここへ入る。
        logger.info("watchlist dispatcher: dispatch lease not acquired batch_id=%s", batch_id)
        return {"skipped": "lease_not_acquired"}

    # 本番検証2026-08で発覚した二重起動対応: rotation cursorのCAS(pointer_version
    # 楽観ロック)だけでは「同じrotation windowの二重選択・二重dispatch」自体は
    # 防げない(cursorはfinalize時にしか前進しないため、50秒差の2回のDispatcher
    # 起動が両方とも同じ未前進cursorを読める)。job_type=="NEW_CANDIDATE_SCREENING"
    # かつrotation.enabled=trueの場合のみ、実際の候補選択・dispatch前に専用lease
    # (watchlist_rotation_dispatch_lease.py)を取得し、同一windowの並行dispatchを
    # 排他する。WATCHLIST_MAINTENANCEやrotation.enabled=false(固定スライス
    # フォールバック)はこのleaseの対象外(候補選択がrotation windowに依存しない)。
    rotation_lease_held = job_type == JOB_TYPE_NEW_CANDIDATE_SCREENING and wc.rotation.enabled
    if rotation_lease_held:
        rotation_lease_seconds = int(wc.batch_processing_timeout_hours * 3600)
        if not try_acquire_rotation_dispatch_lease(
            DEFAULT_ROTATION_ID, batch_id, now, rotation_lease_seconds
        ):
            active_batch_id, lease_started_at, lease_expires_at = (
                get_rotation_dispatch_lease_status(DEFAULT_ROTATION_ID)
            )
            logger.info(
                "watchlist dispatcher: rotation dispatch lease unavailable, skipping "
                "attempted_batch_id=%s active_batch_id=%s lease_expires_at=%s",
                batch_id,
                active_batch_id,
                lease_expires_at,
            )
            record_batch_audit(
                execution_mode="scheduled",
                universe_provider=cu.provider,
                screening_policies=[wc.screening_policy],
                output_values={
                    "execution_result": "rotation_dispatch_already_in_progress",
                    "job_type": job_type,
                    "block_reason": "ROTATION_DISPATCH_ALREADY_IN_PROGRESS",
                    "attempted_batch_id": batch_id,
                    "active_batch_id": active_batch_id,
                    "rotation_id": DEFAULT_ROTATION_ID,
                    "lease_started_at": lease_started_at,
                    "lease_expires_at": lease_expires_at,
                },
                now=now,
                batch_id=batch_id,
            )
            return {"skipped": "rotation_dispatch_in_progress"}

    try:
        if job_type == JOB_TYPE_NEW_CANDIDATE_SCREENING:
            stock_codes, extra_kwargs = _collect_new_candidate_targets(config, now)
        else:
            stock_codes, extra_kwargs = _collect_maintenance_targets()
    except CandidateUniverseError:
        logger.exception("watchlist candidate universe load failed batch_id=%s", batch_id)
        if rotation_lease_held:
            release_rotation_dispatch_lease(DEFAULT_ROTATION_ID, batch_id)
        record_batch_audit(
            execution_mode="scheduled",
            universe_provider=cu.provider,
            screening_policies=[wc.screening_policy],
            output_values={"execution_result": "universe_load_failed"},
            now=now,
            batch_id=batch_id,
        )
        return {"error": "universe_load_failed"}

    total = len(stock_codes)
    if total == 0:
        logger.info(
            "watchlist dispatcher: no candidates to evaluate batch_id=%s job_type=%s",
            batch_id,
            job_type,
        )
        if rotation_lease_held:
            release_rotation_dispatch_lease(DEFAULT_ROTATION_ID, batch_id)
        record_batch_audit(
            execution_mode="scheduled",
            universe_provider=cu.provider,
            screening_policies=[wc.screening_policy],
            output_values={
                "execution_result": "no_candidates",
                "job_type": job_type,
                **extra_kwargs,
            },
            now=now,
            batch_id=batch_id,
        )
        return {"dispatched": 0}

    set_watchlist_batch_total(
        batch_id,
        total,
        wc.candidate_progress_ttl_hours,
        now,
        job_type=job_type,
        **extra_kwargs,
    )
    create_missing_candidate_progress_rows(
        batch_id, stock_codes, now, wc.candidate_progress_ttl_hours
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
        if rotation_lease_held:
            release_rotation_dispatch_lease(DEFAULT_ROTATION_ID, batch_id)
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
    providers = build_cached_provider_bundle(build_real_provider_bundle(now, config), config, now)
    notification_service = _build_notification_service(config)

    def _finalize(batch_id: str, now: dt.datetime) -> None:
        if job_type == JOB_TYPE_NEW_CANDIDATE_SCREENING:
            maybe_finalize(batch_id, now, providers, config, notification_service)
        else:
            maybe_finalize_maintenance(batch_id, now, config)

    to_dispatch = [row for row in progress_rows if not row.dispatched]
    for chunk in _chunked(to_dispatch, _SQS_SEND_BATCH_SIZE):
        entries = [
            {
                "Id": row.stock_code,
                "MessageBody": json.dumps(
                    {"batch_id": batch_id, "stock_code": row.stock_code, "job_type": job_type}
                ),
            }
            for row in chunk
        ]
        results = _send_batch_with_retry(sqs, queue_url, entries)
        for stock_code, success in results.items():
            if success:
                mark_candidate_dispatched(batch_id, stock_code, now)
            elif record_dispatch_send_failure(batch_id, stock_code, now):
                # この銘柄が最後の1件だった場合に対応するため都度finalizeを試みる(1節)。
                _finalize(batch_id, now)

    mark_dispatch_completed(batch_id, now)
    _finalize(batch_id, now)

    logger.info(
        "watchlist dispatcher completed batch_id=%s job_type=%s dispatched=%d",
        batch_id,
        job_type,
        total,
    )
    return {"dispatched": total, "batch_id": batch_id, "job_type": job_type}
