"""dividend_data_provider の yfinance実装。

yfinanceは配当の「権利確定日」を提供しないため(取得できるのは支払日のみ)、
dividend_record_datesは常に空リストとする(推測で補完しない)。年間配当実績は
支払履歴(dividends)を暦年で集計した近似値であり、決算期と暦年がずれる銘柄では
実際の期別配当と多少ずれる可能性がある。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any

import yfinance as yf

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.interfaces.types import DividendInfo

_PROVIDER_NAME = "yfinance"
_TICKER_SUFFIX = ".T"


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    try:
        return Decimal(str(round(f, 2)))
    except InvalidOperation:
        return None


class YFinanceDividendDataProvider:
    def __init__(self, now: dt.datetime | None = None) -> None:
        self._now = now or dt.datetime.now(dt.UTC)

    def _source(self) -> DataSourceReference:
        return DataSourceReference(provider=_PROVIDER_NAME, fetched_at=self._now)

    def get_dividend_info(self, stock_code: str) -> DividendInfo | None:
        ticker = yf.Ticker(f"{stock_code}{_TICKER_SUFFIX}")
        try:
            info: dict[str, Any] = ticker.info or {}
        except Exception:  # noqa: BLE001 - 非公式ライブラリのため例外種別を限定できない
            info = {}

        if not info or info.get("regularMarketPrice") is None:
            return None

        try:
            dividends = ticker.dividends
        except Exception:  # noqa: BLE001
            dividends = None

        yearly_totals = self._sum_by_calendar_year(dividends)
        actual_annual = None
        previous_annual = None
        consecutive_increase_years = None
        if yearly_totals:
            years_sorted = sorted(yearly_totals.keys())
            complete_years = [y for y in years_sorted if y < self._now.year]
            if complete_years:
                actual_annual = Decimal(str(round(yearly_totals[complete_years[-1]], 2)))
                if len(complete_years) >= 2:
                    previous_annual = Decimal(str(round(yearly_totals[complete_years[-2]], 2)))
                consecutive_increase_years = self._count_consecutive_increases(
                    [yearly_totals[y] for y in complete_years]
                )

        forecast_annual = _to_decimal(info.get("dividendRate"))
        if forecast_annual is None:
            forecast_annual = _to_decimal(info.get("trailingAnnualDividendRate"))

        is_dividend_cut_announced = False
        is_dividend_omission_announced = False
        if forecast_annual is not None and actual_annual is not None:
            if forecast_annual == 0 and actual_annual > 0:
                is_dividend_omission_announced = True
            elif forecast_annual < actual_annual:
                is_dividend_cut_announced = True

        return DividendInfo(
            stock_code=stock_code,
            fiscal_year=str(self._now.year),
            forecast_annual_dividend_per_share=forecast_annual,
            actual_annual_dividend_per_share=actual_annual,
            previous_fiscal_year_dividend_per_share=previous_annual,
            is_dividend_cut_announced=is_dividend_cut_announced,
            is_dividend_omission_announced=is_dividend_omission_announced,
            is_progressive_or_doe_policy=False,  # yfinanceからは判定不可(既知の限界)
            dividend_policy_note=None,
            dividend_record_dates=[],  # yfinanceは支払日のみ提供、権利確定日は取得不可
            consecutive_dividend_increase_years=consecutive_increase_years,
            source=self._source(),
        )

    @staticmethod
    def _sum_by_calendar_year(dividends: Any) -> dict[int, float]:
        if dividends is None or len(dividends) == 0:
            return {}
        totals: dict[int, float] = {}
        for index, value in dividends.items():
            year = index.year if hasattr(index, "year") else None
            if year is None:
                continue
            totals[year] = totals.get(year, 0.0) + float(value)
        return totals

    @staticmethod
    def _count_consecutive_increases(values_oldest_to_newest: list[float]) -> int:
        count = 0
        for i in range(len(values_oldest_to_newest) - 1, 0, -1):
            if values_oldest_to_newest[i] > values_oldest_to_newest[i - 1]:
                count += 1
            else:
                break
        return count
