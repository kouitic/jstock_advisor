"""market_data_provider のモック実装(開発・テスト用の合成データ)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.interfaces.types import PriceBar, PriceHistory, PriceSnapshot
from jstock_advisor.providers.mock_fixtures import get_benchmark_series, get_price_volume_series

_PROVIDER_NAME = "mock_market_data"


class MockMarketDataProvider:
    def __init__(self, now: dt.datetime | None = None) -> None:
        self._now = now or dt.datetime.now(dt.UTC)

    def _source(self) -> DataSourceReference:
        return DataSourceReference(provider=_PROVIDER_NAME, fetched_at=self._now)

    def get_latest_price(self, stock_code: str) -> PriceSnapshot | None:
        series = get_price_volume_series(stock_code)
        if not series:
            return None
        latest_date = max(d for d in series if d <= self._now.date())
        close, volume = series[latest_date]
        return PriceSnapshot(
            stock_code=stock_code,
            as_of_date=latest_date,
            close_price=Decimal(str(close)),
            volume=volume,
            source=self._source(),
        )

    def get_price_history(
        self, stock_code: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        series = get_price_volume_series(stock_code)
        if not series:
            return None
        bars = [
            PriceBar(
                date=d,
                open=Decimal(str(close)),
                high=Decimal(str(round(close * 1.01, 1))),
                low=Decimal(str(round(close * 0.99, 1))),
                close=Decimal(str(close)),
                volume=volume,
            )
            for d, (close, volume) in sorted(series.items())
            if start <= d <= end
        ]
        if not bars:
            return None
        return PriceHistory(symbol=stock_code, bars=bars, source=self._source())

    def get_average_trading_value(self, stock_code: str, business_days: int) -> Decimal | None:
        series = get_price_volume_series(stock_code)
        if not series:
            return None
        recent = sorted((d for d in series if d <= self._now.date()), reverse=True)[:business_days]
        if not recent:
            return None
        values = [Decimal(str(series[d][0])) * series[d][1] for d in recent]
        return sum(values, start=Decimal("0")) / len(values)

    def get_benchmark_price_history(
        self, symbol: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        series = get_benchmark_series(symbol)
        if not series:
            return None
        bars = [
            PriceBar(
                date=d,
                open=Decimal(str(value)),
                high=Decimal(str(round(value * 1.005, 2))),
                low=Decimal(str(round(value * 0.995, 2))),
                close=Decimal(str(value)),
                volume=0,
            )
            for d, value in sorted(series.items())
            if start <= d <= end
        ]
        if not bars:
            return None
        return PriceHistory(symbol=symbol, bars=bars, source=self._source())
