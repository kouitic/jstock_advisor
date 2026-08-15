"""ウォッチリスト自動追加(週次スクリーニング)の手動実行・dry-run確認用CLI。

Lambda(watchlist_auto_addition_handler.py)がfan-outで並列実行するのに対し、
このCLIは単一プロセスで同期的に全銘柄を評価する(手元での確認・小規模ユニバース
向け)。カテゴリ分類・AuditLog記録・ランキングロジックはLambdaハンドラと共通の
関数(domain/signals/watchlist_screening.py, services/watchlist_screening_audit.py,
services/watchlist_screening_service.py)を使う。
"""

from __future__ import annotations

import datetime as dt
import uuid

import typer

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.enums import WatchlistRegistrationSource
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.domain.ranking import RankingCalculator
from jstock_advisor.domain.signals.watchlist_screening import (
    RankingEntry,
    WatchlistScoreDetail,
    categorize_exclusion_reasons,
    describe_matched_criteria,
)
from jstock_advisor.infrastructure.aws.batch_tracker import (
    WatchlistBatchStatus,
    WatchlistProgressStatus,
    claim_candidate_lease,
    complete_candidate,
    get_watchlist_batch,
    query_all_candidate_progress,
    try_operator_abort,
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
from jstock_advisor.lambda_handlers.watchlist_worker_handler import (
    _WORKER_LEASE_SECONDS,
    _evaluate_candidate,
)
from jstock_advisor.services.line_notification_service import (
    LineNotificationService,
    compute_watchlist_addition_content_hash,
)
from jstock_advisor.services.provider_factory import (
    build_candidate_universe_provider,
    build_real_provider_bundle,
)
from jstock_advisor.services.screening_data_provider import (
    ScreeningDataStatus,
    StockSnapshotScreeningDataProvider,
)
from jstock_advisor.services.watchlist_addition_summary_builder import (
    WatchlistAdditionSummary,
    build_watchlist_addition_summary,
)
from jstock_advisor.services.watchlist_batch_finalizer import (
    maybe_finalize,
    retry_finalize,
    retry_notification,
)
from jstock_advisor.services.watchlist_candidate_collector import WatchlistCandidateCollector
from jstock_advisor.services.watchlist_data_cache import build_cached_provider_bundle
from jstock_advisor.services.watchlist_display_name import build_stock_display_name_resolver
from jstock_advisor.services.watchlist_score_detail import build_notification_detail
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

# 運用ハードニング6節: batch-status/list-incomplete/retry-finalize/retry-stock/abortは
# SQSベース分散処理(Dispatcher/Worker/...)が使う本番DynamoDB(BatchRunsTable/
# WatchlistCandidateProgressTable)を直接操作する。ローカル実行時にこれらへ接続する
# には、運用手順書記載のAWS_LAMBDA_FUNCTION_NAME環境変数トリック(ローカルCLIから
# 本番相当のDynamoDBバックエンドを選択させる)が必要。
app = typer.Typer(help="ウォッチリスト自動追加(週次スクリーニング)の手動実行・運用コマンド")


def _build_notification_service(config: AppConfig) -> LineNotificationService:
    return LineNotificationService(
        line_client=build_line_client_from_env(),
        notification_log_repository=NotificationLogRepository(),
        recommendation_repository=RecommendationRepository(),
        config=config,
    )


@app.command("run")
def run(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="WatchlistRepositoryへの書き込み・LINE通知・AuditLog記録を行わず結果のみ表示する",
    ),
) -> None:
    """候補銘柄ユニバースを評価し、条件を満たした銘柄をウォッチリストへ追加する。"""
    now = dt.datetime.now(dt.UTC)
    # Lambda側のbatch_idと同様、この実行1回につき1つ発行する。record_candidate_audit
    # とrecord_repository_result_audit(dry-runでは呼ばない)の両方へ同じ値を渡すことで、
    # 後からbatch_id経由で評価結果とRepository結果を突き合わせられるようにする。
    batch_id = f"watchlist-screening-cli-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    config = load_config()
    wc = config.watchlist_screening

    if not dry_run and not wc.enabled:
        typer.echo(
            "ウォッチリスト自動追加機能はconfig(watchlist_screening.enabled)で"
            "無効化されています。--dry-runでの確認のみ可能です。"
        )
        raise typer.Exit(code=1)

    providers = build_real_provider_bundle(now, config)
    universe_provider = build_candidate_universe_provider(config, now)
    screening_data_provider = StockSnapshotScreeningDataProvider(providers, config)
    collector = WatchlistCandidateCollector(
        universe_provider, screening_data_provider, staged_rollout=wc.staged_rollout
    )

    try:
        collector_result = collector.collect_target_codes()
    except CandidateUniverseError as e:
        typer.echo(f"候補銘柄ユニバースの取得に失敗しました: {e}")
        raise typer.Exit(code=1) from e

    screening_service = WatchlistScreeningService(config)

    data_success_count = 0
    data_failure_count = 0
    required_failed_count = 0
    score_failed_count = 0
    unrankable_count = 0
    passed_results: list[WatchlistScreeningResult] = []
    passed_entries: list[RankingEntry] = []
    # LINE通知品質改善(2026-08 修正①): total_score_by_codeはevaluate()実行済み
    # の全銘柄(passed以外も含む)、notification_detail_by_codeはpassed銘柄のみ。
    total_score_by_code: dict[str, float] = {}
    notification_detail_by_code: dict[str, WatchlistScoreDetail] = {}

    for stock_code in collector_result.stock_codes:
        screening_data = screening_data_provider.get_screening_input(stock_code, now)
        if screening_data.status != ScreeningDataStatus.OK or screening_data.input is None:
            data_failure_count += 1
            if not dry_run:
                record_candidate_audit(
                    stock_code, None, "DATA_INSUFFICIENT", now, batch_id=batch_id
                )
            continue

        result = screening_service.evaluate(
            stock_code, screening_data.input.stock_name, screening_data.input, now
        )
        total_score_by_code[stock_code] = result.total_score
        category, evaluation_result = categorize_exclusion_reasons(result.exclusion_reasons)

        ranking_entry = None
        if category == "passed":
            ranking_entry = screening_service.to_ranking_entry(result)
            if ranking_entry is None:
                # MAX_RANKING_ENTRY_BYTESを超過し、main_metricsを空にしても収まらない
                # (v1の単一Policyでは実質発生しないが、将来の複数Policy化への安全策)。
                category = "failed"
                evaluation_result = "PASSED_RANKING_ENTRY_TOO_LARGE"
                unrankable_count += 1
            else:
                detail = build_notification_detail(
                    stock_code,
                    result.policy_results[0].score_breakdown,
                    screening_data.input,
                    policy_name=result.policy_results[0].policy_name,
                )
                if detail is not None:
                    notification_detail_by_code[stock_code] = detail

        if not dry_run:
            record_candidate_audit(stock_code, result, evaluation_result, now, batch_id=batch_id)

        if category == "data_insufficient":
            data_failure_count += 1
            continue
        data_success_count += 1
        if category == "required_condition_failed":
            required_failed_count += 1
        elif category == "score_failed":
            score_failed_count += 1
        elif category == "passed":
            # categoryが"passed"のままであれば、上のブロックでranking_entryは
            # 必ずNoneでない(Noneの場合はcategoryを"failed"へ書き換えている)。
            assert ranking_entry is not None
            passed_results.append(result)
            passed_entries.append(ranking_entry)
        # "failed"(unrankable)はdata_success_count/unrankable_countのみ計上し、
        # ランキング・登録の対象外とする。

    results_by_code = {result.stock_code: result for result in passed_results}
    all_ranked = WatchlistScreeningService.rank(passed_entries)
    limit = wc.max_watchlist_additions_per_run
    ranked_entries = all_ranked[:limit]
    over_limit_entries = all_ranked[limit:]
    over_limit_count = len(over_limit_entries)

    registration_source = WatchlistRegistrationSource.AUTO_SCREENING.value
    added_items: list[WatchlistItem] = []
    added_results: dict[str, WatchlistScreeningResult] = {}
    watchlist_repo = None if dry_run else WatchlistRepository()
    concurrent_duplicate_count = 0
    repository_failure_count = 0
    resolver = build_stock_display_name_resolver(
        wc.stock_display_name.jpx_name_negative_cache_ttl_seconds
    )

    for rank, entry in enumerate(ranked_entries, start=1):
        result = results_by_code[entry.stock_code]
        # LINE通知品質改善(2026-08 修正②): result.stock_nameは既に評価済みで
        # 追加のI/Oを伴わないため、fallback_name(即値)として渡す
        # (fallback_name_providerは指定しない)。
        item = WatchlistItem(
            stock_code=entry.stock_code,
            stock_name=resolver.resolve(entry.stock_code, fallback_name=result.stock_name),
            reason=describe_matched_criteria(entry.matched_criteria),
            registration_source=WatchlistRegistrationSource.AUTO_SCREENING,
            registration_policy=wc.screening_policy,
            created_at=now,
            updated_at=now,
        )
        if watchlist_repo is None:
            added_items.append(item)
            added_results[item.stock_code] = result
            continue
        try:
            added = watchlist_repo.add_if_new(item)
        except Exception as e:  # noqa: BLE001 - 1銘柄の書き込み失敗で全体を止めない
            typer.echo(f"追加に失敗しました: {entry.stock_code}: {e}")
            repository_failure_count += 1
            record_repository_result_audit(
                batch_id,
                entry.stock_code,
                result.stock_name,
                rank,
                entry.total_score,
                REPOSITORY_RESULT_FAILED,
                False,
                registration_source,
                wc.screening_policy,
                now,
                error=e,
            )
            continue
        if not added:
            concurrent_duplicate_count += 1
            record_repository_result_audit(
                batch_id,
                entry.stock_code,
                result.stock_name,
                rank,
                entry.total_score,
                REPOSITORY_RESULT_SKIPPED_EXISTING,
                False,
                registration_source,
                wc.screening_policy,
                now,
            )
            continue
        added_items.append(item)
        added_results[item.stock_code] = result
        record_repository_result_audit(
            batch_id,
            entry.stock_code,
            result.stock_name,
            rank,
            entry.total_score,
            REPOSITORY_RESULT_ADDED,
            True,
            registration_source,
            wc.screening_policy,
            now,
        )

    if not dry_run:
        for rank, entry in enumerate(over_limit_entries, start=len(ranked_entries) + 1):
            over_limit_result = results_by_code[entry.stock_code]
            record_repository_result_audit(
                batch_id,
                entry.stock_code,
                over_limit_result.stock_name,
                rank,
                entry.total_score,
                REPOSITORY_RESULT_SKIPPED_OVER_LIMIT,
                False,
                registration_source,
                wc.screening_policy,
                now,
            )

    _print_summary(
        dry_run=dry_run,
        universe_provider_name=wc.candidate_universe.provider,
        csv_path=wc.candidate_universe.csv_path,
        policy_name=wc.screening_policy,
        universe_count=collector_result.universe_count,
        duplicate_count=collector_result.duplicate_count,
        holding_excluded_count=collector_result.holding_excluded_count,
        watchlist_excluded_count=collector_result.watchlist_excluded_count,
        evaluation_target_count=len(collector_result.stock_codes),
        data_success_count=data_success_count,
        data_failure_count=data_failure_count,
        required_failed_count=required_failed_count,
        score_failed_count=score_failed_count,
        unrankable_count=unrankable_count,
        passed_count=len(passed_results),
        addition_limit=wc.max_watchlist_additions_per_run,
        over_limit_count=over_limit_count,
        added_items=added_items,
        added_results=added_results,
    )

    if dry_run:
        typer.echo("\ndry-runのため、ウォッチリストへの登録およびLINE通知は行っていません。")
        return

    notification_sent = False
    notification_failure = False
    if added_items and wc.notification_enabled:
        notification_service = LineNotificationService(
            line_client=build_line_client_from_env(),
            notification_log_repository=NotificationLogRepository(),
            recommendation_repository=RecommendationRepository(),
            config=config,
        )
        content_hash = compute_watchlist_addition_content_hash(
            batch_id, [item.stock_code for item in added_items], wc.screening_policy, now.date()
        )
        summary: WatchlistAdditionSummary = build_watchlist_addition_summary(
            added_items=added_items,
            total_score_by_code=total_score_by_code,
            notification_detail_by_code=notification_detail_by_code,
            rank_by_code=RankingCalculator.rank(total_score_by_code),
            total_target_count=len(collector_result.stock_codes),
            ranked_count=len(total_score_by_code),
            data_unavailable_count=data_failure_count,
            policy_name=wc.screening_policy,
            scoring_config=wc.scoring,
            thresholds_config=wc.thresholds,
            evaluated_at=now,
        )
        try:
            notification_sent = notification_service.notify_watchlist_additions(
                summary, content_hash
            )
        except Exception as e:  # noqa: BLE001 - 通知失敗はバッチ失敗にしない(ベストエフォート)
            typer.echo(f"LINE通知に失敗しました: {e}")
            notification_failure = True

    record_batch_audit(
        execution_mode="manual",
        universe_provider=wc.candidate_universe.provider,
        screening_policies=[wc.screening_policy],
        output_values={
            "started_at": now.isoformat(),
            "completed_at": dt.datetime.now(dt.UTC).isoformat(),
            "universe_count": collector_result.universe_count,
            "deduplicated_count": collector_result.duplicate_count,
            "invalid_code_count": collector_result.invalid_code_count,
            "holding_excluded_count": collector_result.holding_excluded_count,
            "watchlist_excluded_count": collector_result.watchlist_excluded_count,
            "evaluation_target_count": len(collector_result.stock_codes),
            "data_success_count": data_success_count,
            "data_failure_count": data_failure_count,
            "required_condition_failed_count": required_failed_count,
            "score_failed_count": score_failed_count,
            "passed_count": len(passed_results),
            "addition_limit": wc.max_watchlist_additions_per_run,
            "addition_candidate_count": len(ranked_entries),
            "actual_added_count": len(added_items),
            "concurrent_duplicate_count": concurrent_duplicate_count,
            "repository_failure_count": repository_failure_count,
            "notification_sent": notification_sent,
            "notification_failure": notification_failure,
        },
        now=now,
        batch_id=batch_id,
    )

    typer.echo(f"\nウォッチリストへ{len(added_items)}件追加しました。")
    typer.echo(f"LINE通知: {'送信しました' if notification_sent else '送信していません'}")


def _print_summary(
    *,
    dry_run: bool,
    universe_provider_name: str,
    csv_path: str,
    policy_name: str,
    universe_count: int,
    duplicate_count: int,
    holding_excluded_count: int,
    watchlist_excluded_count: int,
    evaluation_target_count: int,
    data_success_count: int,
    data_failure_count: int,
    required_failed_count: int,
    score_failed_count: int,
    unrankable_count: int,
    passed_count: int,
    addition_limit: int,
    over_limit_count: int,
    added_items: list[WatchlistItem],
    added_results: dict[str, WatchlistScreeningResult],
) -> None:
    title = "ウォッチリスト自動追加 dry-run" if dry_run else "ウォッチリスト自動追加"
    typer.echo("=" * 50)
    typer.echo(title)
    typer.echo("=" * 50)
    typer.echo()
    typer.echo(f"Universe: {universe_provider_name} ({csv_path})")
    typer.echo(f"Policy: {policy_name}")
    typer.echo()
    typer.echo(f"対象ユニバース: {universe_count}件(重複除去: {duplicate_count}件)")
    typer.echo(f"保有銘柄除外: {holding_excluded_count}件")
    typer.echo(f"既登録除外: {watchlist_excluded_count}件")
    typer.echo(f"評価対象: {evaluation_target_count}件")
    typer.echo()
    typer.echo(f"データ取得成功: {data_success_count}件")
    typer.echo(f"データ取得失敗: {data_failure_count}件")
    typer.echo(f"必須条件不一致: {required_failed_count}件")
    typer.echo(f"スコア不足: {score_failed_count}件")
    if unrankable_count:
        typer.echo(f"ランキング算入不可(データ超過): {unrankable_count}件")
    typer.echo(f"合格: {passed_count}件")
    typer.echo(f"追加上限: {addition_limit}件")
    label = "追加予定" if dry_run else "追加"
    typer.echo(f"{label}: {len(added_items)}件(上限超過: {over_limit_count}件)")
    typer.echo()
    typer.echo(f"{label}銘柄")

    for rank, item in enumerate(added_items, start=1):
        result = added_results[item.stock_code]
        typer.echo()
        typer.echo(f"{rank}. {item.stock_name or item.stock_code}（{item.stock_code}）")
        typer.echo(f"   総合スコア: {result.total_score:.1f}点")
        typer.echo("   Policyスコア:")
        for policy_result in result.policy_results:
            typer.echo(f"     {policy_result.policy_name}: {policy_result.score:.1f}点")
        metrics_line = "　".join(
            f"{label}: {value}" for label, value in result.main_metrics.items()
        )
        if metrics_line:
            typer.echo(f"   {metrics_line}")
        typer.echo(f"   一致条件: {describe_matched_criteria(result.matched_criteria)}")


# --- 運用ハードニング6節: SQSベース分散処理のバッチ運用コマンド ------------------


@app.command("batch-status")
def batch_status(batch_id: str) -> None:
    """指定batch_idのBatchRunsTable上の現在の状態を表示する(読み取り専用)。"""
    batch_item = get_watchlist_batch(batch_id)
    if batch_item is None:
        typer.echo(f"batch_id={batch_id} は見つかりませんでした。")
        raise typer.Exit(code=1)
    typer.echo("=" * 50)
    typer.echo(f"batch_id: {batch_id}")
    typer.echo("=" * 50)
    for key in sorted(batch_item):
        if key == "batch_id":
            continue
        typer.echo(f"{key}: {batch_item[key]}")


@app.command("list-incomplete")
def list_incomplete(batch_id: str) -> None:
    """指定batch_id配下で未完了(PENDING/PROCESSING)の進捗行を一覧表示する(読み取り専用)。"""
    records = query_all_candidate_progress(batch_id, consistent_read=True)
    incomplete = [
        r
        for r in records
        if r.status
        in (WatchlistProgressStatus.PENDING.value, WatchlistProgressStatus.PROCESSING.value)
    ]
    typer.echo(f"batch_id={batch_id}: 未完了 {len(incomplete)}/{len(records)}件")
    for r in incomplete:
        typer.echo(
            f"  {r.stock_code}: status={r.status} attempt_count={r.attempt_count} "
            f"lease_owner_id={r.lease_owner_id}"
        )


@app.command("retry-finalize")
def retry_finalize_command(
    batch_id: str,
    execute: bool = typer.Option(
        False, "--execute", help="実際にfinalize処理を再試行する(既定はdry-run)"
    ),
) -> None:
    """FINALIZE_FAILED状態のバッチに対してfinalize処理を再試行する。

    Reconciler(運用ハードニング5節)による自動再試行が上限(max_finalize_retry_attempts)に
    達した後の手動介入用。Reconcilerの試行回数上限とは独立に、1回のみ試みる。
    """
    batch_item = get_watchlist_batch(batch_id)
    if batch_item is None:
        typer.echo(f"batch_id={batch_id} は見つかりませんでした。")
        raise typer.Exit(code=1)
    status = batch_item.get("status")
    typer.echo(f"batch_id={batch_id}: 現在の状態 status={status}")
    if status != WatchlistBatchStatus.FINALIZE_FAILED.value:
        typer.echo("FINALIZE_FAILED状態ではないため、retry-finalizeの対象外です。")
        raise typer.Exit(code=1)
    if not execute:
        typer.echo("--executeを指定すると、finalize処理の再試行を行います(dry-run)。")
        return

    now = dt.datetime.now(dt.UTC)
    config = load_config()
    providers = build_cached_provider_bundle(build_real_provider_bundle(now, config), config, now)
    notification_service = _build_notification_service(config)
    if retry_finalize(batch_id, now, providers, config, notification_service):
        typer.echo("finalizeの再試行に成功しました。")
    else:
        typer.echo("再試行条件が不成立でした(既に他の主体が処理済み、または状態が変化しています)。")


@app.command("retry-notification")
def retry_notification_command(
    batch_id: str,
    execute: bool = typer.Option(
        False, "--execute", help="実際に通知のみを再試行する(既定はdry-run)"
    ),
) -> None:
    """NOTIFICATION_FAILED状態のバッチに対して、LINE通知のみを再試行する
    (運用ハードニング第3弾1節)。

    ウォッチリスト追加自体は既に確定・保持されているため、この再試行では
    WatchlistRepositoryへの書き込みは行わない(通知のみ)。Reconcilerによる
    自動再試行が上限(max_notification_retry_attempts)に達した後の手動介入用。
    """
    batch_item = get_watchlist_batch(batch_id)
    if batch_item is None:
        typer.echo(f"batch_id={batch_id} は見つかりませんでした。")
        raise typer.Exit(code=1)
    status = batch_item.get("status")
    typer.echo(f"batch_id={batch_id}: 現在の状態 status={status}")
    if status != WatchlistBatchStatus.NOTIFICATION_FAILED.value:
        typer.echo("NOTIFICATION_FAILED状態ではないため、retry-notificationの対象外です。")
        raise typer.Exit(code=1)
    if not execute:
        typer.echo("--executeを指定すると、通知のみの再試行を行います(dry-run)。")
        return

    now = dt.datetime.now(dt.UTC)
    config = load_config()
    providers = build_cached_provider_bundle(build_real_provider_bundle(now, config), config, now)
    notification_service = _build_notification_service(config)
    if retry_notification(batch_id, now, providers, config, notification_service):
        typer.echo("通知の再試行に成功しました。")
    else:
        typer.echo("再試行条件が不成立でした(既に他の主体が処理済み、または状態が変化しています)。")


@app.command("retry-stock")
def retry_stock(
    batch_id: str,
    stock_code: str,
    execute: bool = typer.Option(
        False, "--execute", help="実際にこの銘柄の評価を再実行する(既定はdry-run)"
    ),
) -> None:
    """指定銘柄をWorkerと同じロジックでローカルプロセス内から直接再評価する。

    SQSは経由しない(既存Worker評価関数`_evaluate_candidate`を直接呼び出す)。
    """
    records = query_all_candidate_progress(batch_id, consistent_read=True)
    target = next((r for r in records if r.stock_code == stock_code), None)
    if target is None:
        typer.echo(f"batch_id={batch_id} stock_code={stock_code} の進捗行が見つかりませんでした。")
        raise typer.Exit(code=1)
    typer.echo(
        f"現在の状態: status={target.status} attempt_count={target.attempt_count} "
        f"evaluation_result={target.evaluation_result}"
    )
    if not execute:
        typer.echo("--executeを指定すると、この銘柄をWorkerと同じロジックで再評価します。")
        return

    now = dt.datetime.now(dt.UTC)
    owner_id = f"cli-retry-{uuid.uuid4().hex[:8]}"
    if not claim_candidate_lease(batch_id, stock_code, owner_id, now, _WORKER_LEASE_SECONDS):
        typer.echo("リースを取得できませんでした(他のWorker/Reconcilerが処理中の可能性があります)。")
        raise typer.Exit(code=1)

    config = load_config()
    providers = build_cached_provider_bundle(build_real_provider_bundle(now, config), config, now)
    outcome = _evaluate_candidate(stock_code, batch_id, now, providers, config)
    completion_time = dt.datetime.now(dt.UTC)
    duration_ms = int((completion_time - now).total_seconds() * 1000)
    completed = complete_candidate(
        batch_id,
        stock_code,
        owner_id,
        terminal_status=outcome.terminal_status,
        evaluation_result=outcome.evaluation_result,
        ranking_entry=outcome.ranking_entry_json,
        is_provider_failure_suspected=outcome.is_provider_failure_suspected,
        missing_field_names=outcome.missing_field_names,
        processing_duration_ms=duration_ms,
        now=completion_time,
        total_score=outcome.total_score,
        notification_detail=outcome.notification_detail,
    )
    typer.echo(f"評価結果: {outcome.evaluation_result} (completed={completed})")
    if completed:
        notification_service = _build_notification_service(config)
        finalized = maybe_finalize(
            batch_id, completion_time, providers, config, notification_service
        )
        finalize_label = "実行しました" if finalized else "対象外でした(未完了行が他にあります)"
        typer.echo(f"finalize結果: {finalize_label}")


@app.command("abort")
def abort(
    batch_id: str,
    reason: str = typer.Option(..., "--reason", help="中断理由(execution_result・監査ログに記録)"),
    execute: bool = typer.Option(
        False, "--execute", help="実際にABORTEDへ強制遷移する(既定はdry-run)"
    ),
) -> None:
    """終端状態でないバッチを、運用者判断でABORTEDへ強制遷移させる。"""
    batch_item = get_watchlist_batch(batch_id)
    if batch_item is None:
        typer.echo(f"batch_id={batch_id} は見つかりませんでした。")
        raise typer.Exit(code=1)
    status = batch_item.get("status")
    typer.echo(f"batch_id={batch_id}: 現在の状態 status={status}")

    terminal_statuses = {
        WatchlistBatchStatus.COMPLETED.value,
        WatchlistBatchStatus.DISPATCH_FAILED.value,
        WatchlistBatchStatus.TIMED_OUT.value,
        WatchlistBatchStatus.ABORTED.value,
    }
    if status in terminal_statuses:
        typer.echo("既に終端状態のため、abortの対象外です。")
        raise typer.Exit(code=1)
    if not execute:
        typer.echo(f"--executeを指定すると、ABORTEDへ強制遷移します(理由: {reason})。")
        return

    now = dt.datetime.now(dt.UTC)
    if try_operator_abort(batch_id, reason, now):
        typer.echo("ABORTEDへ遷移しました。")
    else:
        typer.echo("遷移条件が不成立でした(既に終端状態へ変化していた可能性があります)。")
