"""market_data_provider インターフェース。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Protocol

from jstock_advisor.interfaces.types import PriceHistory, PriceSnapshot


class MarketDataProvider(Protocol):
    def get_latest_price(self, stock_code: str) -> PriceSnapshot | None:
        """最新の株価スナップショットを取得する。取得できなければNone。"""
        ...

    def get_price_history(
        self, stock_code: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        """指定期間の日次OHLCVを取得する。取得できなければNone。"""
        ...

    def get_average_trading_value(self, stock_code: str, business_days: int) -> Decimal | None:
        """直近N営業日の平均売買代金(円)を取得する。取得できなければNone。"""
        ...

    def get_benchmark_price_history(
        self, symbol: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        """TOPIX等ベンチマーク指数の日次価格を取得する。取得できなければNone。"""
        ...
