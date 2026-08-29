"""EDINET書類一覧の日付単位キャッシュ(Issue #53 Phase B1)。

検証対象:
  - refresh TTL(30分)/ negative TTL(5分)/ refresh window(7暦日)の区別
  - L1(プロセス内メモ)とL2(共有cache)による重複取得の抑止
  - API呼び出し回数が銘柄数Nに比例しないこと
  - itemサイズ上限超過時に「切り詰めない・L2へ保存しない・今回の結果は完全なまま使う」
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pytest

from jstock_advisor.infrastructure.edinet.document_list_cache import (
    MAX_SERIALIZED_PAYLOAD_BYTES,
    EdinetDailyDocumentListCacheRepository,
    EdinetDocumentSource,
)
from jstock_advisor.infrastructure.edinet.scan_window import MAX_REFRESH_WINDOW_DAYS
from jstock_advisor.infrastructure.edinet.types import (
    EdinetDocumentEntry,
    EdinetDownloadResult,
    EdinetFailureReason,
    EdinetFetchStatus,
    EdinetListResult,
)

# JST 09:00 = UTC 00:00。JST暦日とUTC暦日が一致する時刻。
_NOW = dt.datetime(2026, 8, 31, 0, 0, tzinfo=dt.UTC)
_TODAY_JST = dt.date(2026, 8, 31)


def _entry(doc_id: str, sec_code: str = "29140") -> EdinetDocumentEntry:
    return EdinetDocumentEntry(
        sec_code=sec_code,
        doc_id=doc_id,
        doc_type_code="180",
        submit_date_time="2026-08-31 10:00",
    )


class FakeClient:
    def __init__(
        self, result: EdinetListResult | None = None, configured: bool = True
    ) -> None:
        self._result = result or EdinetListResult(
            EdinetFetchStatus.SUCCESS_WITH_DOCUMENTS, [_entry("DOC1")]
        )
        self._configured = configured
        self.list_calls: list[dt.date] = []
        self.download_calls: list[str] = []

    @property
    def is_configured(self) -> bool:
        return self._configured

    def list_documents(self, date: dt.date) -> EdinetListResult:
        self.list_calls.append(date)
        return self._result

    def download_document_zip(self, doc_id: str) -> EdinetDownloadResult:
        self.download_calls.append(doc_id)
        return EdinetDownloadResult(EdinetFetchStatus.SUCCESS_WITH_DOCUMENTS, b"zip")


def _source(tmp_path: Path, client: FakeClient, **kwargs: object) -> EdinetDocumentSource:
    return EdinetDocumentSource(
        client=client,  # type: ignore[arg-type]
        repository=EdinetDailyDocumentListCacheRepository(store_dir=tmp_path),
        **kwargs,  # type: ignore[arg-type]
    )


# --- refresh window の検証 --------------------------------------------------


def test_refresh_window_days_upper_bound_is_enforced(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="refresh_window_days"):
        _source(tmp_path, FakeClient(), refresh_window_days=MAX_REFRESH_WINDOW_DAYS + 1)


def test_negative_refresh_window_days_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="refresh_window_days"):
        _source(tmp_path, FakeClient(), refresh_window_days=-1)


def test_date_inside_refresh_window_is_refetched_after_refresh_ttl(tmp_path: Path) -> None:
    client = FakeClient()
    source = _source(tmp_path, client)

    source.list_documents(_TODAY_JST, _NOW)
    # 30分以内は再取得しない
    source.list_documents(_TODAY_JST, _NOW + dt.timedelta(minutes=29))
    assert len(client.list_calls) == 1

    # 30分を超えたら再取得する(15:30以降に提出された当日分を取り込むため)
    fresh_source = _source(tmp_path, client)  # L1メモを持たない別プロセス相当
    fresh_source.list_documents(_TODAY_JST, _NOW + dt.timedelta(minutes=31))
    assert len(client.list_calls) == 2


def test_previous_day_inside_window_is_refetched_next_morning(tmp_path: Path) -> None:
    """15:30の最終走査後に前日付で提出された書類を翌朝取り込めること。"""
    yesterday = _TODAY_JST - dt.timedelta(days=1)
    client = FakeClient()
    _source(tmp_path, client).list_documents(yesterday, _NOW - dt.timedelta(hours=18))

    # 翌朝08:00 JST(= 前日23:00 UTC)相当の再走査
    _source(tmp_path, client).list_documents(yesterday, _NOW - dt.timedelta(hours=1))

    assert len(client.list_calls) == 2


def test_date_outside_refresh_window_is_never_refetched(tmp_path: Path) -> None:
    old_date = _TODAY_JST - dt.timedelta(days=30)
    client = FakeClient()
    _source(tmp_path, client).list_documents(old_date, _NOW)

    # 別プロセス・十分な時間経過後でも、窓外の成功結果は確定として再取得しない
    _source(tmp_path, client).list_documents(old_date, _NOW + dt.timedelta(days=3))

    assert len(client.list_calls) == 1


# --- negative cache(失敗時のstampede抑止) ---------------------------------


def test_failed_fetch_is_retried_only_after_negative_ttl(tmp_path: Path) -> None:
    failed = EdinetListResult(
        EdinetFetchStatus.FETCH_FAILED, [], EdinetFailureReason.HTTP_ERROR
    )
    client = FakeClient(result=failed)
    _source(tmp_path, client).list_documents(_TODAY_JST, _NOW)

    # 5分以内は再試行しない(障害時に全銘柄が同時リトライするのを防ぐ)
    _source(tmp_path, client).list_documents(_TODAY_JST, _NOW + dt.timedelta(minutes=4))
    assert len(client.list_calls) == 1

    _source(tmp_path, client).list_documents(_TODAY_JST, _NOW + dt.timedelta(minutes=6))
    assert len(client.list_calls) == 2


def test_failed_status_and_reason_are_preserved_through_cache(tmp_path: Path) -> None:
    failed = EdinetListResult(EdinetFetchStatus.FETCH_FAILED, [], EdinetFailureReason.TIMEOUT)
    client = FakeClient(result=failed)
    _source(tmp_path, client).list_documents(_TODAY_JST, _NOW)

    cached = _source(tmp_path, client).list_documents(_TODAY_JST, _NOW + dt.timedelta(minutes=1))

    assert cached.status is EdinetFetchStatus.FETCH_FAILED
    assert cached.failure_reason is EdinetFailureReason.TIMEOUT
    assert cached.succeeded is False


def test_success_empty_is_distinguished_from_failure(tmp_path: Path) -> None:
    client = FakeClient(result=EdinetListResult(EdinetFetchStatus.SUCCESS_EMPTY, []))
    result = _source(tmp_path, client).list_documents(_TODAY_JST, _NOW)

    assert result.status is EdinetFetchStatus.SUCCESS_EMPTY
    assert result.succeeded is True
    assert result.entries == []


def test_not_configured_is_failure_and_is_not_persisted(tmp_path: Path) -> None:
    client = FakeClient(configured=False)
    repo = EdinetDailyDocumentListCacheRepository(store_dir=tmp_path)
    source = EdinetDocumentSource(client=client, repository=repo)  # type: ignore[arg-type]

    result = source.list_documents(_TODAY_JST, _NOW)

    assert result.status is EdinetFetchStatus.FETCH_FAILED
    assert result.failure_reason is EdinetFailureReason.NOT_CONFIGURED
    assert client.list_calls == []
    # APIキー未設定は日付の性質ではなく実行環境の性質のため、L2へは残さない
    assert repo.get(_TODAY_JST) is None


# --- API呼び出し回数(A-2の核心) -------------------------------------------


def test_repeated_lookups_in_one_process_hit_memo(tmp_path: Path) -> None:
    client = FakeClient()
    source = _source(tmp_path, client)

    for _ in range(50):
        source.list_documents(_TODAY_JST, _NOW)

    assert len(client.list_calls) == 1


def test_api_calls_do_not_scale_with_stock_count(tmp_path: Path) -> None:
    """同一プロセスで300銘柄相当を処理しても、scan_dateごとの実API呼び出しは1回。

    cross-processで必ず1回になることは保証しない(分散leaseは導入していない)。
    """
    client = FakeClient()
    source = _source(tmp_path, client)
    scan_dates = [_TODAY_JST - dt.timedelta(days=offset) for offset in range(3)]

    for _ in range(300):  # 銘柄数相当
        for scan_date in scan_dates:
            source.list_documents(scan_date, _NOW)

    assert len(client.list_calls) == len(scan_dates)


def test_second_process_uses_shared_cache_without_api_call(tmp_path: Path) -> None:
    client = FakeClient()
    _source(tmp_path, client).list_documents(_TODAY_JST, _NOW)

    # 別Lambdaプロセス相当(L1メモ無し)。L2共有cacheがあるためAPIは呼ばない。
    result = _source(tmp_path, client).list_documents(_TODAY_JST, _NOW + dt.timedelta(minutes=1))

    assert len(client.list_calls) == 1
    assert [e.doc_id for e in result.entries] == ["DOC1"]


# --- itemサイズ上限 ---------------------------------------------------------


def _entries_exceeding_limit() -> list[EdinetDocumentEntry]:
    entries: list[EdinetDocumentEntry] = []
    approx_bytes = 0
    index = 0
    while approx_bytes <= MAX_SERIALIZED_PAYLOAD_BYTES:
        entry = EdinetDocumentEntry(
            sec_code="29140",
            doc_id=f"DOC{index:06d}",
            doc_type_code="180",
            submit_date_time="2026-08-31 10:00",
            period_end="2026-03-31",
            filer_name="サンプル株式会社" * 5,
        )
        entries.append(entry)
        approx_bytes += len(entry.model_dump_json().encode("utf-8"))
        index += 1
    return entries


def test_oversized_payload_is_not_persisted_and_is_not_truncated(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    entries = _entries_exceeding_limit()
    client = FakeClient(result=EdinetListResult(EdinetFetchStatus.SUCCESS_WITH_DOCUMENTS, entries))
    repo = EdinetDailyDocumentListCacheRepository(store_dir=tmp_path)
    source = EdinetDocumentSource(client=client, repository=repo)  # type: ignore[arg-type]

    with caplog.at_level(logging.ERROR):
        result = source.list_documents(_TODAY_JST, _NOW)

    # 1) 今回取得した結果は完全なまま(切り詰めない)
    assert len(result.entries) == len(entries)
    # 2) L2へは保存しない(部分データも保存しない)
    assert repo.get(_TODAY_JST) is None
    # 3) 黙って欠落させず、logger.errorで可視化する
    assert any("payload too large" in record.message for record in caplog.records)

    # 4) L1メモには保持されるため、同一プロセス内では再取得しない
    source.list_documents(_TODAY_JST, _NOW)
    assert len(client.list_calls) == 1


def test_within_limit_payload_is_persisted(tmp_path: Path) -> None:
    client = FakeClient()
    repo = EdinetDailyDocumentListCacheRepository(store_dir=tmp_path)
    source = EdinetDocumentSource(client=client, repository=repo)  # type: ignore[arg-type]

    source.list_documents(_TODAY_JST, _NOW)

    cached = repo.get(_TODAY_JST)
    assert cached is not None
    assert cached.scan_date == _TODAY_JST.isoformat()
    assert cached.entry_count == 1
    assert cached.fetch_status is EdinetFetchStatus.SUCCESS_WITH_DOCUMENTS


def test_download_is_passed_through_without_caching(tmp_path: Path) -> None:
    client = FakeClient()
    source = _source(tmp_path, client)

    first = source.download_document_zip("DOC1")
    second = source.download_document_zip("DOC1")

    assert first.payload == b"zip"
    assert second.succeeded is True
    # 書類ZIPは日付単位ではないためキャッシュせず、毎回clientへ委譲する
    assert client.download_calls == ["DOC1", "DOC1"]
