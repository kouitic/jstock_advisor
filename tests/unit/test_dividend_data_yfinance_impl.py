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


def test_inferred_decrease_never_sets_official_dividend_cut_announced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # yfinance単独の年間配当合計比較から推測される減少は、あくまでinferredであり、
    # official_dividend_cut_announced(一次情報での公式発表)には絶対にしない(要求仕様§11・§12)。
    import jstock_advisor.providers.dividend_data.yfinance_impl as module

    class _TickerWithDividends(_FakeTicker):
        def __init__(self, symbol: str) -> None:
            super().__init__(symbol)
            self.info = {"regularMarketPrice": 1000, "dividendRate": 50}
            self.dividends = {
                dt.datetime(2024, 6, 27): 50.0,
                dt.datetime(2024, 12, 27): 50.0,
                dt.datetime(2025, 6, 27): 30.0,
                dt.datetime(2025, 12, 29): 30.0,
            }

    monkeypatch.setattr(module.yf, "Ticker", _TickerWithDividends)
    provider = YFinanceDividendDataProvider(now=_NOW)

    info = provider.get_dividend_info("4631")

    assert info is not None
    assert info.inferred_dividend_decrease is True
    assert info.official_dividend_cut_announced is False
    assert info.dividend_breakdown_confirmed is False
