"""株主優待CSV一括登録サービス(要求仕様7節、未確定事項#5)。

1行 = 1優待段階(BenefitDetail)として扱う。同一stock_codeの行は1つの
ShareholderBenefitにまとめる(銘柄レベルの項目 min_shares_required/
frequency_per_year/benefit_record_dates/is_abolished/is_major_downgrade/
change_noteは、その銘柄コードで最初に現れた行の値を採用する)。
"""

from __future__ import annotations

import csv
import datetime as dt
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import BenefitUtilityCategory
from jstock_advisor.infrastructure.local_repository.shareholder_benefit_registry_repository import (
    ShareholderBenefitRegistryRepository,
)
from jstock_advisor.interfaces.types import BenefitDetail, ShareholderBenefit

REQUIRED_COLUMNS = {
    "stock_code",
    "min_shares_required",
    "frequency_per_year",
    "category",
    "description",
    "min_shares_for_tier",
}

_PROVIDER_NAME = "manual_registry_csv"


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


class ShareholderBenefitCsvImportService:
    def __init__(self, repository: ShareholderBenefitRegistryRepository | None = None) -> None:
        self._repo = repository or ShareholderBenefitRegistryRepository()

    def import_file(self, path: Path, now: dt.datetime | None = None) -> CsvImportSummary:
        summary = CsvImportSummary()
        grouped: dict[str, ShareholderBenefit] = {}
        fetched_at = now or dt.datetime.now(dt.UTC)

        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise ValueError("CSVにヘッダー行がありません")
            missing = REQUIRED_COLUMNS - set(reader.fieldnames)
            if missing:
                raise ValueError(f"CSVに必須列がありません: {sorted(missing)}")

            for row_number, row in enumerate(reader, start=2):
                result, benefit = self._process_row(row_number, row, grouped, fetched_at)
                summary.add(result)
                if benefit is not None:
                    grouped[benefit.stock_code] = benefit

        for benefit in grouped.values():
            self._repo.save(benefit)
        return summary

    def _process_row(
        self,
        row_number: int,
        row: dict[str, str | None],
        grouped: dict[str, ShareholderBenefit],
        fetched_at: dt.datetime,
    ) -> tuple[CsvImportRowResult, ShareholderBenefit | None]:
        stock_code = (row.get("stock_code") or "").strip()
        if not stock_code:
            return _error(row_number, None, "銘柄コードが未指定です"), None

        try:
            min_shares_required = int((row.get("min_shares_required") or "").strip())
            if min_shares_required <= 0:
                raise ValueError
        except ValueError:
            return (
                _error(row_number, stock_code, "min_shares_requiredは正の整数である必要があります"),
                None,
            )

        try:
            frequency_per_year = int((row.get("frequency_per_year") or "").strip())
            if frequency_per_year <= 0:
                raise ValueError
        except ValueError:
            return (
                _error(row_number, stock_code, "frequency_per_yearは正の整数である必要があります"),
                None,
            )

        category_raw = (row.get("category") or "").strip().upper()
        try:
            category = BenefitUtilityCategory(category_raw)
        except ValueError:
            return _error(row_number, stock_code, f"categoryが不正です: {category_raw}"), None

        description = (row.get("description") or "").strip()
        if not description:
            return _error(row_number, stock_code, "descriptionが未指定です"), None

        try:
            min_shares_for_tier = int((row.get("min_shares_for_tier") or "").strip())
            if min_shares_for_tier <= 0:
                raise ValueError
        except ValueError:
            return (
                _error(row_number, stock_code, "min_shares_for_tierは正の整数である必要があります"),
                None,
            )

        estimated_value_raw = (row.get("estimated_value") or "").strip()
        estimated_value: Decimal | None = None
        if estimated_value_raw:
            try:
                estimated_value = Decimal(estimated_value_raw)
            except InvalidOperation:
                return (
                    _error(row_number, stock_code, "estimated_valueは数値で指定してください"),
                    None,
                )

        long_term_raw = (row.get("long_term_holding_condition_months") or "").strip()
        long_term_months: int | None = None
        if long_term_raw:
            try:
                long_term_months = int(long_term_raw)
            except ValueError:
                return (
                    _error(
                        row_number,
                        stock_code,
                        "long_term_holding_condition_monthsは整数で指定してください",
                    ),
                    None,
                )

        record_dates_raw = (row.get("benefit_record_dates") or "").strip()
        record_dates: list[dt.date] = []
        if record_dates_raw:
            try:
                record_dates = [
                    dt.date.fromisoformat(d.strip())
                    for d in record_dates_raw.split(";")
                    if d.strip()
                ]
            except ValueError:
                return (
                    _error(
                        row_number,
                        stock_code,
                        "benefit_record_datesはYYYY-MM-DD形式をセミコロン区切りで指定してください",
                    ),
                    None,
                )

        detail = BenefitDetail(
            category=category,
            description=description,
            estimated_value=estimated_value,
            min_shares_for_tier=min_shares_for_tier,
            long_term_holding_condition_months=long_term_months,
        )
        source = DataSourceReference(provider=_PROVIDER_NAME, fetched_at=fetched_at)

        existing = grouped.get(stock_code)
        if existing is not None:
            benefit = existing.model_copy(update={"benefits": [*existing.benefits, detail]})
        else:
            is_abolished_raw = (row.get("is_abolished") or "").strip().lower()
            is_major_downgrade_raw = (row.get("is_major_downgrade") or "").strip().lower()
            benefit = ShareholderBenefit(
                stock_code=stock_code,
                min_shares_required=min_shares_required,
                benefits=[detail],
                frequency_per_year=frequency_per_year,
                benefit_record_dates=record_dates,
                is_abolished=is_abolished_raw in ("true", "1", "yes"),
                is_major_downgrade=is_major_downgrade_raw in ("true", "1", "yes"),
                change_note=(row.get("change_note") or "").strip() or None,
                source=source,
            )

        return (
            CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.SUCCESS,
                stock_code=stock_code,
                message="登録成功",
            ),
            benefit,
        )
