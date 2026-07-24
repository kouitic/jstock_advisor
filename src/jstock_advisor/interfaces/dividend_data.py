"""dividend_data_provider インターフェース。"""

from __future__ import annotations

from typing import Protocol

from jstock_advisor.interfaces.types import DividendInfo


class DividendDataProvider(Protocol):
    def get_dividend_info(self, stock_code: str) -> DividendInfo | None:
        """予想配当・実績配当・減配/無配転落発表の有無等を取得する。取得できなければNone。"""
        ...
