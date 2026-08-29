import datetime as dt
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import SourceType
from jstock_advisor.infrastructure.edinet.disclosure_finder import (
    EdinetDisclosureCache,
    EdinetDisclosureCacheRepository,
    EdinetDisclosureRecord,
)
from jstock_advisor.infrastructure.edinet.types import (
    EdinetDownloadResult,
    EdinetFailureReason,
    EdinetFetchStatus,
    EdinetListResult,
)
from jstock_advisor.providers.disclosure.edinet_yfinance_impl import (
    EdinetYfinanceDisclosureProvider,
)

_STOCK_CODE = "2914"
_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)


class _NotConfiguredSource:
    is_configured = False

    def list_documents(self, scan_date: dt.date, now: dt.datetime) -> EdinetListResult:
        return EdinetListResult(
            EdinetFetchStatus.FETCH_FAILED, [], EdinetFailureReason.NOT_CONFIGURED
        )

    def download_document_zip(self, doc_id: str) -> EdinetDownloadResult:
        return EdinetDownloadResult(
            EdinetFetchStatus.FETCH_FAILED, None, EdinetFailureReason.NOT_CONFIGURED
        )


def test_get_disclosures_returns_empty_when_edinet_not_configured(tmp_path: Path) -> None:
    provider = EdinetYfinanceDisclosureProvider(
        document_source=_NotConfiguredSource(),  # type: ignore[arg-type]
        cache_repository=EdinetDisclosureCacheRepository(store_dir=tmp_path),
        now=_NOW,
    )
    assert provider.get_disclosures(_STOCK_CODE, dt.date(2026, 6, 1)) == []


def test_get_disclosures_filters_by_since_date(tmp_path: Path) -> None:
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)
    repo.save(
        EdinetDisclosureCache(
            stock_code=_STOCK_CODE,
            records=[
                EdinetDisclosureRecord(
                    doc_id="OLD", submit_date=dt.date(2026, 5, 1), summary="古い開示"
                ),
                EdinetDisclosureRecord(
                    doc_id="NEW", submit_date=dt.date(2026, 7, 10), summary="新しい開示"
                ),
            ],
            oldest_scanned_date="2026-01-01",
            newest_scanned_date=_NOW.date().isoformat(),
            updated_at=_NOW,
        )
    )

    class _ConfiguredNoOpSource:
        is_configured = True

        def list_documents(self, scan_date: dt.date, now: dt.datetime) -> EdinetListResult:
            return EdinetListResult(EdinetFetchStatus.SUCCESS_EMPTY, [])

        def download_document_zip(self, doc_id: str) -> EdinetDownloadResult:
            return EdinetDownloadResult(
                EdinetFetchStatus.FETCH_FAILED, None, EdinetFailureReason.DOWNLOAD_ERROR
            )

    provider = EdinetYfinanceDisclosureProvider(
        document_source=_ConfiguredNoOpSource(),  # type: ignore[arg-type]
        cache_repository=repo,
        now=_NOW,
    )
    disclosures = provider.get_disclosures(_STOCK_CODE, dt.date(2026, 6, 1))
    assert len(disclosures) == 1
    assert disclosures[0].summary == "新しい開示"
    assert disclosures[0].stock_code == _STOCK_CODE
    assert disclosures[0].source.source_type == SourceType.TDNET_EDINET
    assert disclosures[0].source.primary_source_flag is True


def test_get_next_earnings_date_returns_date_from_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        @property
        def calendar(self) -> dict[str, object]:
            return {"Earnings Date": [dt.date(2026, 8, 4)]}

    import jstock_advisor.providers.disclosure.edinet_yfinance_impl as module

    monkeypatch.setattr(module.yf, "Ticker", _FakeTicker)
    provider = EdinetYfinanceDisclosureProvider(now=_NOW)
    assert provider.get_next_earnings_date("7203") == dt.date(2026, 8, 4)


def test_get_next_earnings_date_returns_none_when_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        @property
        def calendar(self) -> dict[str, object]:
            return {}

    import jstock_advisor.providers.disclosure.edinet_yfinance_impl as module

    monkeypatch.setattr(module.yf, "Ticker", _FakeTicker)
    provider = EdinetYfinanceDisclosureProvider(now=_NOW)
    assert provider.get_next_earnings_date("7203") is None


def test_get_next_earnings_date_returns_none_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RaisingTicker:
        def __init__(self, symbol: str) -> None:
            raise RuntimeError("network error")

    import jstock_advisor.providers.disclosure.edinet_yfinance_impl as module

    monkeypatch.setattr(module.yf, "Ticker", _RaisingTicker)
    provider = EdinetYfinanceDisclosureProvider(now=_NOW)
    assert provider.get_next_earnings_date("7203") is None
