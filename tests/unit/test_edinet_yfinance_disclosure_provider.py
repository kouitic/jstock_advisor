import datetime as dt
from pathlib import Path

import pytest

import jstock_advisor.providers.disclosure.edinet_yfinance_impl as module
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
from jstock_advisor.interfaces.disclosure import (
    DisclosureAvailability,
    DisclosureUnavailableReason,
)
from jstock_advisor.interfaces.provider_errors import ProviderDataError
from jstock_advisor.providers.disclosure.edinet_yfinance_impl import (
    EdinetYfinanceDisclosureProvider,
)
from jstock_advisor.services import yfinance_rate_limit
from jstock_advisor.services.yfinance_rate_limit import call_with_rate_limit_retry

_STOCK_CODE = "2914"
_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)


class _NotConfiguredSource:
    is_configured = False
    refresh_window_days = 7

    def list_documents(self, scan_date: dt.date, now: dt.datetime) -> EdinetListResult:
        return EdinetListResult(
            EdinetFetchStatus.FETCH_FAILED, [], EdinetFailureReason.NOT_CONFIGURED
        )

    def download_document_zip(self, doc_id: str) -> EdinetDownloadResult:
        return EdinetDownloadResult(
            EdinetFetchStatus.FETCH_FAILED, None, EdinetFailureReason.NOT_CONFIGURED
        )


def test_get_disclosures_is_unavailable_when_edinet_not_configured(tmp_path: Path) -> None:
    """APIキー未設定は「開示0件」ではなく取得不能(Issue #53 Phase B2)。"""
    provider = EdinetYfinanceDisclosureProvider(
        document_source=_NotConfiguredSource(),  # type: ignore[arg-type]
        cache_repository=EdinetDisclosureCacheRepository(store_dir=tmp_path),
        now=_NOW,
    )

    result = provider.get_disclosures(_STOCK_CODE, dt.date(2026, 6, 1))

    assert result.availability is DisclosureAvailability.UNAVAILABLE
    assert result.unavailable_reason is DisclosureUnavailableReason.NOT_CONFIGURED
    assert result.disclosures == []
    assert result.is_available is False


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
        refresh_window_days = 7

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
    result = provider.get_disclosures(_STOCK_CODE, dt.date(2026, 6, 1))

    assert result.availability is DisclosureAvailability.AVAILABLE
    disclosures = result.disclosures
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


    monkeypatch.setattr(module.yf, "Ticker", _FakeTicker)
    provider = EdinetYfinanceDisclosureProvider(now=_NOW)
    assert provider.get_next_earnings_date("7203") is None


def test_get_next_earnings_date_raises_on_provider_access_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1: 外部アクセス失敗を None(=決算予定なし)へ読み替えない。

    旧契約は `except Exception -> None` で、provider障害が欠測と同義になり
    再試行も走らないまま EarningsDateStatus.UNAVAILABLE へロンダリングされていた。
    """

    class _RaisingTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        @property
        def calendar(self) -> dict[str, object]:
            raise RuntimeError("network error")

    monkeypatch.setattr(module.yf, "Ticker", _RaisingTicker)
    provider = EdinetYfinanceDisclosureProvider(now=_NOW)

    with pytest.raises(ProviderDataError) as excinfo:
        provider.get_next_earnings_date("7203")

    assert excinfo.value.provider_name == "yfinance"
    assert excinfo.value.operation == "get_next_earnings_date"
    assert excinfo.value.__cause__ is not None


def test_get_next_earnings_date_marks_rate_limit_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1補足: 429等はretryableとして分類される(既存分類器を再利用)。"""

    class _RateLimitedTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        @property
        def calendar(self) -> dict[str, object]:
            raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(module.yf, "Ticker", _RateLimitedTicker)
    provider = EdinetYfinanceDisclosureProvider(now=_NOW)

    with pytest.raises(ProviderDataError) as excinfo:
        provider.get_next_earnings_date("7203")

    assert excinfo.value.retryable is True


def test_get_next_earnings_date_retryable_failure_reaches_existing_retry_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T2: 既存のretry境界(call_with_rate_limit_retry)経由でretryが起動する。

    consumer側の配線を変えずに、B1/B2で確立したcontractへ接続できていることを固定する。
    """
    attempts: list[int] = []

    class _FlakyTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        @property
        def calendar(self) -> dict[str, object]:
            attempts.append(1)
            if len(attempts) < 2:
                raise RuntimeError("429 Too Many Requests")
            return {"Earnings Date": [dt.date(2026, 8, 4)]}

    monkeypatch.setattr(module.yf, "Ticker", _FlakyTicker)
    monkeypatch.setattr(yfinance_rate_limit.time, "sleep", lambda _seconds: None)
    provider = EdinetYfinanceDisclosureProvider(now=_NOW)

    result = call_with_rate_limit_retry(lambda: provider.get_next_earnings_date("7203"))

    assert len(attempts) >= 2, "retryが起動していない"
    assert result.error is None
    assert result.value == dt.date(2026, 8, 4)


@pytest.mark.parametrize(
    ("label", "calendar_value"),
    [
        ("calendar=None", None),
        ("calendar={}", {}),
        ("Earnings Date キー無し", {"Dividend Date": [dt.date(2026, 8, 4)]}),
        ("Earnings Date=None", {"Earnings Date": None}),
        ("Earnings Date=[]", {"Earnings Date": []}),
    ],
)
def test_get_next_earnings_date_returns_none_for_genuine_missing(
    monkeypatch: pytest.MonkeyPatch, label: str, calendar_value: object
) -> None:
    """T3/T4/T5: アクセス成功かつ決算予定日が未公表・欠測なら None(正常な欠測)。"""

    class _FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        @property
        def calendar(self) -> object:
            return calendar_value

    monkeypatch.setattr(module.yf, "Ticker", _FakeTicker)
    provider = EdinetYfinanceDisclosureProvider(now=_NOW)

    assert provider.get_next_earnings_date("7203") is None, label


@pytest.mark.parametrize(
    ("label", "calendar_value"),
    [
        ("calendar=list", [dt.date(2026, 8, 4)]),
        ("calendar=str", "unexpected"),
        ("Earnings Date=str", {"Earnings Date": "2026-08-04"}),
        ("Earnings Date要素がstr", {"Earnings Date": ["2026-08-04"]}),
        ("Earnings Date要素がint", {"Earnings Date": [20260804]}),
    ],
)
def test_get_next_earnings_date_raises_on_parse_failure(
    monkeypatch: pytest.MonkeyPatch, label: str, calendar_value: object
) -> None:
    """T6/T7: 応答構造が想定外の場合も **missing へ読み替えない**。

    parse failure を None + 警告に留めると「parse failure = missing」の混同が残るため、
    非再試行の failure として送出する(新しい例外階層は作らない)。
    """

    class _FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        @property
        def calendar(self) -> object:
            return calendar_value

    monkeypatch.setattr(module.yf, "Ticker", _FakeTicker)
    provider = EdinetYfinanceDisclosureProvider(now=_NOW)

    with pytest.raises(ProviderDataError) as excinfo:
        provider.get_next_earnings_date("7203")

    assert excinfo.value.retryable is False, label
    assert excinfo.value.operation == "get_next_earnings_date"


def test_get_next_earnings_date_normalizes_datetime_to_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """datetime契約: dt.datetime は dt.date のサブクラスのため素通しすると
    時刻付きの値が「次回決算日」として流れる。日付へ明示的に正規化する。

    既存consumerは日付比較と営業日数算出のみを行うため、挙動は変わらない
    (仕様変更ではなく既存の日付比較契約の明確化)。
    """

    class _FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        @property
        def calendar(self) -> dict[str, object]:
            return {"Earnings Date": [dt.datetime(2026, 8, 4, 15, 30, tzinfo=dt.UTC)]}

    monkeypatch.setattr(module.yf, "Ticker", _FakeTicker)
    provider = EdinetYfinanceDisclosureProvider(now=_NOW)

    result = provider.get_next_earnings_date("7203")

    assert result == dt.date(2026, 8, 4)
    assert not isinstance(result, dt.datetime)


