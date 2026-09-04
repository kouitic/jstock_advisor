"""保有銘柄CLIコマンド(要求仕様3節・23節)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import typer

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER
from jstock_advisor.infrastructure.external_value_parser import ExternalValueParser
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.corporate_action_service import CorporateActionService
from jstock_advisor.services.csv_import_service import HoldingsCsvImportService
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.provider_factory import (
    build_mock_provider_bundle,
    build_real_provider_bundle,
)

app = typer.Typer(help="保有銘柄の登録・編集・削除")

_SOURCE_HELP = "企業行動データの取得元: mock(既定)/ real(yfinance+手動登録レジストリ)"


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


def _parse_positive_decimal(value: str, field_name: str) -> Decimal:
    """正の数値のみを受け付ける(Issue #75 Phase B2)。

    購入単価専用の入口チェックであり、**権威はPortfolioService側の検証**である
    (CLIを経由しない登録経路や将来のcallerも同じ契約で守られる)。ここでは
    永続層へ到達する前に原因の分かるメッセージで拒否することを目的とする。

    手数料(fee)は0が正当なため、`_parse_decimal` へ無条件の正値制約は加えない。
    """
    parsed = _parse_decimal(value, field_name)
    if parsed <= 0:
        raise typer.BadParameter(f"{field_name}は0より大きい値を指定してください")
    return parsed


def _parse_positive_int(value: int, field_name: str) -> int:
    """正の整数のみを受け付ける(Issue #93 Phase B1)。

    購入株数専用の入口チェックであり、**権威はPortfolioService側の検証**である
    (CLIを経由しない登録経路や将来のcallerも同じ契約で守られる)。ここでは
    永続層へ到達する前に原因の分かるメッセージで拒否することを目的とする。

    他の整数引数へ無条件の正値制約を加えないよう、専用のhelperとして分けている。
    """
    if value <= 0:
        raise typer.BadParameter(f"{field_name}は0より大きい値を指定してください")
    return value


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
def show_holding(
    stock_code: str, owner: str = typer.Option(DEFAULT_OWNER, "--owner", help="所有者")
) -> None:
    """特定銘柄の保有詳細と購入ロット一覧を表示する。"""
    service = PortfolioService()
    holding = service.get_holding(owner, stock_code)
    if holding is None:
        typer.echo(f"所有者{owner}・銘柄コード{stock_code}は登録されていません。")
        raise typer.Exit(code=1)
    typer.echo(holding.model_dump_json(indent=2))
    typer.echo("--- 購入ロット ---")
    for lot in service.list_lots(owner, stock_code):
        typer.echo(
            f"{lot.lot_id}\t{lot.purchase_date}\t{lot.shares}株\t{lot.purchase_price}円\t"
            f"手数料:{lot.fee}円\t口座:{lot.account_type.value}"
        )


@app.command("add")
def add_holding(
    stock_code: str = typer.Argument(..., help="銘柄コード(4桁)"),
    owner: str = typer.Option(DEFAULT_OWNER, "--owner", help="所有者"),
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
        owner=owner,
        stock_code=stock_code,
        stock_name=stock_name,
        shares=_parse_positive_int(shares, "購入株数"),
        purchase_price=_parse_positive_decimal(price, "購入単価"),
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
    owner: str = typer.Option(DEFAULT_OWNER, "--owner", help="所有者"),
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
        holding = service.update_holding_meta(owner, stock_code, **fields)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e
    typer.echo(f"更新しました: {holding.stock_code} {holding.stock_name}")


@app.command("delete")
def delete_holding(
    stock_code: str,
    owner: str = typer.Option(DEFAULT_OWNER, "--owner", help="所有者"),
    yes: bool = typer.Option(False, "--yes", "-y", help="確認をスキップする"),
) -> None:
    """保有銘柄(全ロット含む)を削除する。"""
    if not yes and not typer.confirm(
        f"所有者{owner}・{stock_code}の保有銘柄データを全て削除します。よろしいですか?"
    ):
        raise typer.Abort()
    service = PortfolioService()
    deleted = service.delete_holding(owner, stock_code)
    typer.echo("削除しました。" if deleted else "該当する保有銘柄は見つかりませんでした。")


@app.command("delete-lot")
def delete_lot(
    stock_code: str,
    lot_id: str,
    owner: str = typer.Option(DEFAULT_OWNER, "--owner", help="所有者"),
) -> None:
    """特定の購入ロットのみを削除し、保有サマリを再計算する。"""
    service = PortfolioService()
    try:
        holding = service.delete_lot(owner, stock_code, lot_id)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e
    if holding is None:
        typer.echo("最後のロットを削除したため、保有銘柄も削除されました。")
    else:
        typer.echo(f"削除しました。再計算後の平均取得単価: {holding.average_purchase_price}円")


@app.command("recompute-all")
def recompute_all(
    source: str = typer.Option("mock", "--source", help=_SOURCE_HELP),
) -> None:
    """全保有銘柄を、企業行動(株式分割等)調整後の基準で遡及再計算する(要求仕様2節)。

    PurchaseLot(購入時の生データ)は書き換えず、Holdingのshares/
    average_purchase_priceのみを、各ロットの購入日時点からの累積分割係数で
    調整して再計算する。本番DynamoDBに対する実行は、次回の自動分析ジョブが
    未調整の値を使ってしまう前に、デプロイ直後に一度だけ手動で行うこと。
    """
    if source not in ("mock", "real"):
        raise typer.BadParameter("--source は mock または real を指定してください")

    now = dt.datetime.now(dt.UTC)
    config = load_config()
    providers = (
        build_real_provider_bundle(now, config)
        if source == "real"
        else build_mock_provider_bundle(now)
    )
    corporate_action_service = CorporateActionService(providers.corporate_action, now=now)
    service = PortfolioService(corporate_action_service=corporate_action_service)
    audit = AuditService()

    holdings = service.list_holdings()
    if not holdings:
        typer.echo("保有銘柄は登録されていません。")
        return

    for holding in holdings:
        before_shares, before_price = holding.shares, holding.average_purchase_price
        updated = service.recompute_holding(holding.owner, holding.stock_code)
        audit.record(
            decision_type="holding_split_adjustment",
            stock_code=holding.stock_code,
            input_values={
                "shares_before": before_shares,
                "average_purchase_price_before": str(before_price),
            },
            calculation_formulas={
                "shares": (
                    "sum(lot.shares * cumulative_split_factor(lot.purchase_date, now) "
                    "for lot in lots)"
                ),
                "average_purchase_price": "sum(lot.amount() for lot in lots) / adjusted_shares",
            },
            output_values={
                "shares_after": updated.shares,
                "average_purchase_price_after": str(updated.average_purchase_price),
            },
            data_sources=[],
            rule_version="corporate-action-recompute-v1",
            timestamp=now,
        )
        if updated.shares != before_shares or updated.average_purchase_price != before_price:
            typer.echo(
                f"調整しました: {holding.stock_code} "
                f"{before_shares}株@{before_price}円 → "
                f"{updated.shares}株@{updated.average_purchase_price}円"
            )
        else:
            typer.echo(f"変更なし: {holding.stock_code}")


@app.command("import-csv")
def import_csv(
    path: Path = typer.Argument(..., exists=True, readable=True, help="取り込むCSVファイルのパス"),
    on_duplicate: str = typer.Option(
        "additional_purchase",
        "--on-duplicate",
        help="既存銘柄と重複する場合の扱い: additional_purchase(追加購入) または overwrite(上書き)",
    ),
) -> None:
    """保有銘柄CSVを一括登録する(行単位で結果を返す)。

    必須列: stock_code, shares, purchase_price, **owner**
    任意列: stock_name, purchase_date, account_type, investment_purpose,
            profit_target_rate, memo

    Issue #61: ownerは必須です(列が無い、または空欄の行はエラーになります)。
    以前は未指定時に自動で既定の所有者へ割り当てていましたが、別の所有者の保有が
    誤って1件へ統合される事故が起きたため廃止しました。

    同じ内容のCSVを再度取り込んだ場合、取り込み済みの行は登録せずスキップします
    (SKIP)。CSV内に同一内容の行が複数あるときは、2件目以降を登録しません(NG)。
    """
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
        # Issue #61 Phase B1: 取り込み済みでskipした行(SKIPPED_DUPLICATE)を追加。
        marker = {
            "SUCCESS": "OK",
            "WARNING": "WARN",
            "SKIPPED_DUPLICATE": "SKIP",
            "ERROR": "NG",
        }[result.status.value]
        typer.echo(f"[{marker}] 行{result.row_number} {result.stock_code or '-'}: {result.message}")

    typer.echo(
        f"--- 合計{summary.total_rows}行 (成功:{summary.success_count} "
        f"警告:{summary.warning_count} スキップ:{summary.skipped_count} "
        f"エラー:{summary.error_count}) ---"
    )
    if summary.error_count > 0:
        raise typer.Exit(code=1)
