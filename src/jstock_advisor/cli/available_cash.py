"""買付余力(available_cash)の参照・棚卸し更新CLIコマンド(Issue #594、#128 A5c)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import typer

from jstock_advisor.infrastructure.external_value_parser import ExternalValueParser
from jstock_advisor.services.available_cash_service import (
    AvailableCashReconciliationExhaustedError,
    AvailableCashService,
)

app = typer.Typer(help="買付余力(available_cash)の参照・棚卸し更新")


def _parse_decimal(value: str, field_name: str) -> Decimal:
    parsed = ExternalValueParser.decimal(value)
    if parsed is None:
        raise typer.BadParameter(f"{field_name}は数値で指定してください")
    return parsed


@app.command("show")
def show_available_cash(
    owner: str = typer.Option(..., "--owner", help="所有者"),
) -> None:
    """owner単位の現在の買付余力を表示する(未登録と0円を区別する)。"""
    service = AvailableCashService()
    try:
        record = service.get(owner)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e

    if record is None:
        typer.echo(f"所有者{owner}の買付余力は未登録です。")
        return

    reconciled = (
        record.last_reconciled_at.isoformat()
        if record.last_reconciled_at is not None
        else "未棚卸し"
    )
    typer.echo(
        f"owner:{record.owner}\t買付余力:{record.available_cash}円\t"
        f"更新種別:{record.last_update_type.value}\t"
        f"最終更新:{record.updated_at.isoformat()}\t最終棚卸し:{reconciled}"
    )


@app.command("reconcile")
def reconcile_available_cash(
    owner: str = typer.Option(..., "--owner", help="所有者"),
    amount: str = typer.Option(..., "--amount", help="実額と照合した現在の買付余力(円、0以上)"),
) -> None:
    """証券会社等の実額と照合し、買付余力を明示的に上書きする(USER_RECONCILIATION)。

    過去の理論値との差額イベントは作らない(入力値をそのまま正とする。#589と同じ契約)。
    """
    parsed_amount = _parse_decimal(amount, "買付余力")
    service = AvailableCashService()
    now = dt.datetime.now(dt.UTC)
    try:
        record = service.reconcile(owner, parsed_amount, now)
    except (ValueError, AvailableCashReconciliationExhaustedError) as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e

    typer.echo(f"棚卸ししました: owner:{record.owner}\t買付余力:{record.available_cash}円")
