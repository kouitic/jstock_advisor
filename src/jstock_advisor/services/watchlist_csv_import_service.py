"""ウォッチリストCSV一括登録サービス(要求仕様3節・6節)。

1行 = 1銘柄として扱う。既存登録があれば上書きされる(WatchlistService.add_itemと同じ挙動)。
"""

from __future__ import annotations

import csv
import re
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from jstock_advisor.domain.entities.enums import Priority
from jstock_advisor.services.watchlist_service import WatchlistService

REQUIRED_COLUMNS = {"stock_code"}
OPTIONAL_COLUMNS = {
    "stock_name",
    "reason",
    "desired_total_yield_pct",
    "desired_buy_price",
    "benefit_interest",
    "priority",
    "notify_enabled",
    "memo",
}
ALL_COLUMNS = REQUIRED_COLUMNS | OPTIONAL_COLUMNS

_STOCK_CODE_PATTERN = re.compile(r"^[0-9A-Za-z]{4}$")


class CsvRowStatus(StrEnum):
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"


class CsvImportRowResult(BaseModel):
    row_number: int
    status: CsvRowStatus
    stock_code: str | None
    message: str


class CsvImportSummary(BaseModel):
    total_rows: int = 0
    success_count: int = 0
    error_count: int = 0
    results: list[CsvImportRowResult] = []

    def add(self, result: CsvImportRowResult) -> None:
        self.results.append(result)
        self.total_rows += 1
        if result.status == CsvRowStatus.SUCCESS:
            self.success_count += 1
        else:
            self.error_count += 1


def _error(row_number: int, stock_code: str | None, message: str) -> CsvImportRowResult:
    return CsvImportRowResult(
        row_number=row_number, status=CsvRowStatus.ERROR, stock_code=stock_code, message=message
    )


class WatchlistCsvImportService:
    def __init__(self, watchlist_service: WatchlistService | None = None) -> None:
        self._watchlist = watchlist_service or WatchlistService()

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
        stock_code = (row.get("stock_code") or "").strip()
        if not stock_code or not _STOCK_CODE_PATTERN.match(stock_code):
            return _error(row_number, stock_code or None, "銘柄コードが不正です")

        desired_yield_raw = (row.get("desired_total_yield_pct") or "").strip()
        desired_yield: float | None = None
        if desired_yield_raw:
            try:
                desired_yield = float(desired_yield_raw)
            except ValueError:
                return _error(
                    row_number, stock_code, "desired_total_yield_pctは数値で指定してください"
                )

        desired_price_raw = (row.get("desired_buy_price") or "").strip()
        desired_price: Decimal | None = None
        if desired_price_raw:
            try:
                desired_price = Decimal(desired_price_raw)
            except InvalidOperation:
                return _error(row_number, stock_code, "desired_buy_priceは数値で指定してください")

        priority_raw = (row.get("priority") or "").strip().upper()
        priority = Priority.MEDIUM
        if priority_raw:
            try:
                priority = Priority(priority_raw)
            except ValueError:
                return _error(row_number, stock_code, f"priorityが不正です: {priority_raw}")

        benefit_interest_raw = (row.get("benefit_interest") or "").strip().lower()
        notify_raw = (row.get("notify_enabled") or "").strip().lower()

        self._watchlist.add_item(
            stock_code=stock_code,
            stock_name=(row.get("stock_name") or "").strip() or None,
            reason=(row.get("reason") or "").strip() or None,
            desired_total_yield_pct=desired_yield,
            desired_buy_price=desired_price,
            benefit_interest=benefit_interest_raw in ("true", "1", "yes"),
            priority=priority,
            notify_enabled=notify_raw not in ("false", "0", "no"),
            memo=(row.get("memo") or "").strip() or None,
        )

        return CsvImportRowResult(
            row_number=row_number,
            status=CsvRowStatus.SUCCESS,
            stock_code=stock_code,
            message="登録成功",
        )
