import datetime as dt
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from jstock_advisor.domain.entities.enums import RecentPeriodsSource
from jstock_advisor.infrastructure.edinet.document_finder import EdinetFilingCacheRepository
from jstock_advisor.infrastructure.local_repository.stock_name_override_repository import (
    StockNameOverrideRepository,
)
from jstock_advisor.providers.financial_data.yfinance_impl import (
    YFinanceFinancialDataProvider,
    _strip_corporate_suffix,
)

_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("株式会社サンリオ", "サンリオ"),
        ("新明和工業株式会社", "新明和工業"),
        ("トヨタ自動車", "トヨタ自動車"),
    ],
)
def test_strip_corporate_suffix(raw: str, expected: str) -> None:
    assert _strip_corporate_suffix(raw) == expected


class _NotConfiguredClient:
    is_configured = False

    def list_documents(self, date: dt.date) -> list[dict[str, object]]:
        return []

    def download_document_zip(self, doc_id: str) -> bytes | None:
        return None


def test_resolve_japanese_stock_name_returns_none_without_edinet_client(
    tmp_path: Path,
) -> None:
    provider = YFinanceFinancialDataProvider(
        now=_NOW,
        stock_name_override_repository=StockNameOverrideRepository(store_dir=tmp_path),
    )
    assert provider._resolve_japanese_stock_name("8136") is None  # noqa: SLF001


def test_resolve_japanese_stock_name_returns_none_when_edinet_not_configured(
    tmp_path: Path,
) -> None:
    provider = YFinanceFinancialDataProvider(
        now=_NOW,
        edinet_client=_NotConfiguredClient(),  # type: ignore[arg-type]
        edinet_cache_repository=EdinetFilingCacheRepository(store_dir=tmp_path),
        stock_name_override_repository=StockNameOverrideRepository(store_dir=tmp_path),
    )
    assert provider._resolve_japanese_stock_name("8136") is None  # noqa: SLF001


def test_resolve_japanese_stock_name_prefers_manual_override_over_edinet(
    tmp_path: Path,
) -> None:
    # BUYパイプライン第2次修正(2026-07)。要求仕様19節: 手動オーバーライドは
    # EDINET filerNameより優先される。
    override_repo = StockNameOverrideRepository(store_dir=tmp_path)
    override_repo.save("4246", "ダイキョーニシカワ")
    provider = YFinanceFinancialDataProvider(
        now=_NOW,
        edinet_client=_NotConfiguredClient(),  # type: ignore[arg-type]
        edinet_cache_repository=EdinetFilingCacheRepository(store_dir=tmp_path),
        stock_name_override_repository=override_repo,
    )
    assert provider._resolve_japanese_stock_name("4246") == "ダイキョーニシカワ"  # noqa: SLF001


def test_nearest_price_picks_closest_trading_day() -> None:
    index = pd.to_datetime(["2026-03-30", "2026-03-31", "2026-04-01"])
    price_history = pd.DataFrame({"Close": [1000.0, 1010.0, 1020.0]}, index=index)
    price = YFinanceFinancialDataProvider._nearest_price(  # noqa: SLF001
        price_history, dt.date(2026, 3, 31)
    )
    assert price == Decimal("1010")


def test_nearest_price_returns_none_when_no_date_within_window() -> None:
    index = pd.to_datetime(["2026-01-01"])
    price_history = pd.DataFrame({"Close": [1000.0]}, index=index)
    price = YFinanceFinancialDataProvider._nearest_price(  # noqa: SLF001
        price_history, dt.date(2026, 3, 31)
    )
    assert price is None


def test_nearest_price_returns_none_for_empty_history() -> None:
    price = YFinanceFinancialDataProvider._nearest_price(None, dt.date(2026, 3, 31))  # noqa: SLF001
    assert price is None


# ===== デプロイ前対応: 年次期末取得不能時に取得日時を代替値として使わないこと =====


class _FakeTickerFinancials:
    def __init__(
        self,
        symbol: str,
        income_stmt: pd.DataFrame | None = None,
        quarterly_income_stmt: pd.DataFrame | None = None,
        cashflow: pd.DataFrame | None = None,
        quarterly_cashflow: pd.DataFrame | None = None,
        balance_sheet: pd.DataFrame | None = None,
        quarterly_balance_sheet: pd.DataFrame | None = None,
        info: dict[str, object] | None = None,
    ) -> None:
        self.symbol = symbol
        self.income_stmt = income_stmt if income_stmt is not None else pd.DataFrame()
        self.quarterly_income_stmt = (
            quarterly_income_stmt if quarterly_income_stmt is not None else pd.DataFrame()
        )
        self.cashflow = cashflow if cashflow is not None else pd.DataFrame()
        self.quarterly_cashflow = (
            quarterly_cashflow if quarterly_cashflow is not None else pd.DataFrame()
        )
        self.balance_sheet = balance_sheet if balance_sheet is not None else pd.DataFrame()
        self.quarterly_balance_sheet = (
            quarterly_balance_sheet if quarterly_balance_sheet is not None else pd.DataFrame()
        )
        self.info = info if info is not None else {"regularMarketPrice": 1000.0}


def test_get_financial_summary_leaves_fiscal_period_end_none_when_annual_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """デプロイ前対応の回帰: 年次決算期末(income_stmt)を取得できない場合、
    データ取得日時(self._now)を代替のfiscal_period_endとして使わない(None)。
    """
    import jstock_advisor.providers.financial_data.yfinance_impl as module

    fake_ticker = _FakeTickerFinancials("7203.T")  # income_stmt空(取得不能を模擬)
    monkeypatch.setattr(module.yf, "Ticker", lambda symbol: fake_ticker)
    provider = YFinanceFinancialDataProvider(
        now=_NOW, stock_name_override_repository=StockNameOverrideRepository(store_dir=tmp_path)
    )

    summary = provider.get_financial_summary("7203")

    assert summary is not None
    assert summary.fiscal_period_end is None


def test_get_financial_summary_recent_quarters_reflect_newer_quarterly_period(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """四半期(quarterly_income_stmt)に年次(income_stmt)より新しい列がある場合、
    recent_quartersの最新quarter_endはその日付を反映する(fiscal_period_end
    (年次)自体は変わらない)。
    """
    import jstock_advisor.providers.financial_data.yfinance_impl as module

    annual_income_stmt = pd.DataFrame({pd.Timestamp("2026-03-31"): {"Operating Income": 1000.0}})
    quarterly_income_stmt = pd.DataFrame(
        {
            pd.Timestamp("2026-03-31"): {"Operating Income": 300.0},
            pd.Timestamp("2026-06-30"): {"Operating Income": 320.0},
        }
    )
    fake_ticker = _FakeTickerFinancials(
        "7203.T", income_stmt=annual_income_stmt, quarterly_income_stmt=quarterly_income_stmt
    )
    monkeypatch.setattr(module.yf, "Ticker", lambda symbol: fake_ticker)
    provider = YFinanceFinancialDataProvider(
        now=_NOW, stock_name_override_repository=StockNameOverrideRepository(store_dir=tmp_path)
    )

    summary = provider.get_financial_summary("7203")

    assert summary is not None
    assert summary.fiscal_period_end == dt.date(2026, 3, 31)
    quarter_ends = [q.quarter_end for q in summary.recent_quarters]
    assert max(quarter_ends) == dt.date(2026, 6, 30)
    assert summary.recent_periods_source == RecentPeriodsSource.QUARTERLY


def test_get_financial_summary_recent_periods_source_is_annual_fallback_when_quarterly_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """由来精緻化対応: quarterly_income_stmtが取得できず年次income_stmtへ
    フォールバックした場合、recent_periods_sourceはANNUAL_FALLBACKとなり、
    QUARTERLYと誤表示しない。
    """
    import jstock_advisor.providers.financial_data.yfinance_impl as module

    annual_income_stmt = pd.DataFrame(
        {
            pd.Timestamp("2025-03-31"): {"Operating Income": 900.0},
            pd.Timestamp("2026-03-31"): {"Operating Income": 1000.0},
        }
    )
    fake_ticker = _FakeTickerFinancials("7203.T", income_stmt=annual_income_stmt)
    monkeypatch.setattr(module.yf, "Ticker", lambda symbol: fake_ticker)
    provider = YFinanceFinancialDataProvider(
        now=_NOW, stock_name_override_repository=StockNameOverrideRepository(store_dir=tmp_path)
    )

    summary = provider.get_financial_summary("7203")

    assert summary is not None
    assert summary.recent_periods_source == RecentPeriodsSource.ANNUAL_FALLBACK
    quarter_ends = [q.quarter_end for q in summary.recent_quarters]
    assert max(quarter_ends) == dt.date(2026, 3, 31)


def test_recent_periods_skips_columns_without_valid_date() -> None:
    """デプロイ前対応の回帰: 列(period)が実際の日付を持たない場合、その期を
    スキップする(データ取得日時self._now.date()で代替しない)。
    """
    quarterly_income_stmt = pd.DataFrame(
        {"period-1": {"Operating Income": 100.0}, "period-2": {"Operating Income": 200.0}}
    )
    fake_ticker = _FakeTickerFinancials("7203.T", quarterly_income_stmt=quarterly_income_stmt)
    provider = YFinanceFinancialDataProvider(now=_NOW)

    results = provider._recent_periods(fake_ticker, "7203")  # noqa: SLF001

    assert results.periods == []
    assert results.source == RecentPeriodsSource.UNAVAILABLE
