import datetime as dt

import pandas as pd
import pytest

from jstock_advisor.providers.market_data.yfinance_impl import YFinanceMarketDataProvider

_NOW = dt.datetime(2026, 7, 28, tzinfo=dt.UTC)


class _FakeTicker:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    def history(self, **kwargs: object) -> pd.DataFrame:
        index = pd.to_datetime(["2026-07-24", "2026-07-27"])
        return pd.DataFrame(
            {
                "Open": [1000.0, float("nan")],
                "High": [1010.0, float("nan")],
                "Low": [990.0, float("nan")],
                "Close": [1005.0, float("nan")],
                "Volume": [10000, float("nan")],
            },
            index=index,
        )


def test_fetch_history_skips_rows_with_nan_values(monkeypatch: pytest.MonkeyPatch) -> None:
    import jstock_advisor.providers.market_data.yfinance_impl as module

    monkeypatch.setattr(module.yf, "Ticker", _FakeTicker)
    provider = YFinanceMarketDataProvider(now=_NOW)

    history = provider._fetch_history("7203.T", dt.date(2026, 7, 20), dt.date(2026, 7, 28))

    assert history is not None
    assert len(history.bars) == 1
    assert history.bars[0].date == dt.date(2026, 7, 24)


def test_get_latest_price_ignores_trailing_nan_row(monkeypatch: pytest.MonkeyPatch) -> None:
    import jstock_advisor.providers.market_data.yfinance_impl as module

    monkeypatch.setattr(module.yf, "Ticker", _FakeTicker)
    provider = YFinanceMarketDataProvider(now=_NOW)

    snapshot = provider.get_latest_price("7203")

    assert snapshot is not None
    assert snapshot.close_price == pytest.approx(1005.0)
