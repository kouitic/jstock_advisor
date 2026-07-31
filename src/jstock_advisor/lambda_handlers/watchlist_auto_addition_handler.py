"""ウォッチリスト自動追加Lambda(週次、schedule: 毎週土曜07:00 JST)。

CandidateUniverseProvider→WatchlistCandidateCollector(除外)→銘柄ごとfan-out→
ScreeningDataProvider取得→WatchlistScreeningService評価→RankingEntry記録→
最後のワーカーがtry_acquire_finalizeでfinalize権限を取得→ランキング・上限適用→
WatchlistRepository.add_if_new→LINE通知→AuditLog(バッチ集計)、という流れで動作する。

既存のbuy_candidates_handler.py/holdings_watchlist_handler.pyと同じdispatch/worker
fan-outパターン(_fanout.py, batch_tracker.py)を再利用する。本機能はWatchlistRepository
への永続データ更新を伴うため、既存2ハンドラにはないfinalize専用の排他制御
(batch_tracker.try_acquire_finalize)を追加している(実装プラン第3回レビュー対応)。

カテゴリ分類(categorize_exclusion_reasons)とAuditLog記録(watchlist_screening_audit)は
CLI(services/watchlist_screening_service経由の単一プロセス実行)と共通の関数を使う。

dispatch時刻(started_at)は各ワーカーへのdispatchペイロードに含めて伝搬する
(finalizeを担当することになったワーカーがduration_seconds算出に使うため)。
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import uuid
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.enums import WatchlistRegistrationSource
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.domain.signals.watchlist_screening import (
    RankingEntry,
    categorize_exclusion_reasons,
    describe_matched_criteria,
)
from jstock_advisor.infrastructure.aws.batch_tracker import (
    MAX_RANKING_ENTRIES,
    BatchProgress,
    mark_finalize_complete,
    mark_finalize_failed,
    record_result,
    start_batch,
    try_acquire_finalize,
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
from jstock_advisor.lambda_handlers._fanout import dispatch_async, resolve_function_name
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.provider_factory import (
    build_candidate_universe_provider,
    build_real_provider_bundle,
)
from jstock_advisor.services.screening_data_provider import (
    ScreeningDataStatus,
    StockSnapshotScreeningDataProvider,
)
from jstock_advisor.services.watchlist_candidate_collector import WatchlistCandidateCollector
from jstock_advisor.services.watchlist_screening_audit import (
    REPOSITORY_RESULT_ADDED,
    REPOSITORY_RESULT_FAILED,
    REPOSITORY_RESULT_SKIPPED_EXISTING,
    REPOSITORY_RESULT_SKIPPED_OVER_LIMIT,
    record_batch_audit,
    record_candidate_audit,
    record_repository_result_audit,
)
from jstock_advisor.services.watchlist_screening_service import (
    WatchlistScreeningResult,
    WatchlistScreeningService,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _process_single_candidate(
    stock_code: str,
    batch_id: str,
    now: dt.datetime,
    providers: ProviderBundle,
    config: AppConfig,
) -> tuple[str, BatchProgress | None]:
    """1銘柄を評価しrecord_resultへ記録する。戻り値は(category, 記録直後の進捗)。"""
    screening_data_provider = StockSnapshotScreeningDataProvider(providers, config)
    screening_data = screening_data_provider.get_screening_input(stock_code, now)

    if screening_data.status != ScreeningDataStatus.OK or screening_data.input is None:
        logger.info(
            "watchlist screening data unavailable stock_code=%s status=%s error=%s",
            stock_code,
            screening_data.status,
            screening_data.error_message,
        )
        record_candidate_audit(stock_code, None, "DATA_INSUFFICIENT", now, batch_id=batch_id)
        progress = record_result(batch_id, "data_insufficient", stock_code=stock_code)
        return "data_insufficient", progress

    screening_service = WatchlistScreeningService(config)
    result = screening_service.evaluate(
        stock_code, screening_data.input.stock_name, screening_data.input, now
    )
    category, evaluation_result = categorize_exclusion_reasons(result.exclusion_reasons)

    ranking_entry_json = None
    if category == "passed":
        entry = screening_service.to_ranking_entry(result)
        if entry is None:
            # MAX_RANKING_ENTRY_BYTESを超過し、main_metricsを空にしても収まらない
            # (v1の単一Policyでは実質発生しないが、将来の複数Policy化への安全策)。
            # ランキングへ算入できないため"passed"ではなく処理失敗として扱う。
            logger.error(
                "watchlist ranking entry exceeds size limit even after trimming stock_code=%s",
                stock_code,
            )
            category = "failed"
            evaluation_result = "PASSED_RANKING_ENTRY_TOO_LARGE"
        else:
            ranking_entry_json = entry.model_dump_json()

    record_candidate_audit(stock_code, result, evaluation_result, now, batch_id=batch_id)

    progress = record_result(
        batch_id, category, stock_code=stock_code, ranking_entry=ranking_entry_json
    )
    return category, progress


def _fetch_stock_name(providers: ProviderBundle, stock_code: str) -> str | None:
    try:
        summary = providers.financial_data.get_financial_summary(stock_code)
    except Exception:  # noqa: BLE001 - 通知用の銘柄名取得は失敗してもcodeで表示すればよい
        logger.exception("stock name lookup failed stock_code=%s", stock_code)
        return None
    return summary.stock_name if summary is not None else None


def _finalize(
    progress: BatchProgress,
    batch_id: str,
    started_at: dt.datetime,
    now: dt.datetime,
    providers: ProviderBundle,
    config: AppConfig,
    notification_service: LineNotificationService,
) -> None:
    entries = [RankingEntry.model_validate_json(raw) for raw in progress.ranking_entries]
    limit = config.watchlist_screening.max_watchlist_additions_per_run
    all_ranked = WatchlistScreeningService.rank(entries)
    ranked = all_ranked[:limit]
    over_limit = all_ranked[limit:]
    registration_source = WatchlistRegistrationSource.AUTO_SCREENING.value
    registration_policy = config.watchlist_screening.screening_policy

    repository = WatchlistRepository()
    added_items: list[WatchlistItem] = []
    results_by_code: dict[str, WatchlistScreeningResult] = {}
    concurrent_duplicate_count = 0
    repository_failure_count = 0

    for rank, entry in enumerate(ranked, start=1):
        stock_name = _fetch_stock_name(providers, entry.stock_code)
        item = WatchlistItem(
            stock_code=entry.stock_code,
            stock_name=stock_name,
            reason=describe_matched_criteria(entry.matched_criteria),
            registration_source=WatchlistRegistrationSource.AUTO_SCREENING,
            registration_policy=registration_policy,
            created_at=now,
            updated_at=now,
        )
        try:
            added = repository.add_if_new(item)
        except Exception as exc:  # noqa: BLE001 - 1銘柄のRepository書き込み失敗で全体を止めない
            logger.exception("watchlist add_if_new failed stock_code=%s", entry.stock_code)
            repository_failure_count += 1
            record_repository_result_audit(
                batch_id,
                entry.stock_code,
                stock_name,
                rank,
                entry.total_score,
                REPOSITORY_RESULT_FAILED,
                False,
                registration_source,
                registration_policy,
                now,
                error=exc,
            )
            continue
        if not added:
            concurrent_duplicate_count += 1
            record_repository_result_audit(
                batch_id,
                entry.stock_code,
                stock_name,
                rank,
                entry.total_score,
                REPOSITORY_RESULT_SKIPPED_EXISTING,
                False,
                registration_source,
                registration_policy,
                now,
            )
            continue
        added_items.append(item)
        results_by_code[item.stock_code] = WatchlistScreeningResult(
            stock_code=entry.stock_code,
            stock_name=stock_name,
            passed=True,
            policy_results=[],
            total_score=entry.total_score,
            matched_criteria=entry.matched_criteria,
            exclusion_reasons=[],
            missing_required_fields=[],
            missing_scoring_fields=[],
            evaluated_at=now,
            main_metrics=entry.main_metrics,
        )
        record_repository_result_audit(
            batch_id,
            entry.stock_code,
            stock_name,
            rank,
            entry.total_score,
            REPOSITORY_RESULT_ADDED,
            True,
            registration_source,
            registration_policy,
            now,
        )

    # 上限外の合格銘柄も、後から追跡できるようskipped_over_limitとして記録する
    # (stock_nameは追加のfinancial_data呼び出しが必要になるため、追加もされず
    # 通知もされない銘柄のために取得はしない)。
    for rank, entry in enumerate(over_limit, start=len(ranked) + 1):
        record_repository_result_audit(
            batch_id,
            entry.stock_code,
            None,
            rank,
            entry.total_score,
            REPOSITORY_RESULT_SKIPPED_OVER_LIMIT,
            False,
            registration_source,
            registration_policy,
            now,
        )

    notification_sent = False
    notification_failure = False
    if added_items and config.watchlist_screening.notification_enabled:
        try:
            notification_sent = notification_service.notify_watchlist_additions(
                added_items,
                results_by_code,
                config.watchlist_screening.screening_policy,
                now,
            )
        except Exception:  # noqa: BLE001 - 通知失敗はバッチ失敗にしない(ベストエフォート)
            logger.exception("watchlist_auto_addition notification failed batch_id=%s", batch_id)
            notification_failure = True

    record_batch_audit(
        execution_mode="scheduled",
        universe_provider=config.watchlist_screening.candidate_universe.provider,
        screening_policies=[config.watchlist_screening.screening_policy],
        output_values={
            "started_at": started_at.isoformat(),
            "completed_at": now.isoformat(),
            "duration_seconds": (now - started_at).total_seconds(),
            "evaluation_target_count": progress.total,
            "data_success_count": progress.total - progress.category_counts["data_insufficient"],
            "data_failure_count": progress.category_counts["data_insufficient"],
            "required_condition_failed_count": (
                progress.category_counts["required_condition_failed"]
            ),
            "score_failed_count": progress.category_counts["score_failed"],
            "passed_count": progress.category_counts["passed"],
            "addition_limit": limit,
            "addition_candidate_count": len(ranked),
            "actual_added_count": len(added_items),
            "concurrent_duplicate_count": concurrent_duplicate_count,
            "repository_failure_count": repository_failure_count,
            "notification_sent": notification_sent,
            "notification_failure": notification_failure,
        },
        now=now,
        batch_id=batch_id,
    )
    logger.info(
        "watchlist_auto_addition finalized batch_id=%s added=%d candidates=%d",
        batch_id,
        len(added_items),
        len(ranked),
    )


def _build_notification_service(config: AppConfig) -> LineNotificationService:
    return LineNotificationService(
        line_client=build_line_client_from_env(),
        notification_log_repository=NotificationLogRepository(),
        recommendation_repository=RecommendationRepository(),
        config=config,
    )


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    watchlist_config = config.watchlist_screening

    task = event.get("task")
    if task == "screen_candidate":
        providers = build_real_provider_bundle(now, config)
        stock_code = event["stock_code"]
        batch_id = event["batch_id"]
        started_at = dt.datetime.fromisoformat(event["started_at"])

        try:
            category, progress = _process_single_candidate(
                stock_code, batch_id, now, providers, config
            )
        except Exception:  # noqa: BLE001 - 1銘柄の想定外エラーで再帰呼び出し全体を落とさない
            logger.exception(
                "watchlist candidate evaluation failed unexpectedly stock_code=%s", stock_code
            )
            progress = record_result(batch_id, "failed", stock_code=stock_code)
            category = "failed"

        if progress is not None and progress.is_complete and try_acquire_finalize(batch_id):
            try:
                notification_service = _build_notification_service(config)
                _finalize(
                    progress, batch_id, started_at, now, providers, config, notification_service
                )
            except Exception as exc:  # noqa: BLE001 - 失敗を記録してから再送出する
                logger.exception("watchlist_auto_addition finalize failed batch_id=%s", batch_id)
                mark_finalize_failed(batch_id, str(exc))
                raise
            else:
                mark_finalize_complete(batch_id)

        return {"stock_code": stock_code, "category": category}

    # 通常のスケジュール起動(ディスパッチのみ行い、銘柄ごとの実処理は非同期の自己再帰
    # 呼び出しに委ねる。全銘柄を直列処理するとLambdaの最大タイムアウト(900秒)を
    # 超えうるため、既存のbuy_candidates_handler.py/holdings_watchlist_handler.pyと
    # 同じdispatch/workerパターンを踏襲する)。
    if not (watchlist_config.enabled and watchlist_config.weekly_schedule_enabled):
        logger.info(
            "watchlist_auto_addition skipped enabled=%s weekly_schedule_enabled=%s",
            watchlist_config.enabled,
            watchlist_config.weekly_schedule_enabled,
        )
        return {"skipped": True}

    providers = build_real_provider_bundle(now, config)
    universe_provider = build_candidate_universe_provider(config)
    screening_data_provider = StockSnapshotScreeningDataProvider(providers, config)
    collector = WatchlistCandidateCollector(universe_provider, screening_data_provider)

    try:
        collector_result = collector.collect_target_codes()
    except CandidateUniverseError:
        logger.exception("watchlist candidate universe load failed")
        record_batch_audit(
            execution_mode="scheduled",
            universe_provider=watchlist_config.candidate_universe.provider,
            screening_policies=[watchlist_config.screening_policy],
            output_values={"execution_result": "universe_load_failed"},
            now=now,
        )
        return {"error": "universe_load_failed"}

    total = len(collector_result.stock_codes)
    batch_id = f"watchlist-auto-addition-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"

    # 事前ガード(レビュー対応): 評価対象銘柄数がMAX_RANKING_ENTRIESを超える場合、
    # 全銘柄が合格した場合にranking_entries(DynamoDB文字列セット)の書き込み上限に
    # 達する恐れがあるため、dispatch前にバッチを中止する(既存buy_candidates_handler.py
    # のMAX_SECTOR_ENTRIESガードと同じ考え方)。LINE通知は送らない。
    if total > MAX_RANKING_ENTRIES:
        logger.error(
            "watchlist_auto_addition: evaluation_target_count=%d exceeds "
            "MAX_RANKING_ENTRIES=%d; aborting before dispatch batch_id=%s",
            total,
            MAX_RANKING_ENTRIES,
            batch_id,
        )
        record_batch_audit(
            execution_mode="scheduled",
            universe_provider=watchlist_config.candidate_universe.provider,
            screening_policies=[watchlist_config.screening_policy],
            output_values={
                "execution_result": "ranking_capacity_exceeded",
                "evaluation_target_count": total,
                "max_ranking_entries": MAX_RANKING_ENTRIES,
            },
            now=now,
            batch_id=batch_id,
        )
        return {"error": "ranking_capacity_exceeded"}

    if total == 0:
        logger.info("watchlist_auto_addition: no candidates to evaluate batch_id=%s", batch_id)
        record_batch_audit(
            execution_mode="scheduled",
            universe_provider=watchlist_config.candidate_universe.provider,
            screening_policies=[watchlist_config.screening_policy],
            output_values={
                "started_at": now.isoformat(),
                "completed_at": now.isoformat(),
                "duration_seconds": 0.0,
                "universe_count": collector_result.universe_count,
                "deduplicated_count": collector_result.duplicate_count,
                "invalid_code_count": collector_result.invalid_code_count,
                "holding_excluded_count": collector_result.holding_excluded_count,
                "watchlist_excluded_count": collector_result.watchlist_excluded_count,
                "evaluation_target_count": 0,
                "actual_added_count": 0,
                "notification_sent": False,
                "notification_failure": False,
            },
            now=now,
            batch_id=batch_id,
        )
        return {"dispatched": 0}

    start_batch(batch_id, total, now)
    function_name = resolve_function_name(context, os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""))
    for stock_code in collector_result.stock_codes:
        dispatch_async(
            function_name,
            {
                "task": "screen_candidate",
                "stock_code": stock_code,
                "batch_id": batch_id,
                "started_at": now.isoformat(),
            },
        )

    logger.info(
        "watchlist_auto_addition dispatched: candidates=%d batch_id=%s "
        "universe=%d duplicate=%d invalid=%d holding_excluded=%d watchlist_excluded=%d",
        total,
        batch_id,
        collector_result.universe_count,
        collector_result.duplicate_count,
        collector_result.invalid_code_count,
        collector_result.holding_excluded_count,
        collector_result.watchlist_excluded_count,
    )
    return {"dispatched": total}
