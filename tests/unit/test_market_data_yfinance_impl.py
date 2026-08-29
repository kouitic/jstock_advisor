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


# --- Issue #59 Phase B2: 取得失敗を「データ無し」へ潰さない ---------------------


class _RaisingHistoryTicker:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def history(self, **kwargs: object) -> pd.DataFrame:
        raise self._exc


class _EmptyHistoryTicker:
    def history(self, **kwargs: object) -> pd.DataFrame:
        return pd.DataFrame()


def _patch_ticker(monkeypatch: pytest.MonkeyPatch, ticker: object) -> None:
    import jstock_advisor.providers.market_data.yfinance_impl as module

    monkeypatch.setattr(module.yf, "Ticker", lambda symbol: ticker)


class _RateLimitedError(Exception):
    def __init__(self) -> None:
        super().__init__("429 Too Many Requests")


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("get_latest_price", ("7203",)),
        ("get_price_history", ("7203", dt.date(2026, 7, 1), dt.date(2026, 7, 28))),
        ("get_average_trading_value", ("7203", 20)),
        ("get_benchmark_price_history", ("TOPIX", dt.date(2026, 7, 1), dt.date(2026, 7, 28))),
    ],
)
def test_market_data_failure_raises_provider_data_error(
    monkeypatch: pytest.MonkeyPatch, method: str, args: tuple[object, ...]
) -> None:
    """外部取得失敗をNoneへ潰さずProviderDataErrorとして伝播する。"""
    from jstock_advisor.interfaces.provider_errors import ProviderDataError

    original = _RateLimitedError()
    _patch_ticker(monkeypatch, _RaisingHistoryTicker(original))

    with pytest.raises(ProviderDataError) as excinfo:
        getattr(YFinanceMarketDataProvider(now=_NOW), method)(*args)

    assert excinfo.value.provider_name == "yfinance"
    assert excinfo.value.operation == "history"
    assert excinfo.value.retryable is True, "429は再試行対象として分類されること"
    assert excinfo.value.__cause__ is original


def test_market_data_non_retryable_failure_is_still_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jstock_advisor.interfaces.provider_errors import ProviderDataError

    _patch_ticker(monkeypatch, _RaisingHistoryTicker(KeyError("Close")))

    with pytest.raises(ProviderDataError) as excinfo:
        YFinanceMarketDataProvider(now=_NOW).get_latest_price("7203")

    assert excinfo.value.retryable is False


def test_market_data_empty_history_is_normal_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """応答は成立したがバーが無い場合は従来どおりNone(SUCCESS + empty)。"""
    _patch_ticker(monkeypatch, _EmptyHistoryTicker())
    provider = YFinanceMarketDataProvider(now=_NOW)

    assert provider.get_latest_price("7203") is None
    assert provider.get_price_history("7203", dt.date(2026, 7, 1), dt.date(2026, 7, 28)) is None
    assert provider.get_average_trading_value("7203", 20) is None
