"""ウォッチリスト自動追加(候補ユニバース本格対応)のfinalize共通処理(11節)。

`batch_tracker.try_finalize_if_ready(batch_id)`がRUNNING→FINALIZINGへの排他遷移に
成功した実行だけが、`maybe_finalize()`経由で実際のfinalize処理(合格銘柄の
ランキング・WatchlistRepository書き込み・LINE通知・AuditLog記録)を行う。
Worker/Dispatcher/Terminal Failure Handler/Reconcilerのすべてが、それぞれの
完了確定の直後にこの関数を呼ぶ(dispatch/watchlist_dispatcher_handler.py等)。

`TIMED_OUT`(17節)はこのモジュールのfinalize経路を使わない(判定条件・処理内容が
異なる別経路のため、lambda_handlers/watchlist_batch_reconciler_handler.pyが
`compute_batch_metrics()`のみを再利用して独自に処理する)。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.enums import WatchlistRegistrationSource
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.domain.signals.watchlist_screening import (
    RankingEntry,
    describe_matched_criteria,
)
from jstock_advisor.infrastructure.aws.batch_tracker import (
    EVALUATION_RESULT_BATCH_TIMED_OUT,
    EVALUATION_RESULT_DISPATCH_SEND_FAILED,
    EVALUATION_RESULT_SQS_MAX_RECEIVE_EXCEEDED,
    EXECUTION_RESULT_HIGH_THROTTLE_RATE,
    EXECUTION_RESULT_NORMAL,
    EXECUTION_RESULT_PROVIDER_DATA_QUALITY_DEGRADED,
    CandidateProgressRecord,
    WatchlistProgressStatus,
    get_watchlist_batch,
    mark_watchlist_batch_completed,
    mark_watchlist_finalize_failed,
    query_all_candidate_progress,
    try_finalize_if_ready,
    try_retry_finalize,
)
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
)
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.watchlist_screening_audit import (
    REPOSITORY_RESULT_ADDED,
    REPOSITORY_RESULT_FAILED,
    REPOSITORY_RESULT_SKIPPED_EXISTING,
    REPOSITORY_RESULT_SKIPPED_OVER_LIMIT,
    record_batch_audit,
    record_repository_result_audit,
)
from jstock_advisor.services.watchlist_screening_service import (
    WatchlistScreeningResult,
    WatchlistScreeningService,
)

logger = logging.getLogger(__name__)

# 4節で1銘柄あたり8〜15件のHTTP通信が発生すると確認した範囲の中央値概算(15節)。
_ESTIMATED_YAHOO_FINANCE_REQUESTS_PER_STOCK = 11

_TERMINAL_FAILURE_REASONS = frozenset(
    {
        EVALUATION_RESULT_DISPATCH_SEND_FAILED,
        EVALUATION_RESULT_SQS_MAX_RECEIVE_EXCEEDED,
        EVALUATION_RESULT_BATCH_TIMED_OUT,
    }
)


def _percentile(sorted_values: list[int], pct: float) -> int | None:
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, int(len(sorted_values) * pct))
    return sorted_values[idx]


def compute_batch_metrics(records: list[CandidateProgressRecord]) -> dict[str, Any]:
    """15/19節: 段階導入の実測・全件有効化判定に使うバッチメトリクスを集計する。

    p50/p95等はtotal_processing_duration_ms(実処理時間の累積、SQS再配信待ち・
    リース待ち時間を含まない)を基準とする(19節)。

    運用ハードニング3節: is_provider_failure_suspected(429/403/5xx/タイムアウト/
    接続切断/yfinance固有例外等、旧is_rate_limit_suspectedを一般化)と、
    missing_field_names(欠損したスコア項目名)の項目別欠損率(field_coverage_rate、
    観測された項目名についてのみ「1-欠損率」を計算する。一度も欠損しなかった
    項目はこの辞書に現れない=実質100%)を追加集計する。
    """
    terminal_statuses = (
        WatchlistProgressStatus.COMPLETED.value,
        WatchlistProgressStatus.FAILED.value,
    )
    terminal = [r for r in records if r.status in terminal_statuses]
    durations = sorted(r.total_processing_duration_ms for r in terminal)
    processed = len(terminal)

    provider_failure = sum(1 for r in terminal if r.is_provider_failure_suspected)
    data_error = sum(1 for r in terminal if r.evaluation_result == "DATA_INSUFFICIENT")
    redelivery = sum(1 for r in terminal if r.attempt_count > 1)
    terminal_failure = sum(1 for r in terminal if r.evaluation_result in _TERMINAL_FAILURE_REASONS)
    total_duration_ms = sum(durations)

    missing_field_counts: dict[str, int] = {}
    for record in terminal:
        for field_name in record.missing_field_names:
            missing_field_counts[field_name] = missing_field_counts.get(field_name, 0) + 1
    field_coverage_rate = {
        field_name: 1.0 - (count / processed if processed else 0.0)
        for field_name, count in missing_field_counts.items()
    }
    worst_field_missing_rate_pct = (
        (max(missing_field_counts.values()) / processed * 100)
        if processed and missing_field_counts
        else 0.0
    )

    return {
        "processed_count": processed,
        "avg_processing_duration_ms": (total_duration_ms / processed) if processed else None,
        "p50_processing_duration_ms": _percentile(durations, 0.50),
        "p95_processing_duration_ms": _percentile(durations, 0.95),
        "provider_failure_suspected_count": provider_failure,
        "provider_failure_suspected_rate_pct": (
            (provider_failure / processed * 100) if processed else 0.0
        ),
        "data_error_count": data_error,
        "data_error_rate_pct": (data_error / processed * 100) if processed else 0.0,
        "sqs_redelivery_count": redelivery,
        "terminal_failure_count": terminal_failure,
        "terminal_failure_rate_pct": (terminal_failure / processed * 100) if processed else 0.0,
        "field_coverage_rate": field_coverage_rate,
        "worst_field_missing_rate_pct": worst_field_missing_rate_pct,
        "estimated_lambda_total_duration_ms": total_duration_ms,
        "estimated_yahoo_finance_requests": processed * _ESTIMATED_YAHOO_FINANCE_REQUESTS_PER_STOCK,
    }


def _fetch_stock_name(providers: ProviderBundle, stock_code: str) -> str | None:
    try:
        summary = providers.financial_data.get_financial_summary(stock_code)
    except Exception:  # noqa: BLE001 - 通知用の銘柄名取得は失敗してもcodeで表示すればよい
        logger.exception("stock name lookup failed stock_code=%s", stock_code)
        return None
    return summary.stock_name if summary is not None else None


def _add_passed_candidates_to_watchlist(
    batch_id: str,
    records: list[CandidateProgressRecord],
    config: AppConfig,
    providers: ProviderBundle,
    now: dt.datetime,
) -> tuple[list[WatchlistItem], dict[str, WatchlistScreeningResult]]:
    entries = [
        RankingEntry.model_validate_json(r.ranking_entry)
        for r in records
        if r.evaluation_result == "PASSED" and r.ranking_entry is not None
    ]
    limit = config.watchlist_screening.max_watchlist_additions_per_run
    all_ranked = WatchlistScreeningService.rank(entries)
    ranked = all_ranked[:limit]
    over_limit = all_ranked[limit:]
    registration_source = WatchlistRegistrationSource.AUTO_SCREENING.value
    registration_policy = config.watchlist_screening.screening_policy

    repository = WatchlistRepository()
    added_items: list[WatchlistItem] = []
    results_by_code: dict[str, WatchlistScreeningResult] = {}

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

    return added_items, results_by_code


def _finalize_completed(
    batch_id: str,
    now: dt.datetime,
    providers: ProviderBundle,
    config: AppConfig,
    notification_service: LineNotificationService,
) -> None:
    batch_item = get_watchlist_batch(batch_id) or {}
    started_at_raw = batch_item.get("started_at")
    started_at = dt.datetime.fromisoformat(started_at_raw) if started_at_raw else now

    records = query_all_candidate_progress(batch_id, consistent_read=True)
    metrics = compute_batch_metrics(records)
    processed = metrics["processed_count"]
    wc = config.watchlist_screening
    throttle_threshold = wc.high_throttle_rate_threshold_pct
    field_missing_threshold = wc.max_field_missing_rate_pct

    added_items: list[WatchlistItem] = []
    results_by_code: dict[str, WatchlistScreeningResult] = {}
    if processed > 0 and metrics["provider_failure_suspected_rate_pct"] > throttle_threshold:
        # 10節: ABORTED。ウォッチリスト追加・LINE通知は行わない。
        execution_result = EXECUTION_RESULT_HIGH_THROTTLE_RATE
    elif processed > 0 and metrics["worst_field_missing_rate_pct"] > field_missing_threshold:
        # 運用ハードニング3節: 429疑い率が閾値未満でも、主要スコア項目の欠損率が
        # 高い週(データ提供元障害の疑い)はABORTEDとし、部分結果を採用しない。
        execution_result = EXECUTION_RESULT_PROVIDER_DATA_QUALITY_DEGRADED
    else:
        execution_result = EXECUTION_RESULT_NORMAL
        added_items, results_by_code = _add_passed_candidates_to_watchlist(
            batch_id, records, config, providers, now
        )

    notification_sent = False
    notification_failure = False
    if (
        execution_result == EXECUTION_RESULT_NORMAL
        and added_items
        and config.watchlist_screening.notification_enabled
    ):
        try:
            notification_sent = notification_service.notify_watchlist_additions(
                added_items, results_by_code, config.watchlist_screening.screening_policy, now
            )
        except Exception:  # noqa: BLE001 - 通知失敗はバッチ失敗にしない(ベストエフォート)
            logger.exception("watchlist_screening notification failed batch_id=%s", batch_id)
            notification_failure = True

    record_batch_audit(
        execution_mode="scheduled",
        universe_provider=config.watchlist_screening.candidate_universe.provider,
        screening_policies=[config.watchlist_screening.screening_policy],
        output_values={
            "execution_result": execution_result,
            "started_at": started_at.isoformat(),
            "completed_at": now.isoformat(),
            "duration_seconds": (now - started_at).total_seconds(),
            "evaluation_target_count": len(records),
            "actual_added_count": len(added_items),
            "notification_sent": notification_sent,
            "notification_failure": notification_failure,
            # 運用ハードニング1節: 段階導入で実際に適用された設定値をfinalize時点の
            # 監査ログからも追跡できるようにする(Dispatcher側で記録済みの値を参照)。
            "staged_rollout_candidate_limit": batch_item.get("staged_rollout_candidate_limit"),
            "staged_rollout_market_segment_filter": batch_item.get(
                "staged_rollout_market_segment_filter"
            ),
            "universe_count": batch_item.get("universe_count"),
            "staged_rollout_excluded_count": batch_item.get("staged_rollout_excluded_count"),
            **metrics,
        },
        now=now,
        batch_id=batch_id,
    )
    mark_watchlist_batch_completed(batch_id, execution_result, now)
    logger.info(
        "watchlist_screening finalized batch_id=%s execution_result=%s added=%d",
        batch_id,
        execution_result,
        len(added_items),
    )


def maybe_finalize(
    batch_id: str,
    now: dt.datetime,
    providers: ProviderBundle,
    config: AppConfig,
    notification_service: LineNotificationService,
) -> bool:
    """11節: try_finalize_if_ready()がRUNNING→FINALIZINGへの遷移に成功した場合の
    みfinalize処理を実行してTrueを返す。条件不成立(まだ完了していない、または
    他の実行に競り負けた)の場合はFalseを返す(エラーではない)。
    """
    if not try_finalize_if_ready(batch_id, now):
        return False

    try:
        _finalize_completed(batch_id, now, providers, config, notification_service)
    except Exception as exc:  # noqa: BLE001 - 失敗を記録してから再送出する
        logger.exception("watchlist_screening finalize failed batch_id=%s", batch_id)
        mark_watchlist_finalize_failed(batch_id, now, str(exc))
        raise
    return True


def retry_finalize(
    batch_id: str,
    now: dt.datetime,
    providers: ProviderBundle,
    config: AppConfig,
    notification_service: LineNotificationService,
) -> bool:
    """運用ハードニング5節: FINALIZE_FAILED状態のバッチに対する再試行版
    `maybe_finalize`。`try_retry_finalize`(FINALIZE_FAILED→FINALIZING)に成功した
    場合のみ`_finalize_completed`を実行する。

    `_finalize_completed`は冪等な設計(WatchlistRepository.add_if_new()による
    重複追加防止、completedカウンタを一切更新しない、通知は「この実行で新規に
    追加された銘柄」のみを対象にする)であるため、途中まで進んでいた前回の
    実行結果を安全に引き継いで再実行できる。呼び出し元(Reconciler/CLI)は
    それぞれの方針で再試行回数を制御すること(本関数自体は無制限に呼び出し可能)。
    """
    if not try_retry_finalize(batch_id):
        return False

    try:
        _finalize_completed(batch_id, now, providers, config, notification_service)
    except Exception as exc:  # noqa: BLE001 - 失敗を記録してから再送出する
        logger.exception("watchlist_screening finalize retry failed batch_id=%s", batch_id)
        mark_watchlist_finalize_failed(batch_id, now, str(exc))
        raise
    return True
