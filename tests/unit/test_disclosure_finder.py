"""臨時報告書のfinder(Issue #53 Phase B1で走査日・失敗扱いを修正)。

重点:
  - 走査対象日がJST暦日で決まること(JST 00:00〜08:59でも当日を走査する)
  - refresh window内(当日+直前7暦日)を毎回再走査すること
  - 取得に失敗した日を走査済みとしないこと(連続成功範囲までしか前進しない)
  - 書類ZIPの取得失敗もその日を未完了として扱うこと
"""

from __future__ import annotations

import datetime as dt
import inspect
import io
import zipfile
from pathlib import Path

import pytest

from jstock_advisor.infrastructure.edinet.disclosure_finder import (
    EdinetDisclosureCacheRepository,
    _extract_reason_summary,
    find_extraordinary_reports,
)
from jstock_advisor.infrastructure.edinet.document_finder import find_latest_filings
from jstock_advisor.infrastructure.edinet.document_list_cache import (
    EdinetDailyDocumentListCacheRepository,
    EdinetDocumentSource,
)
from jstock_advisor.infrastructure.edinet.types import (
    EdinetDocumentEntry,
    EdinetDownloadResult,
    EdinetFailureReason,
    EdinetFetchStatus,
    EdinetListResult,
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


class FakeDocumentSource:
    """EdinetDocumentSource相当のフェイク。

    finderの責務(どの日を走査し、どこまでを走査済みとするか)のみを検証するため、
    日付単位キャッシュ自体は持たない(そちらはtest_edinet_document_list_cache.py)。
    """

    def __init__(
        self,
        documents_by_date: dict[dt.date, list[EdinetDocumentEntry]] | None = None,
        zips_by_doc_id: dict[str, bytes | None] | None = None,
        failed_dates: set[dt.date] | None = None,
        configured: bool = True,
        refresh_window_days: int = 7,
    ) -> None:
        self._documents_by_date = documents_by_date or {}
        self._zips_by_doc_id = zips_by_doc_id or {}
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
                EdinetFetchStatus.FETCH_FAILED, [], EdinetFailureReason.HTTP_ERROR
            )
        entries = self._documents_by_date.get(scan_date, [])
        status = (
            EdinetFetchStatus.SUCCESS_WITH_DOCUMENTS
            if entries
            else EdinetFetchStatus.SUCCESS_EMPTY
        )
        return EdinetListResult(status, list(entries))

    def download_document_zip(self, doc_id: str) -> EdinetDownloadResult:
        payload = self._zips_by_doc_id.get(doc_id)
        if payload is None:
            return EdinetDownloadResult(
                EdinetFetchStatus.FETCH_FAILED, None, EdinetFailureReason.DOWNLOAD_ERROR
            )
        return EdinetDownloadResult(EdinetFetchStatus.SUCCESS_WITH_DOCUMENTS, payload)


def _entry(
    doc_id: str,
    sec_code: str = _SEC_CODE,
    doc_type: str = "180",
    submit_date_time: str = "2026-07-17 11:10",
) -> EdinetDocumentEntry:
    return EdinetDocumentEntry(
        sec_code=sec_code,
        doc_id=doc_id,
        doc_type_code=doc_type,
        submit_date_time=submit_date_time,
    )


def _find(
    source: FakeDocumentSource,
    repo: EdinetDisclosureCacheRepository,
    now: dt.datetime,
    initial_lookback_days: int = 10,
):
    return find_extraordinary_reports(
        source,  # type: ignore[arg-type]
        repo,
        _STOCK_CODE,
        now,
        initial_lookback_days=initial_lookback_days,
    )


# --- 提出理由の抽出(既存の回帰) ------------------------------------------


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


# --- 基本動作(既存の回帰) --------------------------------------------------


def test_not_configured_returns_none(tmp_path: Path) -> None:
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)
    source = FakeDocumentSource(configured=False)

    assert _find(source, repo, dt.datetime(2026, 7, 24, tzinfo=dt.UTC)) is None


def test_finds_matching_extraordinary_report(tmp_path: Path) -> None:
    now = dt.datetime(2026, 7, 20, tzinfo=dt.UTC)
    source = FakeDocumentSource(
        {dt.date(2026, 7, 17): [_entry("DOC1")]}, {"DOC1": _csv_zip(_REASON_ROWS)}
    )
    cache = _find(source, EdinetDisclosureCacheRepository(store_dir=tmp_path), now)

    assert cache is not None
    assert [r.doc_id for r in cache.records] == ["DOC1"]
    assert "代表取締役の異動について決議しました。" in cache.records[0].summary


def test_ignores_non_matching_sec_code(tmp_path: Path) -> None:
    now = dt.datetime(2026, 7, 20, tzinfo=dt.UTC)
    source = FakeDocumentSource(
        {dt.date(2026, 7, 17): [_entry("DOC1", sec_code="99990")]},
        {"DOC1": _csv_zip(_REASON_ROWS)},
    )
    cache = _find(source, EdinetDisclosureCacheRepository(store_dir=tmp_path), now)

    assert cache is not None
    assert cache.records == []


def test_ignores_non_extraordinary_doc_type(tmp_path: Path) -> None:
    now = dt.datetime(2026, 7, 20, tzinfo=dt.UTC)
    source = FakeDocumentSource(
        {dt.date(2026, 7, 17): [_entry("DOC1", doc_type="120")]},
        {"DOC1": _csv_zip(_REASON_ROWS)},
    )
    cache = _find(source, EdinetDisclosureCacheRepository(store_dir=tmp_path), now)

    assert cache is not None
    assert cache.records == []


def test_incremental_scan_appends_new_documents(tmp_path: Path) -> None:
    source = FakeDocumentSource(
        {dt.date(2026, 7, 17): [_entry("DOC1")], dt.date(2026, 7, 21): [_entry("DOC2")]},
        {"DOC1": _csv_zip(_REASON_ROWS), "DOC2": _csv_zip(_REASON_ROWS)},
    )
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)

    first = _find(source, repo, dt.datetime(2026, 7, 18, tzinfo=dt.UTC), initial_lookback_days=5)
    assert first is not None
    assert {r.doc_id for r in first.records} == {"DOC1"}

    second = _find(source, repo, dt.datetime(2026, 7, 22, tzinfo=dt.UTC), initial_lookback_days=5)
    assert second is not None
    assert {r.doc_id for r in second.records} == {"DOC1", "DOC2"}


# --- JST/UTC境界(#53の核心。現行テストの盲点だった) ------------------------


@pytest.mark.parametrize(
    ("now", "expected_today_jst"),
    [
        # JST 00:00 = 前日15:00Z
        (dt.datetime(2026, 8, 30, 15, 0, tzinfo=dt.UTC), dt.date(2026, 8, 31)),
        # JST 07:59 = 前日22:59Z
        (dt.datetime(2026, 8, 30, 22, 59, tzinfo=dt.UTC), dt.date(2026, 8, 31)),
        # JST 08:00(朝の判定バッチ)= 前日23:00Z
        (dt.datetime(2026, 8, 30, 23, 0, tzinfo=dt.UTC), dt.date(2026, 8, 31)),
        # JST 08:59 = 前日23:59Z
        (dt.datetime(2026, 8, 30, 23, 59, tzinfo=dt.UTC), dt.date(2026, 8, 31)),
        # JST 09:00 = 00:00Z(UTC暦日と一致する唯一の瞬間)
        (dt.datetime(2026, 8, 31, 0, 0, tzinfo=dt.UTC), dt.date(2026, 8, 31)),
        # JST 10:00 / 12:30 / 15:30
        (dt.datetime(2026, 8, 31, 1, 0, tzinfo=dt.UTC), dt.date(2026, 8, 31)),
        (dt.datetime(2026, 8, 31, 3, 30, tzinfo=dt.UTC), dt.date(2026, 8, 31)),
        (dt.datetime(2026, 8, 31, 6, 30, tzinfo=dt.UTC), dt.date(2026, 8, 31)),
    ],
)
def test_scan_range_ends_at_jst_today_for_all_batch_times(
    tmp_path: Path, now: dt.datetime, expected_today_jst: dt.date
) -> None:
    source = FakeDocumentSource()
    _find(source, EdinetDisclosureCacheRepository(store_dir=tmp_path), now)

    assert source.scanned_dates
    assert max(source.scanned_dates) == expected_today_jst


def test_morning_batch_scans_even_when_cache_covers_utc_today(tmp_path: Path) -> None:
    """Issue #53の最小再現。

    cache.newest_scanned_date="2026-08-31"、now=2026-08-31T23:00Z(= 09-01 08:00 JST)。
    修正前は today=now.date()=2026-08-31 となり `newest_scanned >= today` が成立して
    EDINETの呼び出し回数が0だった。
    """
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)
    seed_source = FakeDocumentSource()
    seeded = _find(seed_source, repo, dt.datetime(2026, 8, 31, 1, 0, tzinfo=dt.UTC))
    assert seeded is not None
    assert seeded.newest_scanned_date == "2026-08-31"

    morning_source = FakeDocumentSource()
    cache = _find(morning_source, repo, dt.datetime(2026, 8, 31, 23, 0, tzinfo=dt.UTC))

    assert morning_source.scanned_dates, "朝バッチが一度もEDINETを走査していない"
    assert dt.date(2026, 9, 1) in morning_source.scanned_dates
    assert cache is not None
    assert cache.newest_scanned_date == "2026-09-01"


def test_same_day_rerun_rescans_today_and_picks_up_late_filing(tmp_path: Path) -> None:
    """10:00の走査後に当日提出された書類を15:30の実行で検出できること。"""
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)
    today = dt.date(2026, 8, 31)

    morning = FakeDocumentSource()
    first = _find(morning, repo, dt.datetime(2026, 8, 31, 1, 0, tzinfo=dt.UTC))  # 10:00 JST
    assert first is not None
    assert first.records == []

    afternoon = FakeDocumentSource(
        {today: [_entry("LATE1", submit_date_time="2026-08-31 15:10")]},
        {"LATE1": _csv_zip(_REASON_ROWS)},
    )
    second = _find(afternoon, repo, dt.datetime(2026, 8, 31, 6, 30, tzinfo=dt.UTC))  # 15:30 JST

    assert today in afternoon.scanned_dates
    assert second is not None
    assert [r.doc_id for r in second.records] == ["LATE1"]


def test_refresh_window_rescans_recent_days_only(tmp_path: Path) -> None:
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)
    now = dt.datetime(2026, 8, 31, 1, 0, tzinfo=dt.UTC)  # 2026-08-31 10:00 JST

    _find(FakeDocumentSource(), repo, now, initial_lookback_days=30)

    rescan = FakeDocumentSource(refresh_window_days=7)
    _find(rescan, repo, now, initial_lookback_days=30)

    assert min(rescan.scanned_dates) == dt.date(2026, 8, 24)  # today - 7暦日
    assert max(rescan.scanned_dates) == dt.date(2026, 8, 31)
    # 窓より前の日付は再走査しない
    assert all(d >= dt.date(2026, 8, 24) for d in rescan.scanned_dates)


# --- 失敗時にcacheを前進させない(cache poisoning防止) ---------------------


def test_failed_fetch_does_not_advance_newest_scanned(tmp_path: Path) -> None:
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)
    seeded = _find(
        FakeDocumentSource(), repo, dt.datetime(2026, 8, 25, 1, 0, tzinfo=dt.UTC)
    )
    assert seeded is not None
    assert seeded.newest_scanned_date == "2026-08-25"

    failing = FakeDocumentSource(failed_dates={dt.date(2026, 8, d) for d in range(18, 32)})
    cache = _find(failing, repo, dt.datetime(2026, 8, 31, 1, 0, tzinfo=dt.UTC))

    assert cache is not None
    assert cache.newest_scanned_date == "2026-08-25", "取得失敗日を走査済みにしてはならない"


def test_partial_failure_advances_only_up_to_last_consecutive_success(tmp_path: Path) -> None:
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)
    seeded = _find(FakeDocumentSource(), repo, dt.datetime(2026, 8, 25, 1, 0, tzinfo=dt.UTC))
    assert seeded is not None

    # 08-26(水)は成功、08-27(木)で失敗、08-28(金)は成功
    failing = FakeDocumentSource(failed_dates={dt.date(2026, 8, 27)})
    cache = _find(failing, repo, dt.datetime(2026, 8, 28, 1, 0, tzinfo=dt.UTC))

    assert cache is not None
    assert cache.newest_scanned_date == "2026-08-26"


def test_zip_download_failure_marks_date_incomplete(tmp_path: Path) -> None:
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)
    seeded = _find(FakeDocumentSource(), repo, dt.datetime(2026, 8, 25, 1, 0, tzinfo=dt.UTC))
    assert seeded is not None

    source = FakeDocumentSource(
        {dt.date(2026, 8, 26): [_entry("DOC1", submit_date_time="2026-08-26 12:00")]},
        {"DOC1": None},  # ZIP取得失敗
    )
    cache = _find(source, repo, dt.datetime(2026, 8, 27, 1, 0, tzinfo=dt.UTC))

    assert cache is not None
    assert cache.records == []
    assert cache.newest_scanned_date == "2026-08-25", "ZIP取得失敗日を走査済みにしてはならない"


def test_no_cache_is_written_when_first_scan_completely_fails(tmp_path: Path) -> None:
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)
    now = dt.datetime(2026, 8, 31, 1, 0, tzinfo=dt.UTC)
    failing = FakeDocumentSource(
        failed_dates={dt.date(2026, 8, d) for d in range(20, 32)}
    )

    cache = _find(failing, repo, now, initial_lookback_days=10)

    assert cache is None
    assert repo.get(_STOCK_CODE) is None, "走査できていない範囲を走査済みとして保存しない"


def test_missing_summary_after_successful_download_does_not_block_advance(
    tmp_path: Path,
) -> None:
    """ダウンロードは成功したがテキストブロックが無い場合は未完了扱いにしない。"""
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)
    source = FakeDocumentSource(
        {dt.date(2026, 8, 28): [_entry("DOC1", submit_date_time="2026-08-28 12:00")]},
        {"DOC1": _csv_zip([("jpdei_cor:SecurityCodeDEI", "証券コード、DEI", "29140")])},
    )

    cache = _find(source, repo, dt.datetime(2026, 8, 31, 1, 0, tzinfo=dt.UTC))

    assert cache is not None
    assert cache.records == []
    assert cache.newest_scanned_date == "2026-08-31"


# --- refresh windowの正本がEdinetDocumentSourceであること -------------------
# finder(走査開始日)とdaily cache(freshness判定)が別々の窓を持つと、
# 「finderは再走査するのにcacheは窓外として古い成功結果を永久にfresh扱いする」
# という破綻が起きる。既定値7日では表面化しないため、非既定値で固定する。


class _CountingClient:
    """EdinetDocumentSourceへ渡す実クライアント相当のフェイク。"""

    is_configured = True

    def __init__(self) -> None:
        self.list_calls: list[dt.date] = []

    def list_documents(self, date: dt.date) -> EdinetListResult:
        self.list_calls.append(date)
        return EdinetListResult(EdinetFetchStatus.SUCCESS_EMPTY, [])

    def download_document_zip(self, doc_id: str) -> EdinetDownloadResult:
        return EdinetDownloadResult(
            EdinetFetchStatus.FETCH_FAILED, None, EdinetFailureReason.DOWNLOAD_ERROR
        )


def _real_source(
    tmp_path: Path, client: _CountingClient, refresh_window_days: int
) -> EdinetDocumentSource:
    return EdinetDocumentSource(
        client=client,  # type: ignore[arg-type]
        repository=EdinetDailyDocumentListCacheRepository(store_dir=tmp_path),
        refresh_window_days=refresh_window_days,
    )


def test_finder_uses_source_refresh_window_for_non_default_14_days(tmp_path: Path) -> None:
    """window=14では、14日前のdaily cacheがrefresh TTL超過なら再取得される。"""
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)
    cache_dir = tmp_path / "daily"
    cache_dir.mkdir()
    target = dt.date(2026, 8, 17)  # 2026-08-31 の14日前(月曜)
    now = dt.datetime(2026, 8, 31, 1, 0, tzinfo=dt.UTC)  # 2026-08-31 10:00 JST

    seed_client = _CountingClient()
    seed_source = EdinetDocumentSource(
        client=seed_client,  # type: ignore[arg-type]
        repository=EdinetDailyDocumentListCacheRepository(store_dir=cache_dir),
        refresh_window_days=14,
    )
    # refresh TTL(30分)を大きく超える過去に取得済みのdaily cacheを作る
    seed_source.list_documents(target, now - dt.timedelta(hours=6))
    assert seed_client.list_calls == [target]

    client = _CountingClient()
    source = EdinetDocumentSource(
        client=client,  # type: ignore[arg-type]
        repository=EdinetDailyDocumentListCacheRepository(store_dir=cache_dir),
        refresh_window_days=14,
    )
    find_extraordinary_reports(source, repo, _STOCK_CODE, now, initial_lookback_days=30)

    # finderが14日前を走査対象に含め、cache側もTTL超過として実際に再取得する
    assert target in client.list_calls


def test_source_window_7_treats_eight_days_ago_as_settled(tmp_path: Path) -> None:
    """window=7では8日前は窓外。finderは走査せず、cacheも確定扱いで再取得しない。"""
    repo = EdinetDisclosureCacheRepository(store_dir=tmp_path)
    cache_dir = tmp_path / "daily"
    cache_dir.mkdir()
    eight_days_ago = dt.date(2026, 8, 24) - dt.timedelta(days=1)  # 2026-08-23(日)
    settled_weekday = dt.date(2026, 8, 21)  # 8日以上前の平日
    now = dt.datetime(2026, 8, 31, 1, 0, tzinfo=dt.UTC)

    seed_client = _CountingClient()
    seed_source = EdinetDocumentSource(
        client=seed_client,  # type: ignore[arg-type]
        repository=EdinetDailyDocumentListCacheRepository(store_dir=cache_dir),
        refresh_window_days=7,
    )
    seed_source.list_documents(settled_weekday, now - dt.timedelta(days=3))

    client = _CountingClient()
    source = EdinetDocumentSource(
        client=client,  # type: ignore[arg-type]
        repository=EdinetDailyDocumentListCacheRepository(store_dir=cache_dir),
        refresh_window_days=7,
    )
    # 既存cacheを作り、以後は窓内のみ再走査される状態にする
    seeded = find_extraordinary_reports(source, repo, _STOCK_CODE, now, initial_lookback_days=30)
    assert seeded is not None

    rescan_client = _CountingClient()
    rescan_source = EdinetDocumentSource(
        client=rescan_client,  # type: ignore[arg-type]
        repository=EdinetDailyDocumentListCacheRepository(store_dir=cache_dir),
        refresh_window_days=7,
    )
    find_extraordinary_reports(rescan_source, repo, _STOCK_CODE, now, initial_lookback_days=30)

    assert settled_weekday not in rescan_client.list_calls
    assert eight_days_ago not in rescan_client.list_calls
    assert all(d >= dt.date(2026, 8, 24) for d in rescan_client.list_calls)


def test_finders_do_not_expose_independent_refresh_window_parameter() -> None:
    """finder側に独立したwindow設定を残さない(正本はEdinetDocumentSourceのみ)。"""
    assert "refresh_window_days" not in inspect.signature(find_extraordinary_reports).parameters
    assert "refresh_window_days" not in inspect.signature(find_latest_filings).parameters
