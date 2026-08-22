"""保有銘柄CSV一括登録サービス(要求仕様3節 csv_import_service、5節)。

MVPではローカルCLIからの取り込みを実装する(S3経由の取り込みは後続フェーズで追加)。
全件成功/全件失敗ではなく、行単位で結果を返す。
"""

from __future__ import annotations

import csv
import datetime as dt
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.owner import (
    DEFAULT_OWNER,
    InvalidOwnerError,
    build_holding_id,
    normalize_and_validate_owner,
)
from jstock_advisor.infrastructure.external_value_parser import ExternalValueParser
from jstock_advisor.services.portfolio_service import PortfolioService

REQUIRED_COLUMNS = {"stock_code", "shares", "purchase_price"}
OPTIONAL_COLUMNS = {
    "owner",
    "stock_name",
    "purchase_date",
    "account_type",
    "investment_purpose",
    "profit_target_rate",
    "memo",
}
ALL_COLUMNS = REQUIRED_COLUMNS | OPTIONAL_COLUMNS

DuplicatePolicy = Literal["additional_purchase", "overwrite"]


class CsvRowStatus(StrEnum):
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    ERROR = "ERROR"


class CsvImportRowResult(BaseModel):
    row_number: int
    status: CsvRowStatus
    stock_code: str | None
    message: str


class CsvImportSummary(BaseModel):
    total_rows: int = 0
    success_count: int = 0
    warning_count: int = 0
    error_count: int = 0
    results: list[CsvImportRowResult] = []

    def add(self, result: CsvImportRowResult) -> None:
        self.results.append(result)
        self.total_rows += 1
        if result.status == CsvRowStatus.SUCCESS:
            self.success_count += 1
        elif result.status == CsvRowStatus.WARNING:
            self.warning_count += 1
        else:
            self.error_count += 1


class HoldingsCsvImportService:
    def __init__(self, portfolio_service: PortfolioService | None = None) -> None:
        self._portfolio = portfolio_service or PortfolioService()

    def import_file(
        self,
        path: Path,
        on_duplicate: DuplicatePolicy = "additional_purchase",
    ) -> CsvImportSummary:
        summary = CsvImportSummary()
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise ValueError("CSVにヘッダー行がありません")
            missing = REQUIRED_COLUMNS - set(reader.fieldnames)
            if missing:
                raise ValueError(f"CSVに必須列がありません: {sorted(missing)}")

            seen_rows: set[tuple[str, str, str, str, str]] = set()
            overwritten_holding_ids: set[str] = set()
            for row_number, row in enumerate(reader, start=2):  # 1行目はヘッダー
                result = self._process_row(
                    row_number, row, seen_rows, on_duplicate, overwritten_holding_ids
                )
                summary.add(result)
        return summary

    def _process_row(
        self,
        row_number: int,
        row: dict[str, str | None],
        seen_rows: set[tuple[str, str, str, str, str]],
        on_duplicate: DuplicatePolicy,
        overwritten_holding_ids: set[str],
    ) -> CsvImportRowResult:
        owner_raw = (row.get("owner") or "").strip() or DEFAULT_OWNER
        try:
            owner = normalize_and_validate_owner(owner_raw)
        except InvalidOwnerError:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=None,
                message=f"所有者の指定が不正です: {owner_raw!r}",
            )

        stock_code = ExternalValueParser.stock_code(row.get("stock_code"))
        if stock_code is None:
            raw_stock_code = (row.get("stock_code") or "").strip()
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=raw_stock_code or None,
                message="銘柄コードが不正です(4桁の英数字が必要です)",
            )

        shares = ExternalValueParser.integer(row.get("shares"))
        if shares is None or shares <= 0:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message="株数は正の整数である必要があります",
            )

        purchase_price = ExternalValueParser.decimal(row.get("purchase_price"))
        if purchase_price is None or purchase_price <= 0:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message="購入単価は正の数値である必要があります",
            )

        purchase_date_raw = (row.get("purchase_date") or "").strip()
        if purchase_date_raw:
            purchase_date = ExternalValueParser.date(purchase_date_raw)
            if purchase_date is None:
                return CsvImportRowResult(
                    row_number=row_number,
                    status=CsvRowStatus.ERROR,
                    stock_code=stock_code,
                    message="購入日はYYYY-MM-DD形式で指定してください",
                )
        else:
            purchase_date = dt.date.today()

        messages: list[str] = []

        account_type_raw = (row.get("account_type") or "").strip().upper()
        if account_type_raw:
            try:
                account_type = AccountType(account_type_raw)
            except ValueError:
                return CsvImportRowResult(
                    row_number=row_number,
                    status=CsvRowStatus.ERROR,
                    stock_code=stock_code,
                    message=f"口座区分が不正です: {account_type_raw}",
                )
        else:
            account_type = AccountType.GENERAL
            messages.append("口座区分が未指定のためGENERAL(一般口座)として登録しました")

        profit_target_rate_raw = (row.get("profit_target_rate") or "").strip()
        profit_target_rate: float | None = None
        if profit_target_rate_raw:
            try:
                profit_target_rate = float(profit_target_rate_raw)
            except ValueError:
                return CsvImportRowResult(
                    row_number=row_number,
                    status=CsvRowStatus.ERROR,
                    stock_code=stock_code,
                    message="利確目標率は数値で指定してください",
                )

        dedup_key = (owner, stock_code, str(purchase_date), str(purchase_price), str(shares))
        if dedup_key in seen_rows:
            messages.append("CSV内に同一内容の行が重複しています")
        seen_rows.add(dedup_key)

        stock_name = (row.get("stock_name") or "").strip() or None
        investment_purpose = (row.get("investment_purpose") or "").strip() or None
        memo = (row.get("memo") or "").strip() or None

        holding_id = build_holding_id(owner, stock_code)
        existing = self._portfolio.get_holding(owner, stock_code)
        if (
            existing is not None
            and on_duplicate == "overwrite"
            and holding_id not in overwritten_holding_ids
        ):
            self._portfolio.delete_holding(owner, stock_code)
            overwritten_holding_ids.add(holding_id)
            messages.append("既存の保有銘柄を上書きしました")
        elif existing is not None and on_duplicate == "additional_purchase":
            messages.append("既存の保有銘柄への追加購入として登録しました")

        self._portfolio.register_purchase(
            owner=owner,
            stock_code=stock_code,
            stock_name=stock_name,
            shares=shares,
            purchase_price=purchase_price,
            purchase_date=purchase_date,
            account_type=account_type,
            investment_purpose=investment_purpose,
            profit_target_rate=profit_target_rate,
            memo=memo,
        )

        status = CsvRowStatus.WARNING if messages else CsvRowStatus.SUCCESS
        return CsvImportRowResult(
            row_number=row_number,
            status=status,
            stock_code=stock_code,
            message="; ".join(messages) if messages else "登録成功",
        )
