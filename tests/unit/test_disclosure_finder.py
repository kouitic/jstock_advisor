import datetime as dt
import io
import zipfile
from pathlib import Path

from jstock_advisor.infrastructure.edinet.disclosure_finder import (
    EdinetDisclosureCacheRepository,
    _extract_reason_summary,
    find_extraordinary_reports,
)

_STOCK_CODE = "2914"
_SEC_CODE = "29140"


def _csv_zip(rows: list[tuple[str, str, str]]) -> bytes:
    """(element_id, item_name, value)のリストからEDINET形式のZIP+CSVを組み立てる。"""
    header = (
        "要素ID\t項目名\tコンテキストID\t相対年度\t連結・個別\t期間・時点\tユニットID\t単位\t値\n"
    )
    lines = [header]
    for element_id, item_name, value in rows:
        lines.append(
            f'"{element_id}"\t"{item_name}"\t"c"\t"当期"\t"連結"\t"時点"\t"-"\t"-"\t"{value}"\n'
        )
    csv_text = "".join(lines)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("XBRL_TO_CSV/jpcrp050300-esr-001_E00000-000.csv", csv_text.encode("utf-16"))
    return buf.getvalue()


_REASON_ROWS = [
    (
        "jpcrp-esr_cor:PlaceForPublicInspectionCoverPageTextBlock",
        "縦覧に供する場所、表紙 [テキストブロック]",
        "東京証券取引所",
    ),
    (
        "jpcrp-esr_cor:ReasonForFilingTextBlock",
        "提出理由 [テキストブロック]",
        "代表取締役の異動について決議しました。",
    ),
    (
        "jpcrp-esr_cor:ChangesInRepresentativeDirectorsTextBlock",
        "代表取締役の異動 [テキストブロック]",
        "詳細は以下の通りです。",
    ),
]


class FakeEdinetClient:
    def __init__(
        self,
        documents_by_date: dict[dt.date, list[dict[str, object]]],
        zips_by_doc_id: dict[str, bytes | None],
        configured: bool = True,
    ) -> None:
        self._documents_by_date = documents_by_date
        self._zips_by_doc_id = zips_by_doc_id
        self._configured = configured

    @property
    def is_configured(self) -> bool:
        return self._configured

    def list_documents(self, date: dt.date) -> list[dict[str, object]]:
        return self._documents_by_date.get(date, [])

    def download_document_zip(self, doc_id: str) -> bytes | None:
        return self._zips_by_doc_id.get(doc_id)


def _doc_entry(doc_id: str, sec_code: str = _SEC_CODE, doc_type: str = "180") -> dict[str, object]:
    return {
        "docID": doc_id,
        "secCode": sec_code,
        "docTypeCode": doc_type,
        "submitDateTime": "2026-07-17 11:10",
    }


def test_extract_reason_summary_excludes_cover_page_and_joins_blocks() -> None:
    summary = _extract_reason_summary(_csv_zip(_REASON_ROWS))
    assert summary is not None
    assert "代表取締役の異動について決議しました。" in summary
    assert "詳細は以下の通りです。" in summary
    assert "東京証券取引所" not in summary


def test_extract_reason_summary_returns_none_for_invalid_zip() -> None:
    assert _extract_reason_summary(b"not a zip") is None


def test_extract_reason_summary_returns_none_when_no_text_blocks() -> None:
    rows = [("jpdei_cor:SecurityCodeDEI", "証券コード、DEI", "29140")]
    assert _extract_reason_summary(_csv_zip(rows)) is None


def test_not_configured_returns_none(tmp_path: Path) -> None:
    client = FakeEdinetClient({}, {}, configured=False)
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)
    result = find_extraordinary_reports(
        client, repo, _STOCK_CODE, dt.datetime(2026, 7, 24, tzinfo=dt.UTC)
    )
    assert result is None


def test_finds_matching_extraordinary_report(tmp_path: Path) -> None:
    now = dt.datetime(2026, 7, 20, tzinfo=dt.UTC)
    doc_date = dt.date(2026, 7, 17)
    client = FakeEdinetClient(
        {doc_date: [_doc_entry("DOC1")]},
        {"DOC1": _csv_zip(_REASON_ROWS)},
    )
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)

    cache = find_extraordinary_reports(client, repo, _STOCK_CODE, now, initial_lookback_days=10)
    assert cache is not None
    assert len(cache.records) == 1
    assert cache.records[0].doc_id == "DOC1"
    assert "代表取締役の異動について決議しました。" in cache.records[0].summary


def test_ignores_non_matching_sec_code(tmp_path: Path) -> None:
    now = dt.datetime(2026, 7, 20, tzinfo=dt.UTC)
    doc_date = dt.date(2026, 7, 17)
    client = FakeEdinetClient(
        {doc_date: [_doc_entry("DOC1", sec_code="99990")]},
        {"DOC1": _csv_zip(_REASON_ROWS)},
    )
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)

    cache = find_extraordinary_reports(client, repo, _STOCK_CODE, now, initial_lookback_days=10)
    assert cache is not None
    assert cache.records == []


def test_ignores_non_extraordinary_doc_type(tmp_path: Path) -> None:
    now = dt.datetime(2026, 7, 20, tzinfo=dt.UTC)
    doc_date = dt.date(2026, 7, 17)
    client = FakeEdinetClient(
        {doc_date: [_doc_entry("DOC1", doc_type="120")]},
        {"DOC1": _csv_zip(_REASON_ROWS)},
    )
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)

    cache = find_extraordinary_reports(client, repo, _STOCK_CODE, now, initial_lookback_days=10)
    assert cache is not None
    assert cache.records == []


def test_skips_document_when_download_fails(tmp_path: Path) -> None:
    now = dt.datetime(2026, 7, 20, tzinfo=dt.UTC)
    doc_date = dt.date(2026, 7, 17)
    client = FakeEdinetClient({doc_date: [_doc_entry("DOC1")]}, {"DOC1": None})
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)

    cache = find_extraordinary_reports(client, repo, _STOCK_CODE, now, initial_lookback_days=10)
    assert cache is not None
    assert cache.records == []


def test_incremental_scan_only_covers_new_dates_and_appends(tmp_path: Path) -> None:
    day1 = dt.date(2026, 7, 17)
    day2 = dt.date(2026, 7, 21)
    client = FakeEdinetClient(
        {day1: [_doc_entry("DOC1")], day2: [_doc_entry("DOC2")]},
        {"DOC1": _csv_zip(_REASON_ROWS), "DOC2": _csv_zip(_REASON_ROWS)},
    )
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)

    first_now = dt.datetime(2026, 7, 18, tzinfo=dt.UTC)
    first_cache = find_extraordinary_reports(
        client, repo, _STOCK_CODE, first_now, initial_lookback_days=5
    )
    assert first_cache is not None
    assert {r.doc_id for r in first_cache.records} == {"DOC1"}

    second_now = dt.datetime(2026, 7, 22, tzinfo=dt.UTC)
    second_cache = find_extraordinary_reports(
        client, repo, _STOCK_CODE, second_now, initial_lookback_days=5
    )
    assert second_cache is not None
    assert {r.doc_id for r in second_cache.records} == {"DOC1", "DOC2"}


def test_second_call_same_day_returns_cached_without_rescanning(tmp_path: Path) -> None:
    now = dt.datetime(2026, 7, 20, tzinfo=dt.UTC)
    doc_date = dt.date(2026, 7, 17)
    call_count = {"n": 0}

    class CountingClient(FakeEdinetClient):
        def list_documents(self, date: dt.date) -> list[dict[str, object]]:
            call_count["n"] += 1
            return super().list_documents(date)

    client = CountingClient({doc_date: [_doc_entry("DOC1")]}, {"DOC1": _csv_zip(_REASON_ROWS)})
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)

    find_extraordinary_reports(client, repo, _STOCK_CODE, now, initial_lookback_days=10)
    calls_after_first = call_count["n"]
    find_extraordinary_reports(client, repo, _STOCK_CODE, now, initial_lookback_days=10)
    assert call_count["n"] == calls_after_first
