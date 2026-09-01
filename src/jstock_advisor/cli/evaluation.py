"""推奨の定点評価CLIコマンド(要求仕様29〜36節)。"""

from __future__ import annotations

import datetime as dt

import typer

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.provider_factory import (
    build_mock_provider_bundle,
    build_real_provider_bundle,
)
from jstock_advisor.services.recommendation_evaluation_service import (
    RecommendationEvaluationService,
)

app = typer.Typer(help="推奨の定点評価(推奨後N営業日/N暦日時点の実績計測とラベル付与)")

_SOURCE_HELP = "株価データ提供元: mock(既定)/ real(yfinance)"


def _format_horizon(result: EvaluationResult) -> str:
    if result.horizon_business_days is not None:
        return f"{result.horizon_business_days}営業日"
    return f"{result.horizon_calendar_days}暦日"


def _build_providers(source: str, now: dt.datetime, config: AppConfig) -> ProviderBundle:
    if source == "real":
        return build_real_provider_bundle(now, config)
    return build_mock_provider_bundle(now)


@app.command("run")
def run_due_evaluations(
    source: str = typer.Option("mock", "--source", help=_SOURCE_HELP),
) -> None:
    """評価期限を迎えた推奨について、定点評価を実行する。"""
    if source not in ("mock", "real"):
        raise typer.BadParameter("--source は mock または real を指定してください")

    now = dt.datetime.now(dt.UTC)
    config = load_config()
    calendar = BusinessCalendar.from_config(config.holiday_calendar)
    providers = _build_providers(source, now, config)
    service = RecommendationEvaluationService(
        market_data_provider=providers.market_data, config=config, business_calendar=calendar
    )

    # Issue #113: 営業日ホライズンと暦日ホライズンを1回の走査でまとめて処理する
    # (以前は推奨コレクションを2回走査していた)。
    outcome = service.run_due_evaluations_single_pass(
        now, calendar_horizon_days=config.review_improvement.evaluation_horizon_days
    )
    all_evaluated = outcome.evaluated
    all_skipped = outcome.skipped_due_to_data_error
    if not all_evaluated and not all_skipped:
        typer.echo("評価期限を迎えた推奨はありませんでした。")
        return

    for result in all_evaluated:
        typer.echo(
            f"[{result.evaluation_label.value}] recommendation_id={result.recommendation_id} "
            f"horizon={_format_horizon(result)} "
            f"株価リターン={result.price_return_pct:.1f}%"
        )
        typer.echo(f"  根拠: {result.label_evidence}")

    for stock_code, horizon, reason in all_skipped:
        typer.echo(f"[DATA_ERROR] {stock_code} horizon={horizon}: {reason}")


@app.command("list")
def list_evaluations(
    recommendation_id: str = typer.Option(None, "--recommendation-id", help="推奨IDで絞り込む"),
) -> None:
    """記録済みの評価結果を一覧表示する。"""
    repo = EvaluationResultRepository()
    results = (
        repo.list_by_recommendation(recommendation_id) if recommendation_id else repo.list_all()
    )
    if not results:
        typer.echo("評価結果はありません。")
        return
    for result in results:
        typer.echo(
            f"[{result.evaluation_label.value}] recommendation_id={result.recommendation_id} "
            f"horizon={_format_horizon(result)} "
            f"評価日={result.evaluation_date} 株価リターン={result.price_return_pct:.1f}%"
        )
