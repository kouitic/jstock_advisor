"""financial_data_provider インターフェース。"""

from __future__ import annotations

from typing import Protocol

from jstock_advisor.interfaces.types import FinancialSummary, HistoricalValuation


class FinancialDataProvider(Protocol):
    def get_financial_summary(self, stock_code: str) -> FinancialSummary | None:
        """直近決算に基づく財務サマリを取得する。取得できなければNone。"""
        ...

    def get_historical_valuation(self, stock_code: str, years: int) -> list[HistoricalValuation]:
        """過去N年分のEPS/BPS/株価からPER/PBR算出用の時系列を取得する。データが無ければ空リスト。"""
        ...
