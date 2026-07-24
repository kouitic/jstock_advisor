import datetime as dt
from pathlib import Path

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
