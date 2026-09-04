"""実売買記録CLIコマンド(要求仕様27節・28節)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import typer

from jstock_advisor.domain.entities.enums import AccountType, SkipReason, TransactionType
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER
from jstock_advisor.infrastructure.external_value_parser import ExternalValueParser
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.transaction_csv_import_service import TransactionCsvImportService
from jstock_advisor.services.transaction_history_service import TransactionHistoryService

app = typer.Typer(help="実際の売買記録(推奨に基づく執行結果・見送りの登録)")


def _parse_date(value: str | None) -> dt.date:
    if not value:
        return dt.date.today()
    parsed = ExternalValueParser.date(value)
    if parsed is None:
        raise typer.BadParameter("日付はYYYY-MM-DD形式で指定してください")
    return parsed


def _parse_decimal(value: str, field_name: str) -> Decimal:
    parsed = ExternalValueParser.decimal(value)
    if parsed is None:
        raise typer.BadParameter(f"{field_name}は数値で指定してください")
    return parsed


@app.command("buy-executed")
def buy_executed(
    stock_code: str = typer.Argument(..., help="銘柄コード"),
    shares: int = typer.Argument(..., help="約定株数"),
    price: str = typer.Argument(..., help="約定単価(円)"),
    owner: str = typer.Option(DEFAULT_OWNER, "--owner", help="所有者"),
    recommendation_id: str = typer.Option(None, "--recommendation-id", help="対応する推奨ID"),
    date: str = typer.Option(None, "--date", help="約定日(YYYY-MM-DD、省略時は本日)"),
    account_type: AccountType = typer.Option(None, "--account-type"),
    fee: str = typer.Option("0", "--fee", help="手数料(円)"),
    tax: str = typer.Option("0", "--tax", help="税金(円)"),
    reason: str = typer.Option(None, "--reason", help="売買理由(自由記述)"),
    memo: str = typer.Option(None, "--memo"),
    transaction_type: TransactionType = typer.Option(
        None, "--type", help="BUY/ADDITIONAL_BUY(省略時は保有有無から自動判定)"
    ),
) -> None:
    """買付の執行結果を記録する。"""
    if transaction_type is None:
        existing = PortfolioService().get_holding(owner, stock_code)
        transaction_type = TransactionType.ADDITIONAL_BUY if existing else TransactionType.BUY
    elif transaction_type not in (TransactionType.BUY, TransactionType.ADDITIONAL_BUY):
        raise typer.BadParameter("--type はBUYまたはADDITIONAL_BUYを指定してください")

    service = TransactionHistoryService()
    try:
        transaction = service.record_execution(
            owner=owner,
            stock_code=stock_code,
            transaction_type=transaction_type,
            shares=shares,
            execution_price=_parse_decimal(price, "約定単価"),
            execution_date=_parse_date(date),
            recommendation_id=recommendation_id,
            fee=_parse_decimal(fee, "手数料"),
            tax=_parse_decimal(tax, "税金"),
            account_type=account_type,
            reason=reason,
            memo=memo,
        )
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e

    typer.echo(
        f"記録しました: {transaction.transaction_id} [{transaction_type.value}] "
        f"{stock_code} {shares}株 @{price}円"
    )
    if transaction.price_diff_from_recommendation is not None:
        typer.echo(f"  推奨価格との差: {transaction.price_diff_from_recommendation}円")


@app.command("sell-executed")
def sell_executed(
    stock_code: str = typer.Argument(..., help="銘柄コード"),
    shares: int = typer.Argument(..., help="約定株数"),
    price: str = typer.Argument(..., help="約定単価(円)"),
    owner: str = typer.Option(DEFAULT_OWNER, "--owner", help="所有者"),
    recommendation_id: str = typer.Option(None, "--recommendation-id", help="対応する推奨ID"),
    date: str = typer.Option(None, "--date", help="約定日(YYYY-MM-DD、省略時は本日)"),
    account_type: AccountType = typer.Option(None, "--account-type"),
    fee: str = typer.Option("0", "--fee", help="手数料(円)"),
    tax: str = typer.Option("0", "--tax", help="税金(円)"),
    reason: str = typer.Option(None, "--reason", help="売買理由(自由記述)"),
    memo: str = typer.Option(None, "--memo"),
    transaction_type: TransactionType = typer.Option(
        None, "--type", help="PARTIAL_SELL/FULL_SELL(省略時は保有株数との比較で自動判定)"
    ),
) -> None:
    """売却の執行結果を記録する。"""
    if transaction_type is None:
        existing = PortfolioService().get_holding(owner, stock_code)
        if existing is not None and shares >= existing.shares:
            transaction_type = TransactionType.FULL_SELL
        else:
            transaction_type = TransactionType.PARTIAL_SELL
    elif transaction_type not in (TransactionType.PARTIAL_SELL, TransactionType.FULL_SELL):
        raise typer.BadParameter("--type はPARTIAL_SELLまたはFULL_SELLを指定してください")

    service = TransactionHistoryService()
    try:
        transaction = service.record_execution(
            owner=owner,
            stock_code=stock_code,
            transaction_type=transaction_type,
            shares=shares,
            execution_price=_parse_decimal(price, "約定単価"),
            execution_date=_parse_date(date),
            recommendation_id=recommendation_id,
            fee=_parse_decimal(fee, "手数料"),
            tax=_parse_decimal(tax, "税金"),
            account_type=account_type,
            reason=reason,
            memo=memo,
        )
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e

    typer.echo(
        f"記録しました: {transaction.transaction_id} [{transaction_type.value}] "
        f"{stock_code} {shares}株 @{price}円"
    )
    if transaction.price_diff_from_recommendation is not None:
        typer.echo(f"  推奨価格との差: {transaction.price_diff_from_recommendation}円")


@app.command("skip-recommendation")
def skip_recommendation(
    recommendation_id: str = typer.Argument(..., help="見送る推奨のID"),
    reason: SkipReason = typer.Option(..., "--reason", help="見送り理由"),
    detail: str = typer.Option(None, "--detail", help="理由の詳細(自由記述)"),
) -> None:
    """推奨に従わず見送った場合に、その理由を記録する。"""
    service = TransactionHistoryService()
    try:
        service.record_skip(recommendation_id, reason, detail)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e
    typer.echo(f"見送りとして記録しました: {recommendation_id} ({reason.value})")


@app.command("list")
def list_transactions(
    stock_code: str = typer.Argument(None, help="銘柄コードで絞り込む(省略時は全件)"),
) -> None:
    """記録済みの売買履歴を表示する。"""
    service = TransactionHistoryService()
    transactions = service.list_transactions(stock_code)
    if not transactions:
        typer.echo("売買記録はありません。")
        return
    for t in transactions:
        followed = "推奨あり" if t.followed_recommendation else "推奨なし"
        diff = (
            f" (推奨価格差:{t.price_diff_from_recommendation}円)"
            if t.price_diff_from_recommendation is not None
            else ""
        )
        typer.echo(
            f"{t.execution_date} [{t.transaction_type.value}] {t.stock_code} "
            f"{t.shares}株 @{t.execution_price}円 {followed}{diff}"
        )


@app.command("import-csv")
def import_csv(
    path: Path = typer.Argument(..., exists=True, readable=True, help="取り込むCSVファイルのパス"),
) -> None:
    """実売買記録CSVを一括登録する(行単位で結果を返す)。"""
    service = TransactionCsvImportService()
    try:
        summary = service.import_file(path)
    except ValueError as e:
        typer.echo(f"CSV取り込みに失敗しました: {e}")
        raise typer.Exit(code=1) from e

    for result in summary.results:
        marker = {
            "SUCCESS": "OK",
            "WARNING": "WARN",
            "ERROR": "NG",
            "SKIPPED_DUPLICATE": "SKIP",
        }[result.status.value]
        typer.echo(f"[{marker}] 行{result.row_number} {result.stock_code or '-'}: {result.message}")

    typer.echo(
        f"--- 合計{summary.total_rows}行 (成功:{summary.success_count} "
        f"警告:{summary.warning_count} スキップ:{summary.skipped_count} "
        f"エラー:{summary.error_count}) ---"
    )
    if summary.error_count > 0:
        raise typer.Exit(code=1)
