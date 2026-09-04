"""Issue #125: yfinanceの「期待される恒久missing」ログ降格フィルタの契約。

契約の骨子:
  降格するのは``ERROR``ちょうどのみ。``CRITICAL``は決して降格しない。
  Yahoo側が404 Not Foundを返した事実は単独で降格根拠になる。
  `possibly delisted` は**単独では降格しない**。恒久missingでもtimeout等の
  一時障害でも同じ文言になるため、同一取得コンテキスト内の confirmed 404 と
  相関が取れた場合に限り二次ノイズとして降格する。
  レコードは削除しない(消すのではなくERRORではないと分かる形で残す)。
  Issue #59のprovider failure semantics(分類・retryability・送出条件)は不変。
"""

from __future__ import annotations

import logging
import threading

import pytest

from jstock_advisor.providers.market_data._yfinance_log_filter import (
    SIGNATURE_QUOTE_NOT_FOUND,
    SIGNATURE_SECONDARY_MISSING,
    YFINANCE_LOGGER_NAME,
    YfinanceExpectedMissingLogFilter,
    install_yfinance_expected_missing_log_filter,
    yfinance_fetch_context,
)

# Yahoo側が「銘柄が見つからない」と明示した確定的な文言。
_CONFIRMED_404 = (
    "Failed to get ticker '9999.T' reason: "
    "404 Client Error: Not Found for url: https://example.invalid/v8/finance/chart/9999.T"
)
_CONFIRMED_404_OTHER_TICKER = (
    "Failed to get ticker '8888.T' reason: "
    "404 Client Error: Not Found for url: https://example.invalid/v8/finance/chart/8888.T"
)

# 曖昧な文言。恒久missingでも一時障害でも同じ形になる。
_DELISTED_NO_TZ = "$9999.T: possibly delisted; no timezone found"
_DELISTED_NO_PRICE = "$9999.T: possibly delisted; no price data found (1d 2026-08-01 -> 2026-09-04)"

# 真の障害。いずれも降格してはならない。
_TIMEOUT = (
    "Failed to get ticker '1234.T' reason: "
    "HTTPSConnectionPool(host='example.invalid', port=443): Read timed out. (read timeout=30)"
)
_CONNECTION_ERROR = (
    "Failed to get ticker '1234.T' reason: "
    "HTTPSConnectionPool(host='example.invalid', port=443): Max retries exceeded "
    "(Caused by NewConnectionError('Failed to establish a new connection'))"
)
_SERVER_ERROR = (
    "Failed to get ticker '1234.T' reason: "
    "500 Server Error: Internal Server Error for url: https://example.invalid/v8/finance/chart"
)
_RATE_LIMITED = (
    "Failed to get ticker '1234.T' reason: 429 Client Error: Too Many Requests for url: x"
)
_UNEXPECTED = "Failed to get ticker '1234.T' reason: KeyError('Close')"
# 404を含むが「見つからない」旨が無い。最小安全条件を満たさない。
_404_WITHOUT_NOT_FOUND = (
    "Failed to get ticker '1234.T' reason: 404 Client Error: Gone for url: https://example.invalid"
)


def _record(message: str, *, level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord(
        name=YFINANCE_LOGGER_NAME,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def _apply(
    message: str,
    *,
    level: int = logging.ERROR,
    log_filter: YfinanceExpectedMissingLogFilter | None = None,
) -> logging.LogRecord:
    record = _record(message, level=level)
    active = log_filter or YfinanceExpectedMissingLogFilter()
    assert active.filter(record) is True
    return record


def _assert_untouched(record: logging.LogRecord, message: str) -> None:
    assert record.levelno == logging.ERROR
    assert record.levelname == "ERROR"
    assert record.getMessage() == message
    assert not hasattr(record, "jstock_expected_permanent_missing")


# --- 1/2: confirmed 404 は単独で降格。最小安全条件を満たさなければ ERROR -------


def test_confirmed_404_not_found_is_downgraded() -> None:
    record = _apply(_CONFIRMED_404)

    assert record.levelno == logging.WARNING
    assert record.levelname == "WARNING"
    assert record.jstock_signature == SIGNATURE_QUOTE_NOT_FOUND
    assert record.jstock_ticker == "9999.T"
    assert record.jstock_stock_code == "9999"
    assert record.jstock_original_level == "ERROR"

    rendered = record.getMessage()
    assert "stock_code=9999" in rendered
    assert f"signature={SIGNATURE_QUOTE_NOT_FOUND}" in rendered
    assert "expected=true" in rendered
    assert "original_level=ERROR" in rendered


def test_404_without_not_found_marker_stays_error() -> None:
    _assert_untouched(_apply(_404_WITHOUT_NOT_FOUND), _404_WITHOUT_NOT_FOUND)


# --- 3/4: possibly delisted は単独では絶対に降格しない ------------------------


@pytest.mark.parametrize("message", [_DELISTED_NO_TZ, _DELISTED_NO_PRICE])
def test_possibly_delisted_standalone_stays_error(message: str) -> None:
    """恒久missingでもtimeout等でも同じ文言になるため、単独では降格根拠にならない。"""
    _assert_untouched(_apply(message), message)


@pytest.mark.parametrize("message", [_DELISTED_NO_TZ, _DELISTED_NO_PRICE])
def test_possibly_delisted_stays_error_inside_context_without_confirmation(
    message: str,
) -> None:
    """コンテキストがあっても、confirmed 404 が無ければ降格しない。"""
    log_filter = YfinanceExpectedMissingLogFilter()
    with yfinance_fetch_context():
        _assert_untouched(_apply(message, log_filter=log_filter), message)


# --- 5/6/7: 真の障害は ERROR のまま一切改変しない ----------------------------


@pytest.mark.parametrize(
    "message",
    [_TIMEOUT, _CONNECTION_ERROR, _SERVER_ERROR, _RATE_LIMITED, _UNEXPECTED],
)
def test_real_provider_failure_stays_error_untouched(message: str) -> None:
    _assert_untouched(_apply(message), message)


def test_timeout_derived_possibly_delisted_stays_error() -> None:
    """★ timeoutは同一コンテキスト内で `possibly delisted` を誘発するが降格しない。

    timeoutの行は404 markerを満たさないため confirmed として記録されず、
    後続の曖昧な行も相関が取れないため ERROR のまま残る。
    """
    log_filter = YfinanceExpectedMissingLogFilter()
    with yfinance_fetch_context():
        _assert_untouched(_apply(_TIMEOUT, log_filter=log_filter), _TIMEOUT)
        derived = "$1234.T: possibly delisted; no timezone found"
        _assert_untouched(_apply(derived, log_filter=log_filter), derived)


# --- 8: CRITICAL は決して降格しない ------------------------------------------


@pytest.mark.parametrize("message", [_CONFIRMED_404, _DELISTED_NO_TZ])
def test_critical_is_never_downgraded(message: str) -> None:
    log_filter = YfinanceExpectedMissingLogFilter()
    with yfinance_fetch_context():
        _apply(_CONFIRMED_404, log_filter=log_filter)  # 相関条件を満たす状態にする
        record = _apply(message, level=logging.CRITICAL, log_filter=log_filter)

    assert record.levelno == logging.CRITICAL
    assert record.levelname == "CRITICAL"
    assert record.getMessage() == message
    assert not hasattr(record, "jstock_expected_permanent_missing")


# --- 9: 一致しないレコード・非ERRORは改変しない ------------------------------


def test_unrelated_error_record_is_untouched() -> None:
    message = "Yahoo API request failed for an unrelated reason"
    _assert_untouched(_apply(message), message)


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO, logging.WARNING])
def test_non_error_records_are_untouched(level: int) -> None:
    record = _apply(_CONFIRMED_404, level=level)

    assert record.levelno == level
    assert record.getMessage() == _CONFIRMED_404
    assert not hasattr(record, "jstock_expected_permanent_missing")


def test_lazy_formatted_record_is_matched_after_rendering() -> None:
    record = logging.LogRecord(
        name=YFINANCE_LOGGER_NAME,
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Failed to get ticker '%s' reason: %s",
        args=("9999.T", "404 Client Error: Not Found for url: https://example.invalid"),
        exc_info=None,
    )
    assert YfinanceExpectedMissingLogFilter().filter(record) is True

    assert record.levelno == logging.WARNING
    assert record.jstock_stock_code == "9999"


# --- 10: 相関の安全性 --------------------------------------------------------


def test_same_context_same_ticker_secondary_noise_is_downgraded() -> None:
    log_filter = YfinanceExpectedMissingLogFilter()
    with yfinance_fetch_context():
        confirmed = _apply(_CONFIRMED_404, log_filter=log_filter)
        secondary = _apply(_DELISTED_NO_TZ, log_filter=log_filter)

    assert confirmed.jstock_signature == SIGNATURE_QUOTE_NOT_FOUND
    assert secondary.levelno == logging.WARNING
    assert secondary.jstock_signature == SIGNATURE_SECONDARY_MISSING
    assert secondary.jstock_stock_code == "9999"


def test_different_ticker_is_not_contaminated() -> None:
    log_filter = YfinanceExpectedMissingLogFilter()
    with yfinance_fetch_context():
        _apply(_CONFIRMED_404_OTHER_TICKER, log_filter=log_filter)
        _assert_untouched(_apply(_DELISTED_NO_TZ, log_filter=log_filter), _DELISTED_NO_TZ)


def test_different_fetch_context_is_not_contaminated() -> None:
    log_filter = YfinanceExpectedMissingLogFilter()
    with yfinance_fetch_context():
        _apply(_CONFIRMED_404, log_filter=log_filter)

    with yfinance_fetch_context():
        _assert_untouched(_apply(_DELISTED_NO_TZ, log_filter=log_filter), _DELISTED_NO_TZ)


def test_state_does_not_leak_outside_context() -> None:
    """warm再利用の模擬: コンテキストを抜けた後に状態が残らない。"""
    log_filter = YfinanceExpectedMissingLogFilter()
    for _ in range(3):
        with yfinance_fetch_context():
            _apply(_CONFIRMED_404, log_filter=log_filter)
        _assert_untouched(_apply(_DELISTED_NO_TZ, log_filter=log_filter), _DELISTED_NO_TZ)


def test_state_is_reset_when_context_exits_with_exception() -> None:
    log_filter = YfinanceExpectedMissingLogFilter()
    with pytest.raises(RuntimeError), yfinance_fetch_context():
        _apply(_CONFIRMED_404, log_filter=log_filter)
        raise RuntimeError("fetch failed")

    _assert_untouched(_apply(_DELISTED_NO_TZ, log_filter=log_filter), _DELISTED_NO_TZ)


def test_nested_context_does_not_inherit_outer_confirmation() -> None:
    log_filter = YfinanceExpectedMissingLogFilter()
    with yfinance_fetch_context():
        _apply(_CONFIRMED_404, log_filter=log_filter)
        with yfinance_fetch_context():
            _assert_untouched(_apply(_DELISTED_NO_TZ, log_filter=log_filter), _DELISTED_NO_TZ)
        # 内側を抜けたら外側の観測結果は健在。
        assert _apply(_DELISTED_NO_TZ, log_filter=log_filter).levelno == logging.WARNING


def test_concurrent_threads_do_not_share_state() -> None:
    """並行実行の模擬: 別スレッドの confirmed 404 が漏れ込まない。"""
    log_filter = YfinanceExpectedMissingLogFilter()
    released = threading.Event()
    confirmed_done = threading.Event()
    results: dict[str, int] = {}

    def confirming() -> None:
        with yfinance_fetch_context():
            _apply(_CONFIRMED_404, log_filter=log_filter)
            confirmed_done.set()
            released.wait(timeout=5)

    def observing() -> None:
        confirmed_done.wait(timeout=5)
        with yfinance_fetch_context():
            results["level"] = _apply(_DELISTED_NO_TZ, log_filter=log_filter).levelno
        released.set()

    threads = [threading.Thread(target=confirming), threading.Thread(target=observing)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert results["level"] == logging.ERROR


def test_correlation_is_disabled_outside_any_context() -> None:
    """コンテキスト外では相関しない(fail-safe側へ倒す)。"""
    log_filter = YfinanceExpectedMissingLogFilter()
    _apply(_CONFIRMED_404, log_filter=log_filter)
    _assert_untouched(_apply(_DELISTED_NO_TZ, log_filter=log_filter), _DELISTED_NO_TZ)


# --- 登録の冪等性 ------------------------------------------------------------


def test_installation_is_idempotent(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("jstock_test.yfinance_filter.idempotent")
    logger.filters.clear()

    assert install_yfinance_expected_missing_log_filter(logger) is True
    assert install_yfinance_expected_missing_log_filter(logger) is False
    assert install_yfinance_expected_missing_log_filter(logger) is False
    assert sum(isinstance(f, YfinanceExpectedMissingLogFilter) for f in logger.filters) == 1

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        logger.error(_CONFIRMED_404)

    records = [r for r in caplog.records if r.name == logger.name]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].getMessage().count("stock_code=9999") == 1


def test_double_applied_filter_does_not_transform_twice() -> None:
    """万一同じレコードへ2回作用しても、降格済みなら再変換しない。"""
    log_filter = YfinanceExpectedMissingLogFilter()
    record = _record(_CONFIRMED_404)

    assert log_filter.filter(record) is True
    first = record.getMessage()
    assert log_filter.filter(record) is True

    assert record.getMessage() == first
    assert record.jstock_original_level == "ERROR"


# --- 統合: 実際のyfinance loggerで降格され、レコードは消えない ---------------


def test_yfinance_logger_emits_downgraded_record(caplog: pytest.LogCaptureFixture) -> None:
    import jstock_advisor.providers.market_data.yfinance_impl  # noqa: F401  登録の副作用

    logger = logging.getLogger(YFINANCE_LOGGER_NAME)
    assert any(isinstance(f, YfinanceExpectedMissingLogFilter) for f in logger.filters)

    with caplog.at_level(logging.DEBUG, logger=YFINANCE_LOGGER_NAME):
        with yfinance_fetch_context():
            logger.error(_CONFIRMED_404)
            logger.error(_DELISTED_NO_TZ)
        logger.error(_TIMEOUT)

    records = [r for r in caplog.records if r.name == YFINANCE_LOGGER_NAME]
    assert len(records) == 3, "レコードは抑止せず必ず残す"
    assert records[0].levelno == logging.WARNING
    assert records[1].levelno == logging.WARNING
    assert records[2].levelno == logging.ERROR
    assert records[2].getMessage() == _TIMEOUT


def test_provider_fetch_establishes_the_correlation_context(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """provider の取得経路が実際にコンテキストを張っていること。"""
    from jstock_advisor.providers.market_data import yfinance_impl as module

    yf_logger = logging.getLogger(YFINANCE_LOGGER_NAME)

    class _LoggingTicker:
        def history(self, **_: object) -> None:
            yf_logger.error(_CONFIRMED_404)
            yf_logger.error(_DELISTED_NO_TZ)
            return None

    monkeypatch.setattr(module.yf, "Ticker", lambda _s: _LoggingTicker())

    with caplog.at_level(logging.DEBUG, logger=YFINANCE_LOGGER_NAME):
        assert module.YFinanceMarketDataProvider().get_latest_price("9999") is None

    records = [r for r in caplog.records if r.name == YFINANCE_LOGGER_NAME]
    assert len(records) == 2
    assert [r.levelno for r in records] == [logging.WARNING, logging.WARNING]
    assert records[1].jstock_signature == SIGNATURE_SECONDARY_MISSING


# --- Issue #59 の provider failure semantics は不変 ---------------------------


def test_issue_59_provider_failure_semantics_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """フィルタ導入後も例外経路・分類・送出条件は従来どおりであること。"""
    from jstock_advisor.interfaces.provider_errors import (
        ProviderDataError,
        ProviderFailureCategory,
    )
    from jstock_advisor.providers.market_data import yfinance_impl as module

    class _RaisingTicker:
        def __init__(self, exc: Exception) -> None:
            self._exc = exc

        def history(self, **_: object) -> None:
            raise self._exc

    provider = module.YFinanceMarketDataProvider()

    monkeypatch.setattr(module.yf, "Ticker", lambda _s: _RaisingTicker(TimeoutError("timed out")))
    with pytest.raises(ProviderDataError) as retryable:
        provider.get_latest_price("1234")
    assert retryable.value.provider_name == "yfinance"
    assert retryable.value.operation == "history"
    assert retryable.value.failure_category == ProviderFailureCategory.RETRYABLE_PROVIDER_FAILURE
    assert retryable.value.retryable is True

    monkeypatch.setattr(module.yf, "Ticker", lambda _s: _RaisingTicker(KeyError("Close")))
    with pytest.raises(ProviderDataError) as non_retryable:
        provider.get_latest_price("1234")
    assert (
        non_retryable.value.failure_category
        == ProviderFailureCategory.NON_RETRYABLE_PROVIDER_FAILURE
    )
    assert non_retryable.value.retryable is False


def test_filter_removal_keeps_logging_functional(caplog: pytest.LogCaptureFixture) -> None:
    """フィルタを外しても機能は壊れない(fail-safe。壊れる場合もERROR側へ倒れる)。"""
    logger = logging.getLogger("jstock_test.yfinance_filter.removal")
    logger.filters.clear()
    install_yfinance_expected_missing_log_filter(logger)
    logger.filters.clear()

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        logger.error(_CONFIRMED_404)

    records = [r for r in caplog.records if r.name == logger.name]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].getMessage() == _CONFIRMED_404
