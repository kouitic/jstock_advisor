"""実売買記録CSV一括登録サービス(要求仕様3節 transaction_history_service、28節)。

全件成功/全件失敗ではなく、行単位で結果を返す(csv_import_serviceと同じ方針)。
"""

from __future__ import annotations

import csv
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from jstock_advisor.domain.entities.enums import AccountType, TransactionType
from jstock_advisor.infrastructure.external_value_parser import ExternalValueParser
from jstock_advisor.services.transaction_history_service import TransactionHistoryService

REQUIRED_COLUMNS = {"stock_code", "transaction_type", "execution_date", "shares", "execution_price"}


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


class TransactionCsvImportService:
    def __init__(
        self, transaction_history_service: TransactionHistoryService | None = None
    ) -> None:
        self._service = transaction_history_service or TransactionHistoryService()

    def import_file(self, path: Path) -> CsvImportSummary:
        summary = CsvImportSummary()
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise ValueError("CSVにヘッダー行がありません")
            missing = REQUIRED_COLUMNS - set(reader.fieldnames)
            if missing:
                raise ValueError(f"CSVに必須列がありません: {sorted(missing)}")

            for row_number, row in enumerate(reader, start=2):  # 1行目はヘッダー
                summary.add(self._process_row(row_number, row))
        return summary

    def _process_row(self, row_number: int, row: dict[str, str | None]) -> CsvImportRowResult:
        stock_code = ExternalValueParser.stock_code(row.get("stock_code"))
        if stock_code is None:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=None,
                message="銘柄コードが未指定です",
            )

        type_raw = (row.get("transaction_type") or "").strip().upper()
        try:
            transaction_type = TransactionType(type_raw)
        except ValueError:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message=f"transaction_typeが不正です: {type_raw}",
            )

        execution_date = ExternalValueParser.date(row.get("execution_date"))
        if execution_date is None:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message="execution_dateはYYYY-MM-DD形式で指定してください",
            )

        shares = ExternalValueParser.integer(row.get("shares"))
        if shares is None or shares <= 0:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message="sharesは正の整数である必要があります",
            )

        execution_price = ExternalValueParser.decimal(row.get("execution_price"))
        if execution_price is None or execution_price <= 0:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message="execution_priceは正の数値である必要があります",
            )

        messages: list[str] = []

        fee_raw = (row.get("fee") or "").strip()
        fee = ExternalValueParser.decimal(fee_raw) if fee_raw else Decimal("0")
        if fee is None:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message="feeは数値で指定してください",
            )

        tax_raw = (row.get("tax") or "").strip()
        tax = ExternalValueParser.decimal(tax_raw) if tax_raw else Decimal("0")
        if tax is None:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message="taxは数値で指定してください",
            )

        account_type_raw = (row.get("account_type") or "").strip().upper()
        account_type: AccountType | None = None
        if account_type_raw:
            try:
                account_type = AccountType(account_type_raw)
            except ValueError:
                return CsvImportRowResult(
                    row_number=row_number,
                    status=CsvRowStatus.ERROR,
                    stock_code=stock_code,
                    message=f"account_typeが不正です: {account_type_raw}",
                )

        recommendation_id = (row.get("recommendation_id") or "").strip() or None
        reason = (row.get("execution_reason") or "").strip() or None
        memo = (row.get("memo") or "").strip() or None

        try:
            self._service.record_execution(
                stock_code=stock_code,
                transaction_type=transaction_type,
                shares=shares,
                execution_price=execution_price,
                execution_date=execution_date,
                recommendation_id=recommendation_id,
                fee=fee,
                tax=tax,
                account_type=account_type,
                reason=reason,
                memo=memo,
            )
        except ValueError as e:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message=str(e),
            )

        status = CsvRowStatus.WARNING if messages else CsvRowStatus.SUCCESS
        return CsvImportRowResult(
            row_number=row_number,
            status=status,
            stock_code=stock_code,
            message="; ".join(messages) or "登録成功",
        )
