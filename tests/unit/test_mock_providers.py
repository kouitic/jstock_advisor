import datetime as dt

from jstock_advisor.providers.corporate_action.mock_impl import MockCorporateActionProvider
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.dividend_data.mock_impl import MockDividendDataProvider
from jstock_advisor.providers.financial_data.mock_impl import MockFinancialDataProvider
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.news.mock_impl import MockNewsProvider
from jstock_advisor.providers.shareholder_benefit.mock_impl import MockShareholderBenefitProvider

_NOW = dt.datetime(2026, 7, 24, 7, 0, tzinfo=dt.UTC)


def test_market_data_provider_returns_none_for_unknown_stock() -> None:
    provider = MockMarketDataProvider(now=_NOW)
    assert provider.get_latest_price("0000") is None
    assert provider.get_price_history("0000", dt.date(2026, 1, 1), dt.date(2026, 7, 1)) is None
    assert provider.get_average_trading_value("0000", 20) is None


def test_market_data_provider_price_series_is_deterministic() -> None:
    a = MockMarketDataProvider(now=_NOW).get_latest_price("8136")
    b = MockMarketDataProvider(now=_NOW).get_latest_price("8136")
    assert a is not None and b is not None
    assert a.close_price == b.close_price
    assert a.as_of_date == b.as_of_date


def test_market_data_provider_history_within_requested_range() -> None:
    provider = MockMarketDataProvider(now=_NOW)
    start, end = dt.date(2026, 6, 1), dt.date(2026, 6, 30)
    history = provider.get_price_history("8136", start, end)
    assert history is not None
    assert all(start <= bar.date <= end for bar in history.bars)


def test_financial_data_provider_returns_summary_with_source() -> None:
    provider = MockFinancialDataProvider(now=_NOW)
    summary = provider.get_financial_summary("8136")
    assert summary is not None
    assert summary.source.provider == "mock_financial_data"
    assert summary.equity_ratio_pct is not None


def test_financial_data_provider_unknown_stock_returns_none() -> None:
    provider = MockFinancialDataProvider(now=_NOW)
    assert provider.get_financial_summary("0000") is None
    assert provider.get_historical_valuation("0000", years=3) == []


def test_dividend_data_provider_returns_forecast() -> None:
    provider = MockDividendDataProvider(now=_NOW)
    info = provider.get_dividend_info("2914")
    assert info is not None
    assert info.forecast_annual_dividend_per_share is not None
    assert info.is_dividend_cut_announced is False


def test_shareholder_benefit_provider_none_when_no_benefit() -> None:
    provider = MockShareholderBenefitProvider(now=_NOW)
    assert provider.get_shareholder_benefit("2914") is None  # JTは優待なしのフィクスチャ


def test_shareholder_benefit_provider_returns_benefit_when_present() -> None:
    provider = MockShareholderBenefitProvider(now=_NOW)
    benefit = provider.get_shareholder_benefit("8136")
    assert benefit is not None
    assert benefit.benefits[0].estimated_value is not None


def test_disclosure_provider_next_earnings_date() -> None:
    provider = MockDisclosureProvider(now=_NOW)
    assert provider.get_next_earnings_date("8136") is not None
    assert provider.get_next_earnings_date("0000") is None


def test_corporate_action_provider_empty_by_default() -> None:
    assert MockCorporateActionProvider().get_corporate_actions("8136", dt.date(2020, 1, 1)) == []


def test_news_provider_empty_by_default() -> None:
    assert MockNewsProvider().get_news("8136", dt.date(2020, 1, 1)) == []
