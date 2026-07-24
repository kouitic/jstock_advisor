import datetime as dt
from pathlib import Path

from jstock_advisor.infrastructure.edinet.document_finder import (
    EdinetFilingCacheRepository,
    find_latest_filings,
)

_STOCK_CODE = "8136"
_SEC_CODE = "81360"


class FakeEdinetClient:
    def __init__(
        self,
        documents_by_date: dict[dt.date, list[dict[str, object]]],
        configured: bool = True,
    ) -> None:
        self._documents_by_date = documents_by_date
        self._configured = configured

    @property
    def is_configured(self) -> bool:
        return self._configured

    def list_documents(self, date: dt.date) -> list[dict[str, object]]:
        return self._documents_by_date.get(date, [])

    def download_document_zip(self, doc_id: str) -> bytes | None:
        return None


def _annual_report_entry(filer_name: str | None = "株式会社サンリオ") -> dict[str, object]:
    return {
        "docID": "DOC1",
        "secCode": _SEC_CODE,
        "docTypeCode": "120",
        "periodEnd": "2026-03-31",
        "filerName": filer_name,
    }


def test_not_configured_returns_none(tmp_path: Path) -> None:
    client = FakeEdinetClient({}, configured=False)
    repo = EdinetFilingCacheRepository(store_dir=tmp_path)
    now = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)
    assert find_latest_filings(client, repo, _STOCK_CODE, now, initial_lookback_days=10) is None


def test_captures_filer_name_from_matching_document(tmp_path: Path) -> None:
    doc_date = dt.date(2026, 7, 20)
    client = FakeEdinetClient({doc_date: [_annual_report_entry()]})
    repo = EdinetFilingCacheRepository(store_dir=tmp_path)
    now = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)

    cache = find_latest_filings(client, repo, _STOCK_CODE, now, initial_lookback_days=10)
    assert cache is not None
    assert cache.filer_name == "株式会社サンリオ"
    assert cache.latest_annual_doc_id == "DOC1"


def test_filer_name_persists_once_captured(tmp_path: Path) -> None:
    day1 = dt.date(2026, 7, 20)
    day2 = dt.date(2026, 7, 22)
    client = FakeEdinetClient(
        {
            day1: [_annual_report_entry()],
            day2: [_annual_report_entry(filer_name="別名(無視されるはず)")],
        }
    )
    repo = EdinetFilingCacheRepository(store_dir=tmp_path)

    first_now = dt.datetime(2026, 7, 21, tzinfo=dt.UTC)
    find_latest_filings(client, repo, _STOCK_CODE, first_now, initial_lookback_days=10)

    second_now = dt.datetime(2026, 7, 23, tzinfo=dt.UTC)
    cache = find_latest_filings(client, repo, _STOCK_CODE, second_now, initial_lookback_days=10)
    assert cache is not None
    assert cache.filer_name == "株式会社サンリオ"


def test_no_matching_document_leaves_filer_name_none(tmp_path: Path) -> None:
    client = FakeEdinetClient({})
    repo = EdinetFilingCacheRepository(store_dir=tmp_path)
    now = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)

    cache = find_latest_filings(client, repo, _STOCK_CODE, now, initial_lookback_days=10)
    assert cache is not None
    assert cache.filer_name is None
