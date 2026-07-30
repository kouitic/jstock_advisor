"""株主優待CLIコマンド(要求仕様7節、未確定事項#5)。

株主優待は自動取得できる公式データ源が無いため、必ずユーザー自身が確認した
一次情報(会社発表・証券会社サイト等)に基づいて登録すること。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from pathlib import Path

import typer

from jstock_advisor.domain.entities.enums import BenefitUtilityCategory
from jstock_advisor.services.shareholder_benefit_csv_import_service import (
    ShareholderBenefitCsvImportService,
)
from jstock_advisor.services.shareholder_benefit_registry_service import (
    ShareholderBenefitRegistryService,
)

app = typer.Typer(help="株主優待の手動登録(自動取得非対応のため要ユーザー登録)")


def _parse_date_list(value: str | None) -> list[dt.date]:
    if not value:
        return []
    try:
        return [dt.date.fromisoformat(v.strip()) for v in value.split(",") if v.strip()]
    except ValueError as e:
        raise typer.BadParameter(
            "権利確定日はYYYY-MM-DD形式をカンマ区切りで指定してください"
        ) from e


def _parse_decimal(value: str | None, field_name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as e:
        raise typer.BadParameter(f"{field_name}は数値で指定してください") from e


def _parse_month_list(value: str | None) -> list[int]:
    if not value:
        return []
    try:
        months = [int(v.strip()) for v in value.split(",") if v.strip()]
    except ValueError as e:
        raise typer.BadParameter("権利確定月はカンマ区切りの整数(1-12)で指定してください") from e
    if any(m < 1 or m > 12 for m in months):
        raise typer.BadParameter("権利確定月は1〜12の範囲で指定してください")
    return months


@app.command("add")
def add(
    stock_code: str = typer.Argument(..., help="銘柄コード"),
    min_shares_required: int = typer.Option(..., "--min-shares-required"),
    frequency_per_year: int = typer.Option(..., "--frequency-per-year"),
    category: BenefitUtilityCategory = typer.Option(..., "--category"),
    description: str = typer.Option(..., "--description"),
    min_shares_for_tier: int = typer.Option(..., "--min-shares-for-tier"),
    estimated_value: str = typer.Option(None, "--estimated-value"),
    long_term_months: int = typer.Option(None, "--long-term-months"),
    tier_group: str = typer.Option(
        None,
        "--tier-group",
        help="保有株数×保有期間のマトリクス優待で、段階同士が排他的な選択肢である"
        "ことを示すグループ名(例: digital_gift)。同一グループ内は最も条件の良い"
        "1件のみが有効になる(未指定なら他の明細と独立して常に加算される)",
    ),
    record_dates: str = typer.Option(
        None, "--record-dates", help="権利確定日(カンマ区切り、YYYY-MM-DD)"
    ),
    record_date_recurrence_months: str = typer.Option(
        None,
        "--record-date-recurrence-months",
        help="毎年の権利確定月(カンマ区切り、例: 3,9)。指定すると次回権利確定日を"
        "カレンダー上の実際の月末日から自動算出して保持する",
    ),
    ex_date: str = typer.Option(
        None, "--ex-date", help="権利落ち日(YYYY-MM-DD、権利確定日とは別概念)"
    ),
    long_term_holding_requirement: str = typer.Option(
        None,
        "--long-term-holding-requirement",
        help="長期保有条件の自由記述(例: 3年以上継続保有で優待内容が優遇される)",
    ),
) -> None:
    """株主優待を登録する(既存登録があれば上書きされる)。"""
    ex_date_parsed = None
    if ex_date:
        try:
            ex_date_parsed = dt.date.fromisoformat(ex_date)
        except ValueError as e:
            raise typer.BadParameter("権利落ち日はYYYY-MM-DD形式で指定してください") from e

    service = ShareholderBenefitRegistryService()
    benefit = service.register(
        stock_code=stock_code,
        min_shares_required=min_shares_required,
        frequency_per_year=frequency_per_year,
        category=category,
        description=description,
        min_shares_for_tier=min_shares_for_tier,
        estimated_value=_parse_decimal(estimated_value, "estimated_value"),
        long_term_holding_condition_months=long_term_months,
        tier_group=tier_group,
        benefit_record_dates=_parse_date_list(record_dates),
        benefit_record_date_recurrence_months=_parse_month_list(record_date_recurrence_months),
        benefit_ex_date=ex_date_parsed,
        long_term_holding_requirement=long_term_holding_requirement,
    )
    next_date = (
        f"、次回権利確定日:{benefit.next_benefit_record_date}"
        if benefit.next_benefit_record_date
        else ""
    )
    typer.echo(
        f"登録しました: {benefit.stock_code} ({len(benefit.benefits)}件の優待内容){next_date}"
    )


@app.command("add-tier")
def add_tier(
    stock_code: str = typer.Argument(...),
    category: BenefitUtilityCategory = typer.Option(..., "--category"),
    description: str = typer.Option(..., "--description"),
    min_shares_for_tier: int = typer.Option(..., "--min-shares-for-tier"),
    estimated_value: str = typer.Option(None, "--estimated-value"),
    long_term_months: int = typer.Option(None, "--long-term-months"),
    tier_group: str = typer.Option(
        None, "--tier-group", help="registerコマンドの--tier-groupを参照"
    ),
) -> None:
    """既存登録に、保有株数に応じた別の優待段階を追加する。"""
    service = ShareholderBenefitRegistryService()
    try:
        benefit = service.add_benefit_detail(
            stock_code=stock_code,
            category=category,
            description=description,
            min_shares_for_tier=min_shares_for_tier,
            estimated_value=_parse_decimal(estimated_value, "estimated_value"),
            long_term_holding_condition_months=long_term_months,
            tier_group=tier_group,
        )
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e
    typer.echo(f"追加しました: {benefit.stock_code} ({len(benefit.benefits)}件の優待内容)")


@app.command("set-record-date-recurrence")
def set_record_date_recurrence(
    stock_code: str = typer.Argument(...),
    months: str = typer.Argument(..., help="毎年の権利確定月(カンマ区切り、例: 3,9)"),
) -> None:
    """「毎年◯月末」の周期を登録し、次回権利確定日をカレンダー上の実際の月末日から
    自動算出する。"""
    service = ShareholderBenefitRegistryService()
    try:
        benefit = service.set_record_date_recurrence(
            stock_code=stock_code, recurrence_months=_parse_month_list(months)
        )
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e
    typer.echo(
        f"更新しました: {benefit.stock_code} 次回権利確定日={benefit.next_benefit_record_date}"
    )


@app.command("update-status")
def update_status(
    stock_code: str = typer.Argument(...),
    abolished: bool = typer.Option(None, "--abolished/--not-abolished"),
    major_downgrade: bool = typer.Option(None, "--major-downgrade/--no-major-downgrade"),
    note: str = typer.Option(None, "--note"),
) -> None:
    """優待の廃止・改悪状況を更新する(売却判定ロジックに使用される)。"""
    service = ShareholderBenefitRegistryService()
    try:
        benefit = service.update_status(
            stock_code=stock_code,
            is_abolished=abolished,
            is_major_downgrade=major_downgrade,
            change_note=note,
        )
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e
    typer.echo(
        f"更新しました: {benefit.stock_code} "
        f"廃止={benefit.is_abolished} 大幅改悪={benefit.is_major_downgrade}"
    )


@app.command("list")
def list_benefits() -> None:
    """登録済みの株主優待一覧を表示する。"""
    service = ShareholderBenefitRegistryService()
    benefits = service.list_all()
    if not benefits:
        typer.echo("株主優待は登録されていません。")
        return
    for b in benefits:
        status = ""
        if b.is_abolished:
            status = " [廃止]"
        elif b.is_major_downgrade:
            status = " [大幅改悪]"
        next_date = (
            f"、次回権利確定日:{b.next_benefit_record_date}" if b.next_benefit_record_date else ""
        )
        typer.echo(
            f"{b.stock_code}: 最低{b.min_shares_required}株、年{b.frequency_per_year}回"
            f"{status}{next_date}"
        )
        for detail in b.benefits:
            group = f" [{detail.tier_group}]" if detail.tier_group else ""
            typer.echo(f"  {detail.min_shares_for_tier}株〜: {detail.description}{group}")


@app.command("show")
def show(stock_code: str) -> None:
    """特定銘柄の株主優待詳細を表示する。"""
    service = ShareholderBenefitRegistryService()
    benefit = service.get(stock_code)
    if benefit is None:
        typer.echo(f"{stock_code}の株主優待は登録されていません。")
        raise typer.Exit(code=1)
    typer.echo(benefit.model_dump_json(indent=2))


@app.command("delete")
def delete(
    stock_code: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="確認をスキップする"),
) -> None:
    """株主優待の登録を削除する。"""
    if not yes and not typer.confirm(f"{stock_code}の株主優待登録を削除します。よろしいですか?"):
        raise typer.Abort()
    service = ShareholderBenefitRegistryService()
    deleted = service.delete(stock_code)
    typer.echo("削除しました。" if deleted else "該当する登録は見つかりませんでした。")


@app.command("import-csv")
def import_csv(
    path: Path = typer.Argument(..., exists=True, readable=True, help="取り込むCSVファイルのパス"),
) -> None:
    """株主優待CSVを一括登録する(同一銘柄コードの行は1つにまとめられる)。"""
    service = ShareholderBenefitCsvImportService()
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
