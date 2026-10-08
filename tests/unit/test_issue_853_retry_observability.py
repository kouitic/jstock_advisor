"""Issue #853(#718 I-2): retry の回数・待機秒の観測値と、1 銘柄 1 行の構造化ログ。

悪循環仮説(SQS 滞留 → provider への高負荷 → 速度低下 → 滞留の悪化)の検証のため、
`RateLimitRetryResult` に `attempts` と `slept_seconds` を足し、ウォッチリストの
スクリーニングデータ取得の 1 銘柄ごとに 1 行のログを残す。**観測の追加のみ**で、
retry の挙動・判定・保存・通知は変えない。このファイルは次を固定する。

(a) 互換: 既存のフィールドの位置・意味、新フィールドの既定値(従来の構築がそのまま通る)
(b) retry の挙動が不変で、attempts / slept_seconds が実際の呼び出し回数・待機の合計と一致する
(c) 3 つの呼び出し経路(ウォッチリスト・買い候補・保有監視)が、戻り値を従来どおり読む
(d) ログが 1 銘柄 1 行で、stock_code を含まない。DATA_ERROR・NOT_FOUND・OK の outcome を区別する
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import inspect
import logging
from types import SimpleNamespace

import pytest

from jstock_advisor.lambda_handlers import buy_candidates_handler, holdings_watchlist_handler
from jstock_advisor.services import screening_data_provider as sdp_module
from jstock_advisor.services import yfinance_rate_limit
from jstock_advisor.services.screening_data_provider import (
    LightweightScreeningDataProvider,
    ScreeningDataStatus,
    StockSnapshotScreeningDataProvider,
)
from jstock_advisor.services.yfinance_rate_limit import (
    RateLimitRetryResult,
    call_with_rate_limit_retry,
)
from tests.unit.test_screening_data_provider import _fake_snapshot

_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)
_LOGGER_NAME = "jstock_advisor.services.screening_data_provider"
_STOCK_CODE = "7777"


class _ResponseWithStatus:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}


class _RetryableError(Exception):
    """障害疑い(429)と分類される例外。"""

    def __init__(self) -> None:
        super().__init__("rate limited")
        self.response = _ResponseWithStatus(429)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """time.sleep を記録だけにして(待たない)、jitter を 0 に固定する。"""
    recorded: list[float] = []
    monkeypatch.setattr("jstock_advisor.services.yfinance_rate_limit.time.sleep", recorded.append)
    monkeypatch.setattr(
        "jstock_advisor.services.yfinance_rate_limit.random.uniform", lambda _a, _b: 0.0
    )
    return recorded


def _fetch_stats_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _LOGGER_NAME and "fetch stats" in r.getMessage()]


# --- (a) 互換 ----------------------------------------------------------------------


def test_existing_fields_keep_position_and_new_fields_have_defaults() -> None:
    names = [f.name for f in dataclasses.fields(RateLimitRetryResult)]
    assert names[:3] == ["value", "is_provider_failure_suspected", "error"]
    assert names[3:] == ["attempts", "slept_seconds"]

    legacy = RateLimitRetryResult(value="x", is_provider_failure_suspected=False, error=None)
    assert legacy.attempts == 1
    assert legacy.slept_seconds == 0.0
    assert legacy.value == "x"
    assert legacy.error is None


def test_result_is_still_frozen() -> None:
    result = RateLimitRetryResult(value=1, is_provider_failure_suspected=False, error=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.attempts = 5  # type: ignore[misc]


# --- (b) retry の挙動が不変で、観測値が実際と一致する ---------------------------------------


def test_first_try_success_has_one_attempt_and_no_sleep(sleeps: list[float]) -> None:
    result = call_with_rate_limit_retry(lambda: "ok")
    assert result.value == "ok"
    assert result.error is None
    assert result.is_provider_failure_suspected is False
    assert result.attempts == 1
    assert result.slept_seconds == 0.0
    assert sleeps == []


def test_recovery_after_two_failures_counts_attempts_and_sleep(sleeps: list[float]) -> None:
    calls = {"n": 0}

    def _flaky() -> str:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _RetryableError
        return "recovered"

    result = call_with_rate_limit_retry(_flaky)

    assert result.value == "recovered"
    assert result.error is None
    assert result.is_provider_failure_suspected is False
    assert calls["n"] == 3
    assert result.attempts == 3  # func を呼んだ回数(初回を含む)
    assert len(sleeps) == 2  # retry の待機は失敗の回数分
    assert result.slept_seconds == pytest.approx(sum(sleeps))
    assert result.slept_seconds > 0.0


def test_exhausted_retries_report_all_attempts_and_total_sleep(sleeps: list[float]) -> None:
    calls = {"n": 0}

    def _always_fail() -> None:
        calls["n"] += 1
        raise _RetryableError

    result = call_with_rate_limit_retry(_always_fail)

    assert result.value is None
    assert isinstance(result.error, _RetryableError)
    assert result.is_provider_failure_suspected is True
    max_calls = yfinance_rate_limit._MAX_RETRIES + 1
    assert calls["n"] == max_calls  # retry の回数の上限は従来のまま
    assert result.attempts == max_calls
    assert len(sleeps) == max_calls - 1  # 最後の失敗のあとは待たない
    assert result.slept_seconds == pytest.approx(sum(sleeps))


def test_non_provider_failure_is_still_reraised_without_retry(sleeps: list[float]) -> None:
    calls = {"n": 0}

    def _boom() -> None:
        calls["n"] += 1
        raise KeyError("not a provider failure")

    with pytest.raises(KeyError):
        call_with_rate_limit_retry(_boom)
    assert calls["n"] == 1
    assert sleeps == []


# --- (c) 3 つの呼び出し経路が戻り値を従来どおり読む -----------------------------------------


def test_consumers_read_only_the_legacy_fields() -> None:
    """読み手(ウォッチリスト・買い候補・保有監視)は value / error / is_provider_failure_suspected
    だけを読む。新フィールド(attempts / slept_seconds)に依存する読み方が増えていないこと。
    """
    new_attrs = ("attempts", "slept_seconds")
    for module in (sdp_module, buy_candidates_handler, holdings_watchlist_handler):
        assert "call_with_rate_limit_retry" in inspect.getsource(module)
    # 新フィールドを読むのは、観測用のログの helper(sdp_module)だけ
    for module in (buy_candidates_handler, holdings_watchlist_handler):
        source = inspect.getsource(module)
        for attr in new_attrs:
            assert f"retry_result.{attr}" not in source


def test_provider_results_are_unchanged_for_each_outcome(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """ログを足しても、ScreeningDataResult の status・error_message・障害疑いは従来と同じ。"""
    provider = StockSnapshotScreeningDataProvider(object(), object())  # type: ignore[arg-type]

    monkeypatch.setattr(
        sdp_module, "build_stock_snapshot", lambda *a, **kw: (_fake_snapshot(), None)
    )
    ok = provider.get_screening_input(_STOCK_CODE, _NOW)
    assert ok.status == ScreeningDataStatus.OK
    assert ok.error_message is None

    monkeypatch.setattr(sdp_module, "build_stock_snapshot", lambda *a, **kw: (None, "no price"))
    not_found = provider.get_screening_input(_STOCK_CODE, _NOW)
    assert not_found.status == ScreeningDataStatus.NOT_FOUND
    assert not_found.error_message == "no price"

    def _retryable(*_a: object, **_kw: object) -> None:
        raise _RetryableError

    monkeypatch.setattr(sdp_module, "build_stock_snapshot", _retryable)
    suspected = provider.get_screening_input(_STOCK_CODE, _NOW)
    assert suspected.status == ScreeningDataStatus.DATA_ERROR
    assert suspected.is_provider_failure_suspected is True
    assert "rate limited" in (suspected.error_message or "")


# --- (d) 1 銘柄 1 行のログ --------------------------------------------------------------


def test_logger_level_is_declared_for_lambda() -> None:
    """Lambda の root logger は WARNING。INFO を出すには module が setLevel を宣言する(#413)。"""
    assert logging.getLogger(_LOGGER_NAME).level == logging.INFO


def test_full_provider_logs_one_line_per_stock_without_stock_code(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    calls = {"n": 0}

    def _flaky(*_a: object, **_kw: object) -> tuple[SimpleNamespace, None]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _RetryableError
        return _fake_snapshot(), None

    monkeypatch.setattr(sdp_module, "build_stock_snapshot", _flaky)
    provider = StockSnapshotScreeningDataProvider(object(), object())  # type: ignore[arg-type]

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        result = provider.get_screening_input(_STOCK_CODE, _NOW)

    assert result.status == ScreeningDataStatus.OK
    records = _fetch_stats_records(caplog)
    assert len(records) == 1  # 1 銘柄につき 1 行
    message = records[0].getMessage()
    assert "outcome=OK" in message
    assert "attempts=2" in message
    assert f"retry_slept_seconds={sum(sleeps):.1f}" in message
    assert "duration_ms=" in message
    assert "provider_failure_suspected=False" in message
    assert _STOCK_CODE not in message  # 銘柄コードは出さない
    assert _STOCK_CODE not in records[0].name


@pytest.mark.parametrize(
    ("outcome", "builder"),
    [
        ("NOT_FOUND", lambda *a, **kw: (None, "no price")),
        ("DATA_ERROR", None),  # 障害疑いが尽きる
    ],
)
def test_full_provider_logs_outcome_for_not_found_and_exhausted_retries(
    monkeypatch: pytest.MonkeyPatch,
    sleeps: list[float],
    caplog: pytest.LogCaptureFixture,
    outcome: str,
    builder: object,
) -> None:
    if builder is None:

        def _always_retryable(*_a: object, **_kw: object) -> None:
            raise _RetryableError

        monkeypatch.setattr(sdp_module, "build_stock_snapshot", _always_retryable)
    else:
        monkeypatch.setattr(sdp_module, "build_stock_snapshot", builder)
    provider = StockSnapshotScreeningDataProvider(object(), object())  # type: ignore[arg-type]

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        provider.get_screening_input(_STOCK_CODE, _NOW)

    records = _fetch_stats_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert f"outcome={outcome}" in message
    if outcome == "DATA_ERROR":
        assert f"attempts={yfinance_rate_limit._MAX_RETRIES + 1}" in message
        assert "provider_failure_suspected=True" in message
    else:
        assert "attempts=1" in message
        assert "retry_slept_seconds=0.0" in message
    assert _STOCK_CODE not in message


def test_non_provider_exception_logs_data_error_without_retry_counts(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    def _raise(*_a: object, **_kw: object) -> None:
        raise RuntimeError("network error")

    monkeypatch.setattr(sdp_module, "build_stock_snapshot", _raise)
    provider = StockSnapshotScreeningDataProvider(object(), object())  # type: ignore[arg-type]

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        result = provider.get_screening_input(_STOCK_CODE, _NOW)

    assert result.status == ScreeningDataStatus.DATA_ERROR
    records = _fetch_stats_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "outcome=DATA_ERROR" in message
    assert "attempts=na" in message  # retry の回数は分からない(再送出された)
    assert _STOCK_CODE not in message


def test_lightweight_provider_logs_one_line_per_outcome(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    provider = LightweightScreeningDataProvider(object(), object())  # type: ignore[arg-type]
    ok_input = SimpleNamespace(missing_required_fields=[], missing_scoring_fields=[])

    cases: list[tuple[str, object]] = [
        ("OK", lambda *a, **kw: (ok_input, None)),
        ("NOT_FOUND", lambda *a, **kw: (None, "no data")),
    ]
    for outcome, fetch in cases:
        monkeypatch.setattr(provider, "_fetch_and_build", fetch)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            provider.get_screening_input(_STOCK_CODE, _NOW)
        records = _fetch_stats_records(caplog)
        assert len(records) == 1
        message = records[0].getMessage()
        assert f"outcome={outcome}" in message
        assert "attempts=1" in message
        assert _STOCK_CODE not in message

    def _always_retryable(*_a: object, **_kw: object) -> None:
        raise _RetryableError

    monkeypatch.setattr(provider, "_fetch_and_build", _always_retryable)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        result = provider.get_screening_input(_STOCK_CODE, _NOW)
    assert result.status == ScreeningDataStatus.DATA_ERROR
    assert result.is_provider_failure_suspected is True
    records = _fetch_stats_records(caplog)
    assert len(records) == 1
    assert "outcome=DATA_ERROR" in records[0].getMessage()
    assert f"attempts={yfinance_rate_limit._MAX_RETRIES + 1}" in records[0].getMessage()
