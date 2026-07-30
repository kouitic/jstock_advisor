"""ウォッチリストCLIコマンド(要求仕様3節・6節)。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path

import typer

from jstock_advisor.domain.entities.enums import Priority
from jstock_advisor.services.watchlist_csv_import_service import WatchlistCsvImportService
from jstock_advisor.services.watchlist_service import WatchlistService

app = typer.Typer(help="ウォッチリストの登録・編集・削除")


def _parse_decimal(value: str, field_name: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as e:
        raise typer.BadParameter(f"{field_name}は数値で指定してください") from e


@app.command("list")
def list_items() -> None:
    """ウォッチリスト一覧を表示する。"""
    service = WatchlistService()
    items = service.list_items()
    if not items:
        typer.echo("ウォッチリストは登録されていません。")
        return
    for item in items:
        typer.echo(
            f"{item.stock_code}\t{item.stock_name or '-'}\t優先度:{item.priority.value}\t"
            f"希望利回り:{item.desired_total_yield_pct}\t通知:{item.notify_enabled}"
        )


@app.command("add")
def add_item(
    stock_code: str = typer.Argument(..., help="銘柄コード"),
    stock_name: str = typer.Option(None, "--name", "-n"),
    reason: str = typer.Option(None, "--reason", help="登録理由"),
    desired_total_yield: float = typer.Option(None, "--desired-yield", help="希望総合利回り(%)"),
    desired_buy_price: str = typer.Option(None, "--desired-price", help="希望買値(円)"),
    benefit_interest: bool = typer.Option(False, "--benefit-interest/--no-benefit-interest"),
    priority: Priority = typer.Option(Priority.MEDIUM, "--priority"),
    notify: bool = typer.Option(True, "--notify/--no-notify"),
    memo: str = typer.Option(None, "--memo"),
) -> None:
    """ウォッチリストに銘柄を登録する。"""
    service = WatchlistService()
    item = service.add_item(
        stock_code=stock_code,
        stock_name=stock_name,
        reason=reason,
        desired_total_yield_pct=desired_total_yield,
        desired_buy_price=_parse_decimal(desired_buy_price, "希望買値")
        if desired_buy_price
        else None,
        benefit_interest=benefit_interest,
        priority=priority,
        notify_enabled=notify,
        memo=memo,
    )
    typer.echo(f"登録しました: {item.stock_code} {item.stock_name or ''}")


@app.command("update")
def update_item(
    stock_code: str,
    stock_name: str = typer.Option(None, "--name"),
    reason: str = typer.Option(None, "--reason"),
    desired_total_yield: float = typer.Option(None, "--desired-yield"),
    desired_buy_price: str = typer.Option(None, "--desired-price"),
    benefit_interest: bool = typer.Option(None, "--benefit-interest/--no-benefit-interest"),
    priority: Priority = typer.Option(None, "--priority"),
    notify: bool = typer.Option(None, "--notify/--no-notify"),
    memo: str = typer.Option(None, "--memo"),
) -> None:
    """ウォッチリスト項目を更新する。"""
    fields: dict[str, object] = {}
    if stock_name is not None:
        fields["stock_name"] = stock_name
    if reason is not None:
        fields["reason"] = reason
    if desired_total_yield is not None:
        fields["desired_total_yield_pct"] = desired_total_yield
    if desired_buy_price is not None:
        fields["desired_buy_price"] = _parse_decimal(desired_buy_price, "希望買値")
    if benefit_interest is not None:
        fields["benefit_interest"] = benefit_interest
    if priority is not None:
        fields["priority"] = priority
    if notify is not None:
        fields["notify_enabled"] = notify
    if memo is not None:
        fields["memo"] = memo

    if not fields:
        typer.echo("更新する項目が指定されていません。")
        raise typer.Exit(code=1)

    service = WatchlistService()
    try:
        item = service.update_item(stock_code, **fields)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e
    typer.echo(f"更新しました: {item.stock_code}")


@app.command("delete")
def delete_item(
    stock_code: str,
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """ウォッチリストから銘柄を削除する。"""
    if not yes and not typer.confirm(
        f"{stock_code}をウォッチリストから削除します。よろしいですか?"
    ):
        raise typer.Abort()
    service = WatchlistService()
    deleted = service.delete_item(stock_code)
    typer.echo("削除しました。" if deleted else "該当する項目は見つかりませんでした。")


@app.command("import-csv")
def import_csv(
    path: Path = typer.Argument(..., exists=True, readable=True, help="取り込むCSVファイルのパス"),
) -> None:
    """ウォッチリストCSVを一括登録する(既存登録があれば上書きされる)。"""
    service = WatchlistCsvImportService()
    try:
        summary = service.import_file(path)
    except ValueError as e:
        typer.echo(f"CSV取り込みに失敗しました: {e}")
        raise typer.Exit(code=1) from e

    for result in summary.results:
        marker = "OK" if result.status.value == "SUCCESS" else "NG"
        typer.echo(f"[{marker}] 行{result.row_number} {result.stock_code or '-'}: {result.message}")

    typer.echo(
        f"--- 合計{summary.total_rows}行 (成功:{summary.success_count} "
        f"エラー:{summary.error_count}) ---"
    )
    if summary.error_count > 0:
        raise typer.Exit(code=1)
