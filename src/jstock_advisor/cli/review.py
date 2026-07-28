"""定期レビューレポートCLIコマンド(要求仕様42節)。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import typer

from jstock_advisor.config.loader import load_config
from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.services.before_after_report_service import BeforeAfterReportService
from jstock_advisor.services.provider_factory import (
    build_mock_provider_bundle,
    build_real_provider_bundle,
)
from jstock_advisor.services.review_report_service import ReviewReportService

app = typer.Typer(help="振り返りレポートの生成・LINE送信")


@app.command("report")
def report(
    horizon: int = typer.Option(
        None, "--horizon", help="営業日数で絞り込む(省略時は全ホライズン合算)"
    ),
    notify: bool = typer.Option(
        False,
        "--notify/--no-notify",
        help="LINEへ送信する(LINE_CHANNEL_ACCESS_TOKEN/LINE_USER_ID未設定時は標準出力に表示のみ)",
    ),
) -> None:
    """振り返りレポートを生成し、表示・LINE送信する。"""
    service = ReviewReportService(line_client=build_line_client_from_env() if notify else None)
    if notify:
        text = service.send_report(horizon_business_days=horizon)
    else:
        text = service.build_report_text(horizon_business_days=horizon)
    typer.echo(text)


@app.command("before-after")
def before_after(
    stocks: str = typer.Option(
        ..., "--stocks", help="銘柄コードをカンマ区切りで指定(例: 5401,8136,2914)"
    ),
    basis_date: str = typer.Option(
        None, "--basis-date", help="基準日(YYYY-MM-DD、省略時は本日)"
    ),
    source: str = typer.Option("real", "--source", help="データ提供元: real(既定)/ mock"),
    output: Path = typer.Option(
        None, "--output", help="出力先パス(省略時はdocs/design/配下に自動生成)"
    ),
) -> None:
    """根本原因修正の前後で判定がどう変化したかをMarkdownレポートとして出力する(LINE送信なし)。"""
    stock_codes = [s.strip() for s in stocks.split(",") if s.strip()]
    if not stock_codes:
        typer.echo("--stocksを1件以上指定してください")
        raise typer.Exit(code=1)

    if basis_date:
        try:
            basis = dt.date.fromisoformat(basis_date)
        except ValueError as e:
            raise typer.BadParameter("--basis-dateはYYYY-MM-DD形式で指定してください") from e
        now = dt.datetime.combine(basis, dt.time(hour=7), tzinfo=dt.UTC)
    else:
        now = dt.datetime.now(dt.UTC)

    config = load_config()
    providers = (
        build_real_provider_bundle(now, config)
        if source == "real"
        else build_mock_provider_bundle(now)
    )
    service = BeforeAfterReportService(providers=providers, config=config)
    report = service.build_report(stock_codes, now)
    markdown = service.render_markdown(report)

    output_path = output or (
        Path("docs/design") / f"before_after_report_{now.date().isoformat()}.md"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    typer.echo(f"レポートを出力しました: {output_path}")
