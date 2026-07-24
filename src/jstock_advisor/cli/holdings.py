"""保有銘柄CLIコマンド(要求仕様3節・23節)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from pathlib import Path

import typer

from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.services.csv_import_service import HoldingsCsvImportService
from jstock_advisor.services.portfolio_service import PortfolioService

app = typer.Typer(help="保有銘柄の登録・編集・削除")


def _parse_date(value: str | None) -> dt.date:
    if not value:
        return dt.date.today()
    try:
        return dt.date.fromisoformat(value)
    except ValueError as e:
        raise typer.BadParameter("日付はYYYY-MM-DD形式で指定してください") from e


def _parse_decimal(value: str, field_name: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as e:
        raise typer.BadParameter(f"{field_name}は数値で指定してください") from e


@app.command("list")
def list_holdings() -> None:
    """保有銘柄一覧を表示する。"""
    service = PortfolioService()
    holdings = service.list_holdings()
    if not holdings:
        typer.echo("保有銘柄は登録されていません。")
        return
    for h in holdings:
        typer.echo(
            f"{h.stock_code}\t{h.stock_name}\t{h.shares}株\t平均取得単価:{h.average_purchase_price}円\t"
            f"口座:{h.account_type.value}"
        )


@app.command("show")
def show_holding(stock_code: str) -> None:
    """特定銘柄の保有詳細と購入ロット一覧を表示する。"""
    service = PortfolioService()
    holding = service.get_holding(stock_code)
    if holding is None:
        typer.echo(f"銘柄コード{stock_code}は登録されていません。")
        raise typer.Exit(code=1)
    typer.echo(holding.model_dump_json(indent=2))
    typer.echo("--- 購入ロット ---")
    for lot in service.list_lots(stock_code):
        typer.echo(
            f"{lot.lot_id}\t{lot.purchase_date}\t{lot.shares}株\t{lot.purchase_price}円\t"
            f"手数料:{lot.fee}円\t口座:{lot.account_type.value}"
        )


@app.command("add")
def add_holding(
    stock_code: str = typer.Argument(..., help="銘柄コード(4桁)"),
    shares: int = typer.Option(..., "--shares", "-s", help="購入株数"),
    price: str = typer.Option(..., "--price", "-p", help="購入単価(円)"),
    stock_name: str = typer.Option(None, "--name", "-n", help="銘柄名"),
    purchase_date: str = typer.Option(
        None, "--date", "-d", help="購入日(YYYY-MM-DD、省略時は本日)"
    ),
    account_type: AccountType = typer.Option(AccountType.GENERAL, "--account-type", "-a"),
    fee: str = typer.Option("0", "--fee", help="手数料(円)"),
    investment_purpose: str = typer.Option(None, "--purpose", help="投資目的"),
    sell_policy: str = typer.Option(None, "--sell-policy", help="売却方針"),
    profit_target_rate: float = typer.Option(None, "--profit-target-rate", help="利確目標率(%)"),
    memo: str = typer.Option(None, "--memo"),
) -> None:
    """保有銘柄を1件登録する(既存銘柄の場合は追加購入ロットとして扱う)。"""
    service = PortfolioService()
    holding = service.register_purchase(
        stock_code=stock_code,
        stock_name=stock_name,
        shares=shares,
        purchase_price=_parse_decimal(price, "購入単価"),
        purchase_date=_parse_date(purchase_date),
        account_type=account_type,
        fee=_parse_decimal(fee, "手数料"),
        investment_purpose=investment_purpose,
        sell_policy=sell_policy,
        profit_target_rate=profit_target_rate,
        memo=memo,
    )
    typer.echo(
        f"登録しました: {holding.stock_code} {holding.stock_name} "
        f"平均取得単価{holding.average_purchase_price}円"
    )


@app.command("update-meta")
def update_holding_meta(
    stock_code: str,
    stock_name: str = typer.Option(None, "--name"),
    market_segment: str = typer.Option(None, "--market-segment"),
    industry: str = typer.Option(None, "--industry"),
    investment_purpose: str = typer.Option(None, "--purpose"),
    sell_policy: str = typer.Option(None, "--sell-policy"),
    profit_target_price: str = typer.Option(None, "--profit-target-price"),
    profit_target_rate: float = typer.Option(None, "--profit-target-rate"),
    memo: str = typer.Option(None, "--memo"),
) -> None:
    """ロットから導出されない項目(銘柄名、業種、投資目的等)を更新する。"""
    fields: dict[str, object] = {}
    if stock_name is not None:
        fields["stock_name"] = stock_name
    if market_segment is not None:
        fields["market_segment"] = market_segment
    if industry is not None:
        fields["industry"] = industry
    if investment_purpose is not None:
        fields["investment_purpose"] = investment_purpose
    if sell_policy is not None:
        fields["sell_policy"] = sell_policy
    if profit_target_price is not None:
        fields["profit_target_price"] = _parse_decimal(profit_target_price, "利確目標価格")
    if profit_target_rate is not None:
        fields["profit_target_rate"] = profit_target_rate
    if memo is not None:
        fields["memo"] = memo

    if not fields:
        typer.echo("更新する項目が指定されていません。")
        raise typer.Exit(code=1)

    service = PortfolioService()
    try:
        holding = service.update_holding_meta(stock_code, **fields)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e
    typer.echo(f"更新しました: {holding.stock_code} {holding.stock_name}")


@app.command("delete")
def delete_holding(
    stock_code: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="確認をスキップする"),
) -> None:
    """保有銘柄(全ロット含む)を削除する。"""
    if not yes and not typer.confirm(
        f"{stock_code}の保有銘柄データを全て削除します。よろしいですか?"
    ):
        raise typer.Abort()
    service = PortfolioService()
    deleted = service.delete_holding(stock_code)
    typer.echo("削除しました。" if deleted else "該当する保有銘柄は見つかりませんでした。")


@app.command("delete-lot")
def delete_lot(stock_code: str, lot_id: str) -> None:
    """特定の購入ロットのみを削除し、保有サマリを再計算する。"""
    service = PortfolioService()
    try:
        holding = service.delete_lot(stock_code, lot_id)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e
    if holding is None:
        typer.echo("最後のロットを削除したため、保有銘柄も削除されました。")
    else:
        typer.echo(f"削除しました。再計算後の平均取得単価: {holding.average_purchase_price}円")


@app.command("import-csv")
def import_csv(
    path: Path = typer.Argument(..., exists=True, readable=True, help="取り込むCSVファイルのパス"),
    on_duplicate: str = typer.Option(
        "additional_purchase",
        "--on-duplicate",
        help="既存銘柄と重複する場合の扱い: additional_purchase(追加購入) または overwrite(上書き)",
    ),
) -> None:
    """保有銘柄CSVを一括登録する(行単位で結果を返す)。"""
    if on_duplicate not in ("additional_purchase", "overwrite"):
        raise typer.BadParameter(
            "--on-duplicate は additional_purchase または overwrite を指定してください"
        )

    service = HoldingsCsvImportService()
    try:
        summary = service.import_file(path, on_duplicate=on_duplicate)  # type: ignore[arg-type]
    except ValueError as e:
        typer.echo(f"CSV取り込みに失敗しました: {e}")
        raise typer.Exit(code=1) from e

    for result in summary.results:
        marker = {"SUCCESS": "OK", "WARNING": "WARN", "ERROR": "NG"}[result.status.value]
        typer.echo(f"[{marker}] 行{result.row_number} {result.stock_code or '-'}: {result.message}")

    typer.echo(
        f"--- 合計{summary.total_rows}行 (成功:{summary.success_count} "
        f"警告:{summary.warning_count} エラー:{summary.error_count}) ---"
    )
    if summary.error_count > 0:
        raise typer.Exit(code=1)
