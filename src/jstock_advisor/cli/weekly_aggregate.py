"""週次評価集計(WeeklyEvaluationAggregate)の backfill・照合・rebuild のCLI(Issue #537)。

* **ローカル専用**である(ローカルの保管ディレクトリ・ローカルの Aggregate ストアだけを読み書きする。
  `build_weekly_evaluation_aggregate_store()` は Lambda 以外では本番のテーブルへアクセスしない)。
  Production の Aggregate への backfill / rebuild の**実行手段**(どの主体・どの経路で実行するか)は、
  deploy の Human Gate で決める。本CLIは、その判断材料になる **dry-run の報告**(対象週数・行数・
  想定 write 数)と、同じロジックのローカル実行を提供する。
* **dry-run が既定**。write は `--execute` を明示したときだけ。
* 通常の週次レビューは、これらの処理を呼ばない(呼ぶと raw の全件 Scan になる)。
"""

from __future__ import annotations

import datetime as dt

import typer

from jstock_advisor.config.loader import load_config
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.weekly_evaluation_aggregate_store import (
    build_weekly_evaluation_aggregate_store,
)
from jstock_advisor.services.weekly_evaluation_aggregate_service import (
    WeeklyAggregateMaintenanceService,
)

app = typer.Typer(
    help="週次評価集計(Aggregate)の backfill・照合・rebuild(ローカル専用・dry-run 既定)"
)


def _service() -> WeeklyAggregateMaintenanceService:
    evaluations = EvaluationResultRepository()
    return WeeklyAggregateMaintenanceService(
        store=build_weekly_evaluation_aggregate_store(
            evaluation_inserter=evaluations.insert_if_absent
        ),
        evaluations=evaluations.iter_all,  # 束縛メソッドそのもの(呼ぶたびに新しい走査になる)
        recommendations=RecommendationRepository(),
        horizon_calendar_days=load_config().review_improvement.evaluation_horizon_days,
    )


@app.command("backfill")
def backfill(
    execute: bool = typer.Option(False, "--execute", help="指定しない限り write しない(dry-run)"),
) -> None:
    """全履歴の Aggregate を、raw から一度だけ構築する(dry-run では、計画だけを報告する)。"""
    now = dt.datetime.now(dt.UTC)
    service = _service()
    plan = service.execute_backfill(now) if execute else service.plan_backfill(now)
    typer.echo(f"mode={'EXECUTE' if execute else 'DRY_RUN'}")
    typer.echo(f"scanned_evaluations={plan.scanned_evaluations}")
    typer.echo(f"matched_evaluations={plan.matched_evaluations}")
    typer.echo(f"missing_recommendation_count={plan.missing_recommendation_count}")
    typer.echo(f"weeks={plan.week_count} ({plan.first_week} .. {plan.last_week})")
    typer.echo(f"aggregate_rows={plan.row_count}")
    typer.echo(f"estimated_write_items={plan.estimated_write_items}")


@app.command("verify")
def verify(
    week: list[str] = typer.Option([], "--week", help="照合する週(例 2026-W38)。省略で全週"),
    mark_rebuild_required: bool = typer.Option(
        False, "--mark-rebuild-required", help="不一致の週を AGGREGATE_REBUILD_REQUIRED にする"
    ),
) -> None:
    """raw から作った集計と、保存済みの Aggregate を突合する(不一致があれば終了コード 1)。"""
    now = dt.datetime.now(dt.UTC)
    report = _service().verify(
        now,
        frozenset(week) if week else None,
        mark_rebuild_required=mark_rebuild_required,
    )
    typer.echo(f"weeks_checked={report.weeks_checked}")
    for mismatch in report.mismatches:
        typer.echo(f"MISMATCH {mismatch.review_week}: {'; '.join(mismatch.reasons)}")
    typer.echo(f"consistent={report.consistent}")
    if report.marked_rebuild_required:
        typer.echo(f"marked_rebuild_required={','.join(report.marked_rebuild_required)}")
    if not report.consistent:
        raise typer.Exit(code=1)


@app.command("rebuild")
def rebuild(
    week: list[str] = typer.Option(..., "--week", help="作り直す週(例 2026-W38)。複数指定可"),
    execute: bool = typer.Option(False, "--execute", help="指定しない限り write しない(dry-run)"),
) -> None:
    """指定した週だけを、raw から作り直す(複数週は 1 回の走査にまとめる)。"""
    now = dt.datetime.now(dt.UTC)
    result = _service().rebuild_weeks(frozenset(week), now, execute=execute)
    typer.echo(f"mode={'EXECUTE' if execute else 'DRY_RUN'}")
    for review_week, rows in result.items():
        typer.echo(f"{review_week}: aggregate_rows={rows}")
