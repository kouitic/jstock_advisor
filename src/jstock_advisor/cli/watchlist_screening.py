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
from jstock_advisor.domain.entities.enums import WatchlistRegistrationSource
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.domain.signals.watchlist_screening import (
    RankingEntry,
    categorize_exclusion_reasons,
    describe_matched_criteria,
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
from jstock_advisor.services.line_notification_service import LineNotificationService
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

app = typer.Typer(help="ウォッチリスト自動追加(週次スクリーニング)の手動実行")


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

    for rank, entry in enumerate(ranked_entries, start=1):
        result = results_by_code[entry.stock_code]
        item = WatchlistItem(
            stock_code=entry.stock_code,
            stock_name=result.stock_name,
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
        try:
            notification_sent = notification_service.notify_watchlist_additions(
                added_items, added_results, wc.screening_policy, now
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
