"""監査ログCLIコマンド(要求仕様13節・21節)。"""

from __future__ import annotations

import typer

from jstock_advisor.domain.jst import format_jst
from jstock_advisor.infrastructure.local_repository.audit_log_repository import (
    AuditLogRepository,
)

app = typer.Typer(help="監査ログ(判定の入力値・計算式・出力値・出典)の閲覧")


@app.command("show")
def show_audit_log(
    stock_code: str = typer.Argument(..., help="銘柄コード"),
    decision_type: str = typer.Option(
        None, "--decision-type", help="buy_signal / profit_taking / sell_signal で絞り込む"
    ),
) -> None:
    """指定銘柄の監査ログ一覧を表示する。"""
    repo = AuditLogRepository()
    entries = repo.list_by_stock(stock_code)
    if decision_type:
        entries = [e for e in entries if e.decision_type == decision_type]

    if not entries:
        typer.echo(f"{stock_code}の監査ログはありません。")
        return

    for entry in entries:
        typer.echo(
            f"[{format_jst(entry.timestamp)}] {entry.decision_type} "
            f"(rule_version={entry.rule_version})"
        )
        typer.echo(f"  入力値: {entry.input_values}")
        typer.echo(f"  計算式: {entry.calculation_formulas}")
        typer.echo(f"  出力値: {entry.output_values}")
        if entry.data_sources:
            sources = ", ".join(
                f"{s.provider}@{format_jst(s.fetched_at)}" for s in entry.data_sources
            )
            typer.echo(f"  データ出典: {sources}")
