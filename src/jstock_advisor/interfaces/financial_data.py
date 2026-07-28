"""financial_data_provider インターフェース。"""

from __future__ import annotations

from typing import Protocol

from jstock_advisor.interfaces.types import (
    CashflowDecomposition,
    FinancialSummary,
    HistoricalValuation,
)


class FinancialDataProvider(Protocol):
    def get_financial_summary(self, stock_code: str) -> FinancialSummary | None:
        """直近決算に基づく財務サマリを取得する。取得できなければNone。"""
        ...

    def get_historical_valuation(self, stock_code: str, years: int) -> list[HistoricalValuation]:
        """過去N年分のEPS/BPS/株価からPER/PBR算出用の時系列を取得する。データが無ければ空リスト。"""
        ...

    def get_cashflow_decomposition(self, stock_code: str) -> CashflowDecomposition | None:
        """直近期の営業キャッシュフロー要因分解を取得する(要求仕様4節)。
        取得できない項目が多い場合はNoneを返す(推測で補完しない)。"""
        ...
