"""週次評価集計(WeeklyEvaluationAggregate)の backfill・照合・rebuild のCLI(Issue #537、#833)。

* **既定はローカル**である(`--backend local`。ローカルの保管ディレクトリ・ローカルの Aggregate
  ストアだけを読み書きする)。
* Production の DynamoDB を読む / 書く経路は、**`--backend dynamodb` を明示したときだけ**
  (Issue #833 E2)。
  環境変数 `AWS_LAMBDA_FUNCTION_NAME` による暗黙の切替(Lambda を装う設定)には依存しない。
  むしろ、その環境変数が設定されている環境では `--backend local` を fail-closed で拒否する
  (ローカルのつもりで Production に触れることを防ぐ)。
* `--backend dynamodb` は次の 3 つが揃わなければ、1 件も読まない・書かない:
    (a) `--backend dynamodb` の明示指定
    (b) `--aws-region`(既定なし)
    (c) `--confirm-table`(対象の Aggregate 表名の完全一致。
        表名は `{prefix}-weekly_evaluation_aggregate`)
* write は `--execute`(`verify` は `--mark-rebuild-required`)を**別に**明示したときだけ。
  dry-run / 突合のみは、書込 API を呼ばない。
* 資格情報の選択(`--aws-profile` または環境の AWS_PROFILE 等)は backend の選択と独立している。
  読取(dry-run・verify)は読取専用の profile で実行できる
  (書込権限が無ければ write は構造的にできない)。
* 通常の週次レビューは、これらの処理を呼ばない(呼ぶと raw の全件 Scan になる)。
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass

import boto3
import typer

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.aws.dynamodb_store import DynamoDbCollectionStore
from jstock_advisor.infrastructure.aws.weekly_evaluation_aggregate_dynamodb import (
    DynamoWeeklyEvaluationAggregateStore,
)
from jstock_advisor.infrastructure.collection_store import running_on_lambda
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
    help="週次評価集計(Aggregate)の backfill・照合・rebuild(既定はローカル・dry-run 既定)"
)

#: `--table-prefix` を省略したときの表名の prefix(テンプレートの TablePrefix の既定と同じ)。
DEFAULT_TABLE_PREFIX = "jstock"
_AGGREGATE_TABLE_SUFFIX = "weekly_evaluation_aggregate"
_EVALUATION_TABLE_SUFFIX = "evaluation_results"
_RECOMMENDATION_TABLE_SUFFIX = "recommendations"


class Backend(enum.StrEnum):
    """`--backend` の許容値。未知の値を local へ暗黙にフォールバックさせない。"""

    LOCAL = "local"
    DYNAMODB = "dynamodb"


@dataclass(frozen=True)
class BackendSelection:
    backend: Backend
    region: str | None = None
    confirm_table: str | None = None
    table_prefix: str = DEFAULT_TABLE_PREFIX
    profile: str | None = None

    @property
    def aggregate_table(self) -> str:
        return f"{self.table_prefix}-{_AGGREGATE_TABLE_SUFFIX}"

    @property
    def evaluation_table(self) -> str:
        return f"{self.table_prefix}-{_EVALUATION_TABLE_SUFFIX}"

    @property
    def recommendation_table(self) -> str:
        return f"{self.table_prefix}-{_RECOMMENDATION_TABLE_SUFFIX}"


def select_backend(
    backend: Backend,
    region: str | None,
    confirm_table: str | None,
    table_prefix: str | None,
    profile: str | None,
) -> BackendSelection:
    """backend の選択を検証する。条件を満たさなければ `typer.BadParameter`(読む前・書く前に失敗)。

    **環境変数からは何も選ばない。** `AWS_LAMBDA_FUNCTION_NAME` は『設定されていたら local を
    拒否する』方向にだけ使う(local のつもりで Production に触れないため)。
    """
    if backend is Backend.LOCAL:
        if running_on_lambda():
            raise typer.BadParameter(
                "環境変数 AWS_LAMBDA_FUNCTION_NAME が設定されているため、"
                "--backend local は拒否します"
                "(この設定のもとでは、ローカルのつもりでも Production の DynamoDB に触れる可能性が"
                "あります)。Production を対象にするなら --backend dynamodb を明示してください。"
                "ローカルで実行するなら、この環境変数を外してください",
                param_hint="--backend",
            )
        if region or confirm_table or profile or table_prefix:
            raise typer.BadParameter(
                "--aws-region / --confirm-table / --aws-profile / --table-prefix は"
                " --backend dynamodb のときだけ指定できます",
                param_hint="--backend",
            )
        return BackendSelection(Backend.LOCAL)
    if not region:
        raise typer.BadParameter(
            "--backend dynamodb では --aws-region の明示が必要です(既定はありません)",
            param_hint="--aws-region",
        )
    prefix = table_prefix or DEFAULT_TABLE_PREFIX
    selection = BackendSelection(Backend.DYNAMODB, region, confirm_table, prefix, profile or None)
    if not confirm_table:
        raise typer.BadParameter(
            "--backend dynamodb では --confirm-table(対象の Aggregate 表名)の入力が必要です",
            param_hint="--confirm-table",
        )
    if confirm_table != selection.aggregate_table:
        raise typer.BadParameter(
            "--confirm-table が、対象の Aggregate 表名と一致しません"
            "(読まない・書かないまま中止します)",
            param_hint="--confirm-table",
        )
    return selection


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


def _dynamodb_service(selection: BackendSelection) -> WeeklyAggregateMaintenanceService:
    """Production の DynamoDB を、表名を明示して直接構築する(環境変数に依存しない)。"""
    boto3.setup_default_session(profile_name=selection.profile, region_name=selection.region)
    evaluations = DynamoDbCollectionStore(
        EvaluationResult, selection.evaluation_table, "evaluation_id"
    )
    recommendations = DynamoDbCollectionStore(
        Recommendation, selection.recommendation_table, "recommendation_id"
    )
    store = DynamoWeeklyEvaluationAggregateStore(
        selection.aggregate_table, selection.evaluation_table, boto3.client("dynamodb")
    )
    return WeeklyAggregateMaintenanceService(
        store=store,
        evaluations=evaluations.iter_all,  # 束縛メソッドそのもの(呼ぶたびに新しい走査になる)
        recommendations=recommendations,
        horizon_calendar_days=load_config().review_improvement.evaluation_horizon_days,
    )


def _prepare(selection: BackendSelection, *, writes: bool) -> WeeklyAggregateMaintenanceService:
    """backend・対象(表名・region)・write の有無を先に表示してから、サービスを組み立てる。"""
    typer.echo(f"backend={selection.backend.value}")
    if selection.backend is Backend.DYNAMODB:
        typer.echo(f"aws_region={selection.region}")
        typer.echo(f"aggregate_table={selection.aggregate_table}")
        typer.echo(f"writes={'YES' if writes else 'NO(read-only)'}")
        return _dynamodb_service(selection)
    return _service()


_BACKEND_OPTION = typer.Option(
    Backend.LOCAL, "--backend", help="local(既定)| dynamodb(Production の DynamoDB。明示指定)"
)
_REGION_OPTION = typer.Option(None, "--aws-region", help="--backend dynamodb のとき必須(既定なし)")
_CONFIRM_TABLE_OPTION = typer.Option(
    None,
    "--confirm-table",
    help="--backend dynamodb のとき必須。対象の Aggregate 表名(完全一致)を入力して確認する",
)
_PREFIX_OPTION = typer.Option(
    None,
    "--table-prefix",
    help=f"表名の prefix(--backend dynamodb のみ。既定 {DEFAULT_TABLE_PREFIX})",
)
_PROFILE_OPTION = typer.Option(
    None,
    "--aws-profile",
    help="--backend dynamodb のとき使う AWS profile(任意。読取は読取専用の profile で足りる)",
)


@app.command("backfill")
def backfill(
    execute: bool = typer.Option(False, "--execute", help="指定しない限り write しない(dry-run)"),
    backend: Backend = _BACKEND_OPTION,
    aws_region: str | None = _REGION_OPTION,
    confirm_table: str | None = _CONFIRM_TABLE_OPTION,
    table_prefix: str | None = _PREFIX_OPTION,
    aws_profile: str | None = _PROFILE_OPTION,
) -> None:
    """全履歴の Aggregate を、raw から一度だけ構築する(dry-run では、計画だけを報告する)。"""
    selection = select_backend(backend, aws_region, confirm_table, table_prefix, aws_profile)
    now = dt.datetime.now(dt.UTC)
    service = _prepare(selection, writes=execute)
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
    backend: Backend = _BACKEND_OPTION,
    aws_region: str | None = _REGION_OPTION,
    confirm_table: str | None = _CONFIRM_TABLE_OPTION,
    table_prefix: str | None = _PREFIX_OPTION,
    aws_profile: str | None = _PROFILE_OPTION,
) -> None:
    """raw から作った集計と、保存済みの Aggregate を突合する(不一致があれば終了コード 1)。"""
    selection = select_backend(backend, aws_region, confirm_table, table_prefix, aws_profile)
    now = dt.datetime.now(dt.UTC)
    service = _prepare(selection, writes=mark_rebuild_required)
    report = service.verify(
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
    backend: Backend = _BACKEND_OPTION,
    aws_region: str | None = _REGION_OPTION,
    confirm_table: str | None = _CONFIRM_TABLE_OPTION,
    table_prefix: str | None = _PREFIX_OPTION,
    aws_profile: str | None = _PROFILE_OPTION,
) -> None:
    """指定した週だけを、raw から作り直す(複数週は 1 回の走査にまとめる)。"""
    selection = select_backend(backend, aws_region, confirm_table, table_prefix, aws_profile)
    now = dt.datetime.now(dt.UTC)
    service = _prepare(selection, writes=execute)
    result = service.rebuild_weeks(frozenset(week), now, execute=execute)
    typer.echo(f"mode={'EXECUTE' if execute else 'DRY_RUN'}")
    for review_week, rows in result.items():
        typer.echo(f"{review_week}: aggregate_rows={rows}")
