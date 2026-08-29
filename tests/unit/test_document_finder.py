"""有報・半期報告書のfinder(Issue #53 Phase B1でdisclosure_finderと同規約へ統一)。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from jstock_advisor.infrastructure.edinet.document_finder import (
    EdinetFilingCacheRepository,
    find_latest_filings,
)
from jstock_advisor.infrastructure.edinet.types import (
    EdinetDocumentEntry,
    EdinetDownloadResult,
    EdinetFailureReason,
    EdinetFetchStatus,
    EdinetListResult,
)

_STOCK_CODE = "8136"
_SEC_CODE = "81360"


class FakeDocumentSource:
    def __init__(
        self,
        documents_by_date: dict[dt.date, list[EdinetDocumentEntry]] | None = None,
        failed_dates: set[dt.date] | None = None,
        configured: bool = True,
        refresh_window_days: int = 7,
    ) -> None:
        self._documents_by_date = documents_by_date or {}
        self._failed_dates = failed_dates or set()
        self._configured = configured
        self._refresh_window_days = refresh_window_days
        self.scanned_dates: list[dt.date] = []

    @property
    def is_configured(self) -> bool:
        return self._configured

    @property
    def refresh_window_days(self) -> int:
        return self._refresh_window_days

    def list_documents(self, scan_date: dt.date, now: dt.datetime) -> EdinetListResult:
        del now
        self.scanned_dates.append(scan_date)
        if scan_date in self._failed_dates:
            return EdinetListResult(
                EdinetFetchStatus.FETCH_FAILED, [], EdinetFailureReason.TIMEOUT
            )
        entries = self._documents_by_date.get(scan_date, [])
        status = (
            EdinetFetchStatus.SUCCESS_WITH_DOCUMENTS
            if entries
            else EdinetFetchStatus.SUCCESS_EMPTY
        )
        return EdinetListResult(status, list(entries))

    def download_document_zip(self, doc_id: str) -> EdinetDownloadResult:
        del doc_id
        return EdinetDownloadResult(
            EdinetFetchStatus.FETCH_FAILED, None, EdinetFailureReason.DOWNLOAD_ERROR
        )


def _annual_entry(
    filer_name: str | None = "株式会社サンリオ", doc_id: str = "DOC1"
) -> EdinetDocumentEntry:
    return EdinetDocumentEntry(
        sec_code=_SEC_CODE,
        doc_id=doc_id,
        doc_type_code="120",
        period_end="2026-03-31",
        filer_name=filer_name,
    )


def _find(
    source: FakeDocumentSource,
    repo: EdinetFilingCacheRepository,
    now: dt.datetime,
    initial_lookback_days: int = 10,
):
    return find_latest_filings(
        source,  # type: ignore[arg-type]
        repo,
        _STOCK_CODE,
        now,
        initial_lookback_days=initial_lookback_days,
    )


# --- 既存の回帰 -------------------------------------------------------------


def test_not_configured_returns_none(tmp_path: Path) -> None:
    repo = EdinetFilingCacheRepository(store_dir=tmp_path)
    source = FakeDocumentSource(configured=False)

    assert _find(source, repo, dt.datetime(2026, 7, 24, tzinfo=dt.UTC)) is None


def test_captures_filer_name_from_matching_document(tmp_path: Path) -> None:
    source = FakeDocumentSource({dt.date(2026, 7, 20): [_annual_entry()]})
    cache = _find(
        source,
        EdinetFilingCacheRepository(store_dir=tmp_path),
        dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
    )

    assert cache is not None
    assert cache.filer_name == "株式会社サンリオ"
    assert cache.latest_annual_doc_id == "DOC1"


def test_filer_name_persists_once_captured(tmp_path: Path) -> None:
    source = FakeDocumentSource(
        {
            dt.date(2026, 7, 20): [_annual_entry()],
            dt.date(2026, 7, 22): [_annual_entry(filer_name="別名(無視されるはず)")],
        }
    )
    repo = EdinetFilingCacheRepository(store_dir=tmp_path)

    _find(source, repo, dt.datetime(2026, 7, 21, tzinfo=dt.UTC))
    cache = _find(source, repo, dt.datetime(2026, 7, 23, tzinfo=dt.UTC))

    assert cache is not None
    assert cache.filer_name == "株式会社サンリオ"


def test_no_matching_document_leaves_filer_name_none(tmp_path: Path) -> None:
    cache = _find(
        FakeDocumentSource(),
        EdinetFilingCacheRepository(store_dir=tmp_path),
        dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
    )

    assert cache is not None
    assert cache.filer_name is None


# --- JST/UTC境界 ------------------------------------------------------------


@pytest.mark.parametrize(
    "now",
    [
        dt.datetime(2026, 8, 30, 15, 0, tzinfo=dt.UTC),  # JST 00:00
        dt.datetime(2026, 8, 30, 22, 59, tzinfo=dt.UTC),  # JST 07:59
        dt.datetime(2026, 8, 30, 23, 0, tzinfo=dt.UTC),  # JST 08:00
        dt.datetime(2026, 8, 30, 23, 59, tzinfo=dt.UTC),  # JST 08:59
        dt.datetime(2026, 8, 31, 0, 0, tzinfo=dt.UTC),  # JST 09:00
        dt.datetime(2026, 8, 31, 1, 0, tzinfo=dt.UTC),  # JST 10:00
        dt.datetime(2026, 8, 31, 3, 30, tzinfo=dt.UTC),  # JST 12:30
        dt.datetime(2026, 8, 31, 6, 30, tzinfo=dt.UTC),  # JST 15:30
    ],
)
def test_scan_range_ends_at_jst_today(tmp_path: Path, now: dt.datetime) -> None:
    source = FakeDocumentSource()
    _find(source, EdinetFilingCacheRepository(store_dir=tmp_path), now)

    assert max(source.scanned_dates) == dt.date(2026, 8, 31)


def test_morning_batch_scans_even_when_cache_covers_utc_today(tmp_path: Path) -> None:
    repo = EdinetFilingCacheRepository(store_dir=tmp_path)
    seeded = _find(FakeDocumentSource(), repo, dt.datetime(2026, 8, 31, 1, 0, tzinfo=dt.UTC))
    assert seeded is not None
    assert seeded.newest_scanned_date == "2026-08-31"

    morning = FakeDocumentSource()
    cache = _find(morning, repo, dt.datetime(2026, 8, 31, 23, 0, tzinfo=dt.UTC))

    assert dt.date(2026, 9, 1) in morning.scanned_dates
    assert cache is not None
    assert cache.newest_scanned_date == "2026-09-01"


# --- 失敗時にcacheを前進させない -------------------------------------------


def test_failed_fetch_does_not_advance_newest_scanned(tmp_path: Path) -> None:
    repo = EdinetFilingCacheRepository(store_dir=tmp_path)
    seeded = _find(FakeDocumentSource(), repo, dt.datetime(2026, 8, 25, 1, 0, tzinfo=dt.UTC))
    assert seeded is not None

    failing = FakeDocumentSource(failed_dates={dt.date(2026, 8, d) for d in range(18, 32)})
    cache = _find(failing, repo, dt.datetime(2026, 8, 31, 1, 0, tzinfo=dt.UTC))

    assert cache is not None
    assert cache.newest_scanned_date == "2026-08-25"


def test_partial_failure_advances_only_up_to_last_consecutive_success(tmp_path: Path) -> None:
    repo = EdinetFilingCacheRepository(store_dir=tmp_path)
    seeded = _find(FakeDocumentSource(), repo, dt.datetime(2026, 8, 25, 1, 0, tzinfo=dt.UTC))
    assert seeded is not None

    failing = FakeDocumentSource(failed_dates={dt.date(2026, 8, 27)})
    cache = _find(failing, repo, dt.datetime(2026, 8, 28, 1, 0, tzinfo=dt.UTC))

    assert cache is not None
    assert cache.newest_scanned_date == "2026-08-26"


def test_no_cache_is_written_when_first_scan_completely_fails(tmp_path: Path) -> None:
    repo = EdinetFilingCacheRepository(store_dir=tmp_path)
    failing = FakeDocumentSource(failed_dates={dt.date(2026, 8, d) for d in range(20, 32)})

    cache = _find(failing, repo, dt.datetime(2026, 8, 31, 1, 0, tzinfo=dt.UTC))

    assert cache is None
    assert repo.get(_STOCK_CODE) is None


def test_documents_found_on_failed_day_do_not_block_later_success(tmp_path: Path) -> None:
    """失敗日より後の日で見つかった書類は取り込むが、走査済み範囲は前進させない。"""
    repo = EdinetFilingCacheRepository(store_dir=tmp_path)
    seeded = _find(FakeDocumentSource(), repo, dt.datetime(2026, 8, 25, 1, 0, tzinfo=dt.UTC))
    assert seeded is not None

    source = FakeDocumentSource(
        {dt.date(2026, 8, 28): [_annual_entry(doc_id="DOC9")]},
        failed_dates={dt.date(2026, 8, 26)},
    )
    cache = _find(source, repo, dt.datetime(2026, 8, 28, 1, 0, tzinfo=dt.UTC))

    assert cache is not None
    assert cache.latest_annual_doc_id == "DOC9"
    assert cache.newest_scanned_date == "2026-08-25"
