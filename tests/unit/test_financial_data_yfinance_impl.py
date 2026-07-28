import datetime as dt
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from jstock_advisor.infrastructure.edinet.document_finder import EdinetFilingCacheRepository
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


def test_resolve_japanese_stock_name_returns_none_without_edinet_client() -> None:
    provider = YFinanceFinancialDataProvider(now=_NOW)
    assert provider._resolve_japanese_stock_name("8136") is None  # noqa: SLF001


def test_resolve_japanese_stock_name_returns_none_when_edinet_not_configured(
    tmp_path: Path,
) -> None:
    provider = YFinanceFinancialDataProvider(
        now=_NOW,
        edinet_client=_NotConfiguredClient(),  # type: ignore[arg-type]
        edinet_cache_repository=EdinetFilingCacheRepository(store_dir=tmp_path),
    )
    assert provider._resolve_japanese_stock_name("8136") is None  # noqa: SLF001


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
