"""dividend_data_provider インターフェース。"""

from __future__ import annotations

from typing import Protocol

from jstock_advisor.interfaces.types import DividendInfo


class DividendDataProvider(Protocol):
    def get_dividend_info(
        self, stock_code: str, fiscal_year_end_month: int | None = None
    ) -> DividendInfo | None:
        """予想配当・実績配当・減配/無配転落発表の有無等を取得する。取得できなければNone。

        fiscal_year_end_monthは企業の正式な決算期末月(FinancialSummary.fiscal_year_end_month
        と同じ意味)。yfinance実装がこれを使って配当支払いイベントを暦年ではなく決算期単位で
        集計する。呼び出し元がFinancialSummaryを持たない場合(未対応の呼び出し経路)はNoneのままでよく、
        その場合は暦年集計へフォールバックする(配当データクロスバリデーション根本修正)。
        """
        ...
