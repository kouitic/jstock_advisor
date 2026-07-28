import datetime as dt

import pytest

from jstock_advisor.domain.entities.enums import RecordDateUnknownReason
from jstock_advisor.providers.dividend_data.yfinance_impl import YFinanceDividendDataProvider

_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)


class _FakeTicker:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.info = {"regularMarketPrice": 1000, "dividendRate": 50}
        self.dividends: dict[object, float] = {}


def test_get_dividend_info_marks_record_date_as_permanently_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jstock_advisor.providers.dividend_data.yfinance_impl as module

    monkeypatch.setattr(module.yf, "Ticker", _FakeTicker)
    provider = YFinanceDividendDataProvider(now=_NOW)

    info = provider.get_dividend_info("7203")

    assert info is not None
    assert info.dividend_record_date is None
    assert info.dividend_ex_date is None
    assert info.dividend_record_date_unknown_reason == RecordDateUnknownReason.DATA_PROVIDER_MISSING
