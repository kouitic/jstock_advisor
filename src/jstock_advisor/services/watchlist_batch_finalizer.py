"""ウォッチリスト自動追加(候補ユニバース本格対応)のfinalize共通処理(11節)。

`batch_tracker.try_finalize_if_ready(batch_id)`がRUNNING→FINALIZE_PREPARINGへの
排他遷移に成功した実行だけが、`maybe_finalize()`経由で実際のfinalize処理(合格
銘柄のランキング・WatchlistRepository書き込み・LINE通知・AuditLog記録)を行う。
Worker/Dispatcher/Terminal Failure Handler/Reconcilerのすべてが、それぞれの
完了確定の直後にこの関数を呼ぶ(dispatch/watchlist_dispatcher_handler.py等)。

運用ハードニング第2弾2節: finalize処理を4段階(FINALIZE_PREPARING→
WATCHLIST_WRITE_COMPLETED→NOTIFICATION_PENDING→NOTIFICATION_SENT→COMPLETED)へ
分割し、`_finalize_completed()`は**状態のstatus文字列ではなくBatchRunsTable項目の
フィールドの有無**を見て、どこまで完了しているかを判定して再開する。

- `finalize_target_stock_codes`/`finalize_ranking_json`が無ければ、abort判定
  (`_evaluate_abort_reasons`)を行い、非該当ならランキングを計算して永続化する
  (既に存在すれば再計算せずそのまま再利用する)。
- `finalize_target_stock_codes`のうち`repository_results`(銘柄コード→
  `REPOSITORY_RESULT_*`)に未登録の銘柄だけを`WatchlistRepository.add_if_new()`で
  処理する。結果は1銘柄処理するたびに即座に永続化する(`record_repository_result_item`)
  ため、この処理の途中でLambdaが異常終了しても、次回は未処理の銘柄のみを再処理する
  (`add_if_new`自体も条件付き書き込みのため、たとえ永続化自体が欠落しても実際の
  重複追加は発生しない、二重の安全策)。
- `finalize_notification_outcome`が無ければ、通知フェーズを解決する。追加0件なら
  `NOT_REQUIRED`、`notification_enabled=false`なら`SKIPPED`として即座に解決する。
  それ以外は`notify_watchlist_additions()`を試みる。**運用ハードニング第3弾1節**:
  この呼び出しが例外を送出した場合、Phase3は例外を自分自身で捕捉し
  `NOTIFICATION_FAILED`(通知失敗回数+1、エラー概要を保存)として記録したうえで
  **`_finalize_completed`自体は正常returnする**(finalize全体をFINALIZE_FAILEDに
  しない。ウォッチリスト追加結果は既にPhase2で確定・保持済みのため失われない)。
  Reconciler/CLIの`retry_notification()`が、通知失敗回数が上限
  (`max_notification_retry_attempts`)未満の間はNOTIFICATION_FAILED→
  NOTIFICATION_PENDINGへ戻して通知のみを再試行する(Phase1/2は
  finalize_target_stock_codes/repository_resultsが既に存在するため
  スキップされ、ウォッチリスト書込みは再実行されない)。上限に達した場合のみ
  `COMPLETED_WITH_NOTIFICATION_FAILURE`として終端する。送信成功時は
  `finalize_notified_stock_codes`へ実際に通知した銘柄一覧を永続化する。
  content hashは`batch_id`・`screening_policy`・バッチ開始日(`started_at`、
  再試行時の`now`ではない)から算出し、最初の解決試行時に永続化して以後の
  再試行では再計算せず再利用する(運用ハードニング第3弾4節: 日付をまたぐ
  再試行でも重複抑止が機能するようにするため)。「送信成功後・
  finalize_notified_stock_codes永続化前にLambdaが異常終了」した場合、再試行時に
  同じcontent_hashで再度`notify_watchlist_additions()`を呼ぶことになるが、
  同関数自体が持つ重複抑止(`NotificationLog`との突き合わせ)により実際の
  再送は抑止される(完全なexactly-onceではなく、この既存の重複抑止機構と
  組み合わせて実質的な重複防止を実現する設計)。
- 最後に`finalize_batch_audit_recorded`が未設定の場合のみ`record_batch_audit`を
  呼ぶ(batch audit重複防止の最適化)。安全性自体は`record_batch_audit`が
  決定的なaudit_id(`f"watchlist_batch_audit:{batch_id}"`)による
  `insert_if_absent`で担保するため、このフラグと実際の書き込みが競合・
  順序不整合を起こしても実際に重複記録されることはない(運用ハードニング
  第3弾3節)。

`TIMED_OUT`(17節)はこのモジュールのfinalize経路を使わない(判定条件・処理内容が
異なる別経路のため、lambda_handlers/watchlist_batch_reconciler_handler.pyが
`compute_batch_metrics()`のみを再利用して独自に処理する)。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Any

from pydantic import TypeAdapter

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.enums import WatchlistRegistrationSource
from jstock_advisor.domain.entities.watchlist import WatchlistItem, WatchlistRemovalHistory
from jstock_advisor.domain.ranking import RankingCalculator
from jstock_advisor.domain.signals.watchlist_screening import (
    RankingEntry,
    WatchlistScoreDetail,
    describe_matched_criteria,
)
from jstock_advisor.infrastructure.aws.batch_tracker import (
    EVALUATION_RESULT_BATCH_TIMED_OUT,
    EVALUATION_RESULT_DISPATCH_SEND_FAILED,
    EVALUATION_RESULT_SQS_MAX_RECEIVE_EXCEEDED,
    EXECUTION_RESULT_EXCESSIVE_DATA_ERRORS,
    EXECUTION_RESULT_EXCESSIVE_NOT_FOUND,
    EXECUTION_RESULT_EXCESSIVE_TERMINAL_FAILURES,
    EXECUTION_RESULT_HIGH_THROTTLE_RATE,
    EXECUTION_RESULT_NORMAL,
    EXECUTION_RESULT_REQUIRED_DATA_QUALITY_DEGRADED,
    EXECUTION_RESULT_SCORING_DATA_QUALITY_DEGRADED,
    JOB_TYPE_NEW_CANDIDATE_SCREENING,
    NOTIFICATION_OUTCOME_NOT_REQUIRED,
    NOTIFICATION_OUTCOME_SENT,
    NOTIFICATION_OUTCOME_SKIPPED,
    CandidateProgressRecord,
    WatchlistProgressStatus,
    get_watchlist_batch,
    mark_batch_audit_recorded,
    mark_watchlist_batch_completed,
    mark_watchlist_finalize_failed,
    mark_watchlist_write_completed,
    query_all_candidate_progress,
    record_finalize_target,
    record_notification_failed,
    record_notification_pending,
    record_notification_resolved,
    record_repository_result_item,
    try_finalize_if_ready,
    try_retry_finalize,
    try_retry_notification,
)
from jstock_advisor.infrastructure.aws.watchlist_rotation_dispatch_lease import (
    release_rotation_dispatch_lease,
)
from jstock_advisor.infrastructure.aws.watchlist_rotation_state import (
    DEFAULT_ROTATION_ID,
    get_rotation_state,
    get_rotation_state_consistent,
    try_commit_rotation_advance,
)
from jstock_advisor.infrastructure.local_repository.watchlist_removal_history_repository import (
    WatchlistRemovalHistoryRepository,
)
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
)
from jstock_advisor.services.line_notification_service import (
    LineNotificationService,
    compute_watchlist_addition_content_hash,
)
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.screening_data_provider import (
    REQUIRED_FIELD_NAMES,
    SCORING_FIELD_NAMES,
)
from jstock_advisor.services.watchlist_addition_summary_builder import (
    WatchlistAdditionSummary,
    build_watchlist_addition_summary,
)
from jstock_advisor.services.watchlist_display_name import (
    StockDisplayNameResolver,
    build_stock_display_name_resolver,
)
from jstock_advisor.services.watchlist_maintenance_service import (
    MaintenanceOutcome,
    MaintenanceScreeningSummary,
    evaluate_maintenance_decision,
)
from jstock_advisor.services.watchlist_screening_audit import (
    REPOSITORY_RESULT_ADDED,
    REPOSITORY_RESULT_FAILED,
    REPOSITORY_RESULT_SKIPPED_COOLDOWN,
    REPOSITORY_RESULT_SKIPPED_EXISTING,
    REPOSITORY_RESULT_SKIPPED_OVER_LIMIT,
    record_batch_audit,
    record_removal_audit,
    record_repository_result_audit,
    record_rotation_commit_audit,
)
from jstock_advisor.services.watchlist_screening_service import WatchlistScreeningService

_ranking_entry_list_adapter: TypeAdapter[list[RankingEntry]] = TypeAdapter(list[RankingEntry])

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


_ALL_KNOWN_FIELD_NAMES = REQUIRED_FIELD_NAMES + SCORING_FIELD_NAMES


def compute_batch_metrics(records: list[CandidateProgressRecord]) -> dict[str, Any]:
    """15/19節: 段階導入の実測・全件有効化判定に使うバッチメトリクスを集計する。

    p50/p95等はtotal_processing_duration_ms(実処理時間の累積、SQS再配信待ち・
    リース待ち時間を含まない)を基準とする(19節)。

    運用ハードニング第2弾3節: `ScreeningDataResult.input`が作られなかった行
    (DATA_ERROR/NOT_FOUND/terminal_failure/UNEXPECTED_ERROR)をfield_coverage_rate
    等の母数から除外する(screening_input_created_count)。DATA_ERROR/NOT_FOUNDは
    旧"DATA_INSUFFICIENT"を分離したもの(watchlist_worker_handler.py参照)。

    運用ハードニング第2弾4節: field_coverage_rateは既知の全フィールド名
    (REQUIRED_FIELD_NAMES+SCORING_FIELD_NAMES)を必ずキーとして持ち、一度も
    欠損しなかった項目は1.0を出力する。
    """
    terminal_statuses = (
        WatchlistProgressStatus.COMPLETED.value,
        WatchlistProgressStatus.FAILED.value,
    )
    terminal = [r for r in records if r.status in terminal_statuses]
    durations = sorted(r.total_processing_duration_ms for r in terminal)
    # 計画Part B-1: フェーズ別計測(Before/After比較用)。data_fetch_duration_msは
    # DATA_ERROR/NOT_FOUNDでもセットされうるが、scoring_duration_msはevaluate()が
    # 実行できた銘柄のみ(いずれもNoneの行は母数から除外する)。
    data_fetch_durations = sorted(
        r.data_fetch_duration_ms for r in terminal if r.data_fetch_duration_ms is not None
    )
    scoring_durations = sorted(
        r.scoring_duration_ms for r in terminal if r.scoring_duration_ms is not None
    )

    total_candidate_count = len(records)
    terminal_count = len(terminal)
    terminal_failure_count = sum(
        1 for r in terminal if r.evaluation_result in _TERMINAL_FAILURE_REASONS
    )
    evaluation_attempted_count = terminal_count - terminal_failure_count

    data_error_count = sum(1 for r in terminal if r.evaluation_result == "DATA_ERROR")
    not_found_count = sum(1 for r in terminal if r.evaluation_result == "NOT_FOUND")
    unexpected_error_count = sum(1 for r in terminal if r.evaluation_result == "UNEXPECTED_ERROR")
    provider_failure_count = sum(1 for r in terminal if r.is_provider_failure_suspected)

    provider_call_completed_count = (
        evaluation_attempted_count - provider_failure_count - unexpected_error_count
    )
    # ScreeningDataResult.inputが実際に作られた行数(missing_field_namesが意味を
    # 持つ行数)。field_coverage_rate等の母数はこれを使う(processed/terminal_count
    # ではない、運用ハードニング第2弾3節)。
    screening_input_created_count = (
        evaluation_attempted_count - data_error_count - not_found_count - unexpected_error_count
    )
    # 現行アーキテクチャでは「入力が作れた行は必ず評価まで完了する」ため恒等だが、
    # 将来入力作成後に評価自体が独立して失敗しうる変更が入った場合に分離できるよう、
    # 別名の指標として先に用意しておく。
    screening_completed_count = screening_input_created_count

    redelivery = sum(1 for r in terminal if r.attempt_count > 1)
    total_duration_ms = sum(durations)

    missing_field_counts: dict[str, int] = dict.fromkeys(_ALL_KNOWN_FIELD_NAMES, 0)
    for record in terminal:
        for field_name in record.missing_field_names:
            missing_field_counts[field_name] = missing_field_counts.get(field_name, 0) + 1
    denom = screening_input_created_count
    field_coverage_rate = {
        field_name: 1.0 - (count / denom if denom else 0.0)
        for field_name, count in missing_field_counts.items()
    }

    def _worst_rate_pct(field_names: tuple[str, ...]) -> float:
        if not denom:
            return 0.0
        counts = [missing_field_counts[name] for name in field_names]
        return (max(counts) / denom * 100) if counts else 0.0

    worst_required_field_missing_rate_pct = _worst_rate_pct(REQUIRED_FIELD_NAMES)
    worst_scoring_field_missing_rate_pct = _worst_rate_pct(SCORING_FIELD_NAMES)

    return {
        "total_candidate_count": total_candidate_count,
        "terminal_count": terminal_count,
        "evaluation_attempted_count": evaluation_attempted_count,
        "provider_call_completed_count": provider_call_completed_count,
        "screening_input_created_count": screening_input_created_count,
        "screening_completed_count": screening_completed_count,
        # 後方互換用(15/19節の既存利用箇所向け、processed_count=terminal_count)。
        "processed_count": terminal_count,
        "avg_processing_duration_ms": (
            (total_duration_ms / terminal_count) if terminal_count else None
        ),
        "p50_processing_duration_ms": _percentile(durations, 0.50),
        "p95_processing_duration_ms": _percentile(durations, 0.95),
        # 計画Part B-1: フェーズ別計測(Before/After比較用)。
        "avg_data_fetch_duration_ms": (
            (sum(data_fetch_durations) / len(data_fetch_durations))
            if data_fetch_durations
            else None
        ),
        "p50_data_fetch_duration_ms": _percentile(data_fetch_durations, 0.50),
        "p95_data_fetch_duration_ms": _percentile(data_fetch_durations, 0.95),
        "avg_scoring_duration_ms": (
            (sum(scoring_durations) / len(scoring_durations)) if scoring_durations else None
        ),
        "p50_scoring_duration_ms": _percentile(scoring_durations, 0.50),
        "p95_scoring_duration_ms": _percentile(scoring_durations, 0.95),
        "provider_failure_count": provider_failure_count,
        "provider_failure_rate_pct": (
            (provider_failure_count / evaluation_attempted_count * 100)
            if evaluation_attempted_count
            else 0.0
        ),
        "data_error_count": data_error_count,
        "data_error_rate_pct": (
            (data_error_count / evaluation_attempted_count * 100)
            if evaluation_attempted_count
            else 0.0
        ),
        "not_found_count": not_found_count,
        "not_found_rate_pct": (
            (not_found_count / evaluation_attempted_count * 100)
            if evaluation_attempted_count
            else 0.0
        ),
        # LINE通知品質改善(2026-08、修正⑦): data_unavailable_countの算出に
        # 必要なため返り値へ追加する(第7版まではこの値が返されず、
        # data_unavailable_countの算出式から漏れていたバグがあった)。
        "unexpected_error_count": unexpected_error_count,
        "sqs_redelivery_count": redelivery,
        "terminal_failure_count": terminal_failure_count,
        "terminal_failure_rate_pct": (
            (terminal_failure_count / total_candidate_count * 100) if total_candidate_count else 0.0
        ),
        "field_coverage_rate": field_coverage_rate,
        "worst_required_field_missing_rate_pct": worst_required_field_missing_rate_pct,
        "worst_scoring_field_missing_rate_pct": worst_scoring_field_missing_rate_pct,
        "estimated_lambda_total_duration_ms": total_duration_ms,
        "estimated_yahoo_finance_requests": (
            terminal_count * _ESTIMATED_YAHOO_FINANCE_REQUESTS_PER_STOCK
        ),
    }


def _fetch_stock_name(providers: ProviderBundle, stock_code: str) -> str | None:
    try:
        summary = providers.financial_data.get_financial_summary(stock_code)
    except Exception:  # noqa: BLE001 - 通知用の銘柄名取得は失敗してもcodeで表示すればよい
        logger.exception("stock name lookup failed stock_code=%s", stock_code)
        return None
    return summary.stock_name if summary is not None else None


def _fallback_name_provider(
    providers: ProviderBundle, stock_code: str
) -> Callable[[], str | None]:
    """StockDisplayNameResolver.resolve()のfallback_name_provider向けの遅延
    評価クロージャを作る。stock_codeを関数引数として束縛することで、
    ループ内でのlambda遅延束縛バグ(全クロージャが最後のループ変数を参照して
    しまう問題)を避ける。
    """
    return lambda: _fetch_stock_name(providers, stock_code)


def _compute_finalize_target(
    records: list[CandidateProgressRecord], config: AppConfig
) -> tuple[list[RankingEntry], list[RankingEntry]]:
    """PASSED行からランキングを計算し、(追加上限内, 上限外)を返す。

    `records`は既に全終端状態(finalize開始時点で確定済み)であるため、この関数は
    純粋にrecordsのみに依存する冪等な計算であり、再開時に何度呼び直しても同じ
    結果になる(運用ハードニング第2弾2節)。
    """
    entries = [
        RankingEntry.model_validate_json(r.ranking_entry)
        for r in records
        if r.evaluation_result == "PASSED" and r.ranking_entry is not None
    ]
    limit = config.watchlist_screening.max_watchlist_additions_per_run
    all_ranked = WatchlistScreeningService.rank(entries)
    return all_ranked[:limit], all_ranked[limit:]


def _record_over_limit_audit(
    batch_id: str,
    ranked: list[RankingEntry],
    over_limit: list[RankingEntry],
    wc: Any,
    now: dt.datetime,
) -> None:
    registration_source = WatchlistRegistrationSource.AUTO_SCREENING.value
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
            wc.screening_policy,
            now,
        )


def _build_watchlist_items_for_codes(
    stock_codes: list[str],
    ranked: list[RankingEntry],
    providers: ProviderBundle,
    resolver: StockDisplayNameResolver,
    now: dt.datetime,
    registration_policy: str,
) -> list[WatchlistItem]:
    """通知フェーズ再開時、`stock_codes`(追加成功済みだが未通知)について
    `WatchlistItem`を`ranked`(永続化済みのランキングJSONから復元)+
    日本語表示名解決から再構築する(stock_name自体はBatchRunsTableへ
    永続化しない、運用ハードニング第2弾2節)。

    stock_nameはStockDisplayNameResolver.resolve()で解決する(JPX/override/
    既存Watchlistで解決できれば`_fetch_stock_name`(外部Provider呼び出しを
    伴う重い処理)は呼ばれない、LINE通知品質改善2026-08 修正②)。
    """
    entries_by_code = {entry.stock_code: entry for entry in ranked}
    items: list[WatchlistItem] = []
    for stock_code in stock_codes:
        entry = entries_by_code.get(stock_code)
        if entry is None:
            logger.warning(
                "watchlist_screening finalize: stock_code=%s not in ranked entries, skipping "
                "notification rebuild",
                stock_code,
            )
            continue
        stock_name = resolver.resolve(
            stock_code,
            fallback_name_provider=_fallback_name_provider(providers, stock_code),
        )
        items.append(
            WatchlistItem(
                stock_code=stock_code,
                stock_name=stock_name,
                reason=describe_matched_criteria(entry.matched_criteria),
                registration_source=WatchlistRegistrationSource.AUTO_SCREENING,
                registration_policy=registration_policy,
                created_at=now,
                updated_at=now,
            )
        )
    return items


def _write_watchlist_additions(
    batch_id: str,
    pending_entries: list[RankingEntry],
    rank_by_code: dict[str, int],
    config: AppConfig,
    providers: ProviderBundle,
    resolver: StockDisplayNameResolver,
    now: dt.datetime,
) -> dict[str, str]:
    """WATCHLIST_WRITE_COMPLETEDフェーズ本体。`pending_entries`は
    `finalize_target_stock_codes`のうち、まだ`repository_results`に結果が
    永続化されていない銘柄のみ(呼び出し側が絞り込み済み、運用ハードニング
    第2弾2節)。1銘柄処理するたびに`record_repository_result_item`で即座に
    永続化するため、この関数の途中でLambdaが異常終了しても、次回は未処理の
    銘柄のみが`pending_entries`として渡される(`add_if_new()`自体も条件付き
    書き込みのため、たとえ永続化自体が欠落しても実際の重複追加は発生しない、
    二重の安全策)。戻り値は今回処理した銘柄分のみのdict(呼び出し側が
    既存のrepository_resultsへマージする)。

    計画Part C-4: add_if_new()を試みる前に、自動削除後の再追加クールダウン
    (`WatchlistRemovalHistory.cooldown_until`)が有効な銘柄は追加をスキップする
    (削除→即再追加→削除、という振動を防ぐ)。
    """
    registration_source = WatchlistRegistrationSource.AUTO_SCREENING.value
    registration_policy = config.watchlist_screening.screening_policy
    repository = WatchlistRepository()
    removal_history_repo = WatchlistRemovalHistoryRepository(
        config.watchlist_screening.auto_removal.readd_cooldown_days
    )

    newly_resolved: dict[str, str] = {}

    for entry in pending_entries:
        rank = rank_by_code[entry.stock_code]
        # LINE通知品質改善(2026-08 修正②): JPX/override/既存Watchlistで解決
        # できれば_fetch_stock_name(外部Provider呼び出しを伴う)は呼ばれない。
        stock_name = resolver.resolve(
            entry.stock_code,
            fallback_name_provider=_fallback_name_provider(providers, entry.stock_code),
        )
        if removal_history_repo.is_in_cooldown(entry.stock_code, now):
            newly_resolved[entry.stock_code] = REPOSITORY_RESULT_SKIPPED_COOLDOWN
            record_repository_result_item(
                batch_id, now, entry.stock_code, REPOSITORY_RESULT_SKIPPED_COOLDOWN
            )
            record_repository_result_audit(
                batch_id,
                entry.stock_code,
                stock_name,
                rank,
                entry.total_score,
                REPOSITORY_RESULT_SKIPPED_COOLDOWN,
                False,
                registration_source,
                registration_policy,
                now,
            )
            continue
        item = WatchlistItem(
            stock_code=entry.stock_code,
            stock_name=stock_name,
            reason=describe_matched_criteria(entry.matched_criteria),
            registration_source=WatchlistRegistrationSource.AUTO_SCREENING,
            registration_policy=registration_policy,
            registration_batch_id=batch_id,
            created_at=now,
            updated_at=now,
        )
        try:
            added = repository.add_if_new(item)
        except Exception as exc:  # noqa: BLE001 - 1銘柄のRepository書き込み失敗で全体を止めない
            logger.exception("watchlist add_if_new failed stock_code=%s", entry.stock_code)
            newly_resolved[entry.stock_code] = REPOSITORY_RESULT_FAILED
            record_repository_result_item(batch_id, now, entry.stock_code, REPOSITORY_RESULT_FAILED)
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
            # 運用ハードニング第3弾2節: add_if_new()==True後・
            # record_repository_result_item()永続化前に障害が起きた場合、再試行時
            # ここへ到達しadd_if_new()はFalseを返す。既存項目がこのバッチ自身の
            # AUTO_SCREENING追加(registration_batch_id一致)であれば、今回追加した
            # 事実を見失わずADDEDとして復元する(通知対象からの漏れを防ぐ)。
            # 別バッチの追加・手動登録・batch_id未設定の旧レコードは復元しない。
            existing = repository.get(entry.stock_code)
            if (
                existing is not None
                and existing.registration_source == WatchlistRegistrationSource.AUTO_SCREENING
                and existing.registration_batch_id == batch_id
            ):
                repository_result = REPOSITORY_RESULT_ADDED
                added_to_watchlist = True
            else:
                repository_result = REPOSITORY_RESULT_SKIPPED_EXISTING
                added_to_watchlist = False
            newly_resolved[entry.stock_code] = repository_result
            record_repository_result_item(batch_id, now, entry.stock_code, repository_result)
            record_repository_result_audit(
                batch_id,
                entry.stock_code,
                stock_name,
                rank,
                entry.total_score,
                repository_result,
                added_to_watchlist,
                registration_source,
                registration_policy,
                now,
            )
            continue
        newly_resolved[entry.stock_code] = REPOSITORY_RESULT_ADDED
        record_repository_result_item(batch_id, now, entry.stock_code, REPOSITORY_RESULT_ADDED)
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

    return newly_resolved


def _evaluate_abort_reasons(metrics: dict[str, Any], wc: Any) -> list[str]:
    """運用ハードニング第2弾5節: 未知の障害パターンでも安全に中止できる独立の
    安全弁。該当した理由を全て返す(複数該当しうる、監査ログのabort_reasonsへ
    そのまま保存する)。
    """
    reasons: list[str] = []
    if metrics["provider_failure_rate_pct"] > wc.high_throttle_rate_threshold_pct:
        # 10節: ABORTED。ウォッチリスト追加・LINE通知は行わない。
        reasons.append(EXECUTION_RESULT_HIGH_THROTTLE_RATE)
    if metrics["worst_scoring_field_missing_rate_pct"] > wc.max_scoring_field_missing_rate_pct:
        # 運用ハードニング3節: 429疑い率が閾値未満でも、主要スコア項目の欠損率が
        # 高い週(データ提供元障害の疑い)はABORTEDとし、部分結果を採用しない。
        reasons.append(EXECUTION_RESULT_SCORING_DATA_QUALITY_DEGRADED)
    if metrics["data_error_rate_pct"] > wc.max_data_error_rate_pct:
        reasons.append(EXECUTION_RESULT_EXCESSIVE_DATA_ERRORS)
    if metrics["not_found_rate_pct"] > wc.max_not_found_rate_pct:
        reasons.append(EXECUTION_RESULT_EXCESSIVE_NOT_FOUND)
    if metrics["terminal_failure_rate_pct"] > wc.max_terminal_failure_rate_pct:
        reasons.append(EXECUTION_RESULT_EXCESSIVE_TERMINAL_FAILURES)
    if metrics["worst_required_field_missing_rate_pct"] > wc.max_required_field_missing_rate_pct:
        reasons.append(EXECUTION_RESULT_REQUIRED_DATA_QUALITY_DEGRADED)
    return reasons


def _maybe_commit_rotation(
    batch_id: str,
    batch_item: dict[str, Any],
    records: list[CandidateProgressRecord],
    now: dt.datetime,
) -> None:
    """計画Part A-5: rotation commitは、この関数の呼び出し元(`_finish_batch`)
    に到達した時点(=業務処理がCOMPLETED/COMPLETED_WITH_NOTIFICATION_FAILURE/
    ABORTEDのいずれかとして確定した時点)でのみ行う。銘柄個別の評価失敗
    (poison stock)は`try_finalize_if_ready`の条件(`completed>=total`)を
    妨げないため、それだけではrotation commitをブロックしない(計画Part A-6)。
    一方、ランキング/ウォッチリスト書き込み自体が技術的に失敗した場合は
    `_finish_batch`へ到達せず(`mark_watchlist_finalize_failed`のみ)、
    rotationは進まない。

    計画Part A-9: job_type=="NEW_CANDIDATE_SCREENING"以外(WATCHLIST_
    MAINTENANCE)は専用の`_finalize_maintenance_completed`を経由するため
    構造的にこの関数へ到達しないが、防御的に明示チェックする。

    本番検証2026-08対応: この関数への到達(=業務処理の確定)は、rotation
    dispatch leaseを解放する唯一の正規経路でもある(watchlist_rotation_
    dispatch_lease.py)。rotation.enabled=falseでleaseがそもそも未取得の
    バッチに対しても安全な no-op として呼んでよい。leaseの解放とcursorの
    前進(pointer_version CAS)は別責務であり、どちらか一方を欠かすと
    「次回dispatchが永久にブロックされる」または「同じwindowを再度dispatch
    できてしまう」のいずれかにつながるため、両方を必ず行う。
    """
    job_type = batch_item.get("job_type", JOB_TYPE_NEW_CANDIDATE_SCREENING)
    if job_type != JOB_TYPE_NEW_CANDIDATE_SCREENING:
        return

    release_rotation_dispatch_lease(DEFAULT_ROTATION_ID, batch_id)

    rotation_end_key = batch_item.get("rotation_end_key")
    if not rotation_end_key:
        # rotation.enabled=falseで実行された場合等、rotation window自体が
        # 記録されていない(従来の固定スライス方式へのフォールバック)。
        return

    evaluation_result_counts: dict[str, int] = {}
    for record in records:
        key = record.evaluation_result or "UNKNOWN"
        evaluation_result_counts[key] = evaluation_result_counts.get(key, 0) + 1

    rotation_cycle = batch_item.get("rotation_cycle")
    rotation_start_key = batch_item.get("rotation_start_key")
    wrapped = bool(batch_item.get("rotation_wrapped", False))
    selected_count = int(batch_item.get("total", 0))

    state = get_rotation_state()
    committed = False
    observed_version: int | None = None
    if state is None:
        logger.warning(
            "watchlist rotation commit skipped: rotation state not initialized batch_id=%s",
            batch_id,
        )
    else:
        committed = try_commit_rotation_advance(
            state.pointer_version,
            rotation_end_key[0],
            rotation_end_key[1],
            wrapped,
            selected_count,
            now,
        )
        if not committed:
            conflict_state = get_rotation_state_consistent()
            observed_version = (
                conflict_state.pointer_version if conflict_state is not None else None
            )
            logger.warning(
                "watchlist rotation commit conflict batch_id=%s rotation_id=%s "
                "expected_version=%d observed_version=%s",
                batch_id,
                DEFAULT_ROTATION_ID,
                state.pointer_version,
                observed_version,
            )

    record_rotation_commit_audit(
        batch_id,
        rotation_cycle,
        rotation_start_key,
        rotation_end_key,
        wrapped,
        selected_count,
        evaluation_result_counts,
        committed,
        now,
        rotation_id=DEFAULT_ROTATION_ID,
        expected_version=(state.pointer_version if state is not None else None),
        observed_version=observed_version,
    )


def _finish_batch(
    batch_id: str,
    now: dt.datetime,
    started_at: dt.datetime,
    batch_item: dict[str, Any],
    records: list[CandidateProgressRecord],
    metrics: dict[str, Any],
    config: AppConfig,
    execution_result: str,
    abort_reasons: list[str],
    added_stock_codes: list[str],
    notification_sent: bool,
    notification_failure: bool,
    notification_permanently_failed: bool = False,
) -> None:
    """COMPLETED(またはCOMPLETED_WITH_NOTIFICATION_FAILURE)フェーズ。
    `finalize_batch_audit_recorded`が既に立っている場合は`record_batch_audit`を
    呼ばない(無駄な呼び出しを避ける最適化、運用ハードニング第2弾2節)。安全性
    自体は決定的なaudit_id(`f"watchlist_batch_audit:{batch_id}"`)による
    `insert_if_absent`が担保するため、このフラグの有無に関わらず実際に重複
    記録されることはない(運用ハードニング第3弾3節)。
    """
    if not batch_item.get("finalize_batch_audit_recorded"):
        record_batch_audit(
            execution_mode="scheduled",
            universe_provider=config.watchlist_screening.candidate_universe.provider,
            screening_policies=[config.watchlist_screening.screening_policy],
            output_values={
                "execution_result": execution_result,
                "abort_reasons": abort_reasons,
                "started_at": started_at.isoformat(),
                "completed_at": now.isoformat(),
                "duration_seconds": (now - started_at).total_seconds(),
                "evaluation_target_count": len(records),
                "actual_added_count": len(added_stock_codes),
                "notification_sent": notification_sent,
                "notification_failure": notification_failure,
                "notification_permanently_failed": notification_permanently_failed,
                # 運用ハードニング1節: 段階導入で実際に適用された設定値をfinalize
                # 時点の監査ログからも追跡できるようにする(Dispatcher側で記録済みの
                # 値を参照)。
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
            idempotency_key=f"watchlist_batch_audit:{batch_id}",
        )
        mark_batch_audit_recorded(batch_id, now)
    mark_watchlist_batch_completed(
        batch_id, execution_result, now, notification_permanently_failed
    )
    _maybe_commit_rotation(batch_id, batch_item, records, now)
    logger.info(
        "watchlist_screening finalized batch_id=%s execution_result=%s added=%d "
        "notification_permanently_failed=%s",
        batch_id,
        execution_result,
        len(added_stock_codes),
        notification_permanently_failed,
    )


def _finalize_completed(
    batch_id: str,
    now: dt.datetime,
    providers: ProviderBundle,
    config: AppConfig,
    notification_service: LineNotificationService,
) -> None:
    """運用ハードニング第2弾2節: 4段階(FINALIZE_PREPARING→
    WATCHLIST_WRITE_COMPLETED→NOTIFICATION_PENDING→NOTIFICATION_SENT→
    COMPLETED)の再開可能なfinalize処理。各`if`はBatchRunsTable項目の
    フィールドの有無で「そのフェーズが既に完了しているか」を判定する
    (statusの文字列そのものは分岐条件に使わない)。
    """
    batch_item = get_watchlist_batch(batch_id) or {}
    started_at_raw = batch_item.get("started_at")
    started_at = dt.datetime.fromisoformat(started_at_raw) if started_at_raw else now

    records = query_all_candidate_progress(batch_id, consistent_read=True)
    metrics = compute_batch_metrics(records)
    wc = config.watchlist_screening
    resolver = build_stock_display_name_resolver(
        wc.stock_display_name.jpx_name_negative_cache_ttl_seconds
    )
    # LINE通知品質改善(2026-08 修正⑦): total_score_by_codeはevaluate()実行済み
    # の全銘柄(passed以外も含む)、notification_detail_by_codeはpassed銘柄のみ。
    # いずれもrecordsから直接組み立てるため追加のDynamoDB読み取りは発生しない。
    total_score_by_code: dict[str, float] = {
        r.stock_code: r.total_score for r in records if r.total_score is not None
    }
    notification_detail_by_code: dict[str, WatchlistScoreDetail] = {
        r.stock_code: r.notification_detail for r in records if r.notification_detail is not None
    }
    # data_unavailable_countは4分類(NOT_FOUND/DATA_ERROR/UNEXPECTED_ERROR/
    # terminal_failure)の合算とし、引き算では算出しない。これら4分類は
    # evaluation_resultが単一文字列であるため相互排他的で二重計上は起きない。
    # total_target_count == ranked_count + data_unavailable_countが常に成立する
    # (不変条件)。
    data_unavailable_count = (
        metrics["not_found_count"]
        + metrics["data_error_count"]
        + metrics["unexpected_error_count"]
        + metrics["terminal_failure_count"]
    )
    ranked_count = metrics["screening_completed_count"]
    total_target_count = metrics["total_candidate_count"]

    # --- Phase 1: FINALIZE_PREPARING ---
    if "finalize_target_stock_codes" in batch_item:
        target_codes: list[str] = list(batch_item["finalize_target_stock_codes"])
        ranked = _ranking_entry_list_adapter.validate_json(batch_item["finalize_ranking_json"])
    else:
        abort_reasons = _evaluate_abort_reasons(metrics, wc)
        if abort_reasons:
            # 複数条件に同時該当する場合、execution_resultは"|"区切りの複合
            # 文字列になる(監査ログのabort_reasons配列には該当理由を全て
            # 個別に保存する)。ウォッチリスト追加・LINE通知は行わない。
            execution_result = "|".join(abort_reasons)
            _finish_batch(
                batch_id,
                now,
                started_at,
                batch_item,
                records,
                metrics,
                config,
                execution_result,
                abort_reasons,
                [],
                False,
                False,
            )
            return
        ranked, over_limit = _compute_finalize_target(records, config)
        target_codes = [entry.stock_code for entry in ranked]
        ranking_json = _ranking_entry_list_adapter.dump_json(ranked).decode("utf-8")
        record_finalize_target(batch_id, now, target_codes, ranking_json)
        _record_over_limit_audit(batch_id, ranked, over_limit, wc, now)
        batch_item["finalize_target_stock_codes"] = target_codes
        batch_item["finalize_ranking_json"] = ranking_json

    # --- Phase 2: WATCHLIST_WRITE_COMPLETED ---
    # 運用ハードニング第2弾2節: finalize_target_stock_codesのうちrepository_results
    # に未登録のものだけを処理する(1銘柄処理するたびに即座に永続化されるため、
    # 再開時は前回処理済みの銘柄をadd_if_newへ渡し直さない)。
    repository_results: dict[str, str] = dict(batch_item.get("repository_results", {}))
    pending_codes = [code for code in target_codes if code not in repository_results]
    if pending_codes:
        entries_by_code = {entry.stock_code: entry for entry in ranked}
        rank_by_code = {entry.stock_code: i for i, entry in enumerate(ranked, start=1)}
        pending_entries = [entries_by_code[code] for code in pending_codes]
        newly_resolved = _write_watchlist_additions(
            batch_id, pending_entries, rank_by_code, config, providers, resolver, now
        )
        repository_results.update(newly_resolved)
        batch_item["repository_results"] = repository_results
    # 純粋な状態遷移(データは銘柄単位で既に永続化済み)。前回既にこのフェーズを
    # 完了していた場合はConditionExpression不成立でFalseになるだけで、再開ロジック
    # はこの戻り値を分岐条件に使わない。
    mark_watchlist_write_completed(batch_id, now)
    added_stock_codes = [
        code for code, result in repository_results.items() if result == REPOSITORY_RESULT_ADDED
    ]

    # --- Phase 3: NOTIFICATION_PENDING -> NOTIFICATION_SENT/NOTIFICATION_FAILED ---
    # 運用ハードニング第3弾1節: このフェーズは自己完結的に例外を処理する。
    # notify_watchlist_additions()が例外を送出しても、この関数自体は正常return
    # する(NOTIFICATION_FAILEDとして記録するのみ)。maybe_finalize/retry_finalize
    # の外側try/exceptへは伝播させず、finalize全体をFINALIZE_FAILEDにしない
    # (ウォッチリスト追加結果はPhase2で既に確定・保持済みのため失われない)。
    if "finalize_notification_outcome" not in batch_item:
        pending_notification_codes = list(added_stock_codes)
        if not pending_notification_codes:
            record_notification_resolved(batch_id, now, [], NOTIFICATION_OUTCOME_NOT_REQUIRED)
            notification_outcome = NOTIFICATION_OUTCOME_NOT_REQUIRED
        elif not wc.notification_enabled:
            record_notification_resolved(batch_id, now, [], NOTIFICATION_OUTCOME_SKIPPED)
            notification_outcome = NOTIFICATION_OUTCOME_SKIPPED
        else:
            if "finalize_notification_content_hash" in batch_item:
                content_hash = batch_item["finalize_notification_content_hash"]
            else:
                # 運用ハードニング第3弾4節: 再試行時のnow()ではなく、バッチ開始日
                # (started_at)を評価基準日として使う(日付をまたぐ再試行でも
                # 同じhashになるようにするため)。この値はrecord_notification_pending
                # で永続化し、以後の再試行では再計算せずそのまま再利用する。
                content_hash = compute_watchlist_addition_content_hash(
                    batch_id, pending_notification_codes, wc.screening_policy, started_at.date()
                )
                record_notification_pending(batch_id, now, content_hash)
            pending_items = _build_watchlist_items_for_codes(
                pending_notification_codes,
                ranked,
                providers,
                resolver,
                started_at,
                wc.screening_policy,
            )
            summary: WatchlistAdditionSummary = build_watchlist_addition_summary(
                added_items=pending_items,
                total_score_by_code=total_score_by_code,
                notification_detail_by_code=notification_detail_by_code,
                rank_by_code=RankingCalculator.rank(total_score_by_code),
                total_target_count=total_target_count,
                ranked_count=ranked_count,
                data_unavailable_count=data_unavailable_count,
                policy_name=wc.screening_policy,
                scoring_config=wc.scoring,
                thresholds_config=wc.thresholds,
                evaluated_at=started_at,
            )
            try:
                notification_service.notify_watchlist_additions(summary, content_hash)
            except Exception as exc:  # noqa: BLE001 - NOTIFICATION_FAILEDとして記録しfinalize全体は失敗にしない
                logger.exception(
                    "watchlist_screening notification failed batch_id=%s", batch_id
                )
                failure_count = record_notification_failed(batch_id, now, str(exc))
                permanently_failed = failure_count >= wc.max_notification_retry_attempts
                logger.warning(
                    "watchlist_screening notification failed batch_id=%s "
                    "failure_count=%d max_notification_retry_attempts=%d permanently_failed=%s",
                    batch_id,
                    failure_count,
                    wc.max_notification_retry_attempts,
                    permanently_failed,
                )
                if permanently_failed:
                    _finish_batch(
                        batch_id,
                        now,
                        started_at,
                        batch_item,
                        records,
                        metrics,
                        config,
                        EXECUTION_RESULT_NORMAL,
                        [],
                        added_stock_codes,
                        False,
                        True,
                        notification_permanently_failed=True,
                    )
                return
            record_notification_resolved(
                batch_id, now, pending_notification_codes, NOTIFICATION_OUTCOME_SENT
            )
            notification_outcome = NOTIFICATION_OUTCOME_SENT
    else:
        notification_outcome = batch_item["finalize_notification_outcome"]

    # --- Phase 4: COMPLETED ---
    notification_sent = notification_outcome == NOTIFICATION_OUTCOME_SENT
    _finish_batch(
        batch_id,
        now,
        started_at,
        batch_item,
        records,
        metrics,
        config,
        EXECUTION_RESULT_NORMAL,
        [],
        added_stock_codes,
        notification_sent,
        False,
    )


def maybe_finalize(
    batch_id: str,
    now: dt.datetime,
    providers: ProviderBundle,
    config: AppConfig,
    notification_service: LineNotificationService,
) -> bool:
    """11節: try_finalize_if_ready()がRUNNING→FINALIZE_PREPARINGへの遷移に成功
    した場合のみfinalize処理を実行してTrueを返す。条件不成立(まだ完了していない、
    または他の実行に競り負けた)の場合はFalseを返す(エラーではない)。
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
    """運用ハードニング5節/第2弾2節: FINALIZE_FAILED状態のバッチに対する再試行版
    `maybe_finalize`。`try_retry_finalize`(FINALIZE_FAILED→FINALIZE_PREPARING)に
    成功した場合のみ`_finalize_completed`を実行する。

    `_finalize_completed`はBatchRunsTable項目のフィールドの有無で段階的に再開
    可能な設計(FINALIZE_PREPARING/WATCHLIST_WRITE_COMPLETED/
    NOTIFICATION_PENDING/NOTIFICATION_SENTの4段階、詳細はモジュールdocstring
    参照)であるため、途中まで進んでいた前回の実行結果を安全に引き継いで
    再実行できる。呼び出し元(Reconciler/CLI)はそれぞれの方針で再試行回数を
    制御すること(本関数自体は無制限に呼び出し可能)。
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


def retry_notification(
    batch_id: str,
    now: dt.datetime,
    providers: ProviderBundle,
    config: AppConfig,
    notification_service: LineNotificationService,
) -> bool:
    """運用ハードニング第3弾1節: NOTIFICATION_FAILED状態のバッチに対する、通知
    のみの再試行版`maybe_finalize`。`try_retry_notification`
    (NOTIFICATION_FAILED→NOTIFICATION_PENDING)に成功した場合のみ
    `_finalize_completed`を実行する。

    Phase1(対象決定)・Phase2(ウォッチリスト書込み)はfinalize_target_stock_codes/
    repository_resultsが既に永続化されているため即座にスキップされ、
    **ウォッチリスト書込みは再実行されない**。Phase3(通知)自体が送出する例外は
    `_finalize_completed`内部で処理され外へ伝播しないため、この関数の外側
    try/exceptはPhase3以外での想定外の例外(監査記録失敗等)のみを捕捉して
    FINALIZE_FAILEDへ落とす。呼び出し元(Reconciler/CLI)はそれぞれの方針で
    再試行回数を制御すること(本関数自体は無制限に呼び出し可能)。
    """
    if not try_retry_notification(batch_id, now):
        return False

    try:
        _finalize_completed(batch_id, now, providers, config, notification_service)
    except Exception as exc:  # noqa: BLE001 - 失敗を記録してから再送出する
        logger.exception("watchlist_screening notification retry failed batch_id=%s", batch_id)
        mark_watchlist_finalize_failed(batch_id, now, str(exc))
        raise
    return True


# --- ウォッチリスト自動運用の改善(自動メンテナンス、計画Part C)----------------
# WATCHLIST_MAINTENANCEジョブ専用の軽量finalize。ランキング・ウォッチリスト
# 追加・LINE通知は一切行わない(対象は既に登録済みのAUTO_SCREENING銘柄の
# 再評価・自動削除のみのため)。rotation commit(_maybe_commit_rotation)も
# 呼ばない(計画Part A-9、job_type=="NEW_CANDIDATE_SCREENING"専用)。


def _parse_maintenance_screening_summary(raw: str | None) -> MaintenanceScreeningSummary | None:
    if raw is None:
        return None
    try:
        return MaintenanceScreeningSummary.model_validate_json(raw)
    except Exception:
        logger.exception(
            "watchlist maintenance screening_summary_json parse failed, treating as absent"
        )
        return None


def _finalize_maintenance_completed(batch_id: str, now: dt.datetime, config: AppConfig) -> None:
    """計画Part C-3の3段階判定を全銘柄へ適用し、削除・監査記録を行う。"""
    records = query_all_candidate_progress(batch_id, consistent_read=True)
    watchlist_repo = WatchlistRepository()
    auto_removal_config = config.watchlist_screening.auto_removal
    removal_history_repo = WatchlistRemovalHistoryRepository(
        auto_removal_config.readd_cooldown_days
    )

    outcome_counts: dict[str, int] = {}
    stale_unconfirmed_count = 0

    for record in records:
        item = watchlist_repo.get(record.stock_code)
        if item is None:
            # 手動削除等で既に存在しない。スキップ(このバッチのfinalize対象外)。
            continue

        summary = _parse_maintenance_screening_summary(record.screening_summary_json)
        decision = evaluate_maintenance_decision(item, summary, auto_removal_config, now)
        outcome_counts[decision.outcome.value] = outcome_counts.get(decision.outcome.value, 0) + 1
        if decision.stale_unconfirmed:
            stale_unconfirmed_count += 1
            logger.warning(
                "watchlist maintenance: stock long unconfirmed stock_code=%s batch_id=%s",
                item.stock_code,
                batch_id,
            )

        if decision.outcome in (
            MaintenanceOutcome.IMMEDIATE_REMOVAL,
            MaintenanceOutcome.CONSECUTIVE_NOT_QUALIFIED_REMOVAL,
        ):
            removal_category = (
                "IMMEDIATE"
                if decision.outcome == MaintenanceOutcome.IMMEDIATE_REMOVAL
                else "CONSECUTIVE_NOT_QUALIFIED"
            )
            watchlist_repo.delete(item.stock_code)
            removal_history_repo.upsert(
                WatchlistRemovalHistory(
                    stock_code=item.stock_code,
                    removed_at=now,
                    removal_reason=decision.removal_reason or "",
                    removal_category=removal_category,
                    cooldown_until=now
                    + dt.timedelta(days=auto_removal_config.readd_cooldown_days),
                )
            )
            record_removal_audit(
                item.stock_code,
                item.stock_name,
                item.created_at,
                item.registration_policy,
                now,
                decision.removal_reason or "",
                removal_category,
                decision.updated_item.last_monitoring_score,
                decision.updated_item.last_matched_target_types,
                decision.updated_item.consecutive_not_qualified_count,
                summary.hard_exclusion_reasons if summary is not None else [],
                now,
                batch_id,
            )
        else:
            watchlist_repo.upsert(decision.updated_item)

    record_batch_audit(
        execution_mode="scheduled",
        universe_provider="watchlist_maintenance",
        screening_policies=[config.watchlist_screening.screening_policy],
        output_values={
            "execution_result": EXECUTION_RESULT_NORMAL,
            "outcome_counts": outcome_counts,
            "stale_unconfirmed_count": stale_unconfirmed_count,
        },
        now=now,
        batch_id=batch_id,
        idempotency_key=f"watchlist_maintenance_batch_audit:{batch_id}",
    )
    mark_watchlist_batch_completed(batch_id, EXECUTION_RESULT_NORMAL, now)
    logger.info(
        "watchlist_maintenance finalized batch_id=%s outcome_counts=%s stale_unconfirmed=%d",
        batch_id,
        outcome_counts,
        stale_unconfirmed_count,
    )


def maybe_finalize_maintenance(batch_id: str, now: dt.datetime, config: AppConfig) -> bool:
    """計画Part C-7案A: `try_finalize_if_ready()`(既存の排他遷移、共通)が成功
    した場合のみメンテナンスfinalizeを実行する。`maybe_finalize`(ADD用)と
    同じ排他制御・再開耐性を持つが、ランキング・通知フェーズは持たない。
    """
    if not try_finalize_if_ready(batch_id, now):
        return False

    try:
        _finalize_maintenance_completed(batch_id, now, config)
    except Exception as exc:  # noqa: BLE001 - 失敗を記録してから再送出する
        logger.exception("watchlist_maintenance finalize failed batch_id=%s", batch_id)
        mark_watchlist_finalize_failed(batch_id, now, str(exc))
        raise
    return True
