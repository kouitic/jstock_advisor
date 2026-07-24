"""定期レビューレポートCLIコマンド(要求仕様42節)。"""

from __future__ import annotations

import typer

from jstock_advisor.infrastructure.line.client import build_line_client_from_env
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
