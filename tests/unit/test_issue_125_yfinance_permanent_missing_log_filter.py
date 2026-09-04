"""Issue #125: yfinanceの「期待される恒久missing」ログ降格フィルタの契約。

契約の骨子:
  降格するのは**期待される恒久missingのsignatureに一致するERRORのみ**。
  timeout / connection error / HTTP 5xx / 想定外例外は`ERROR`のまま素通しする。
  レコードは削除しない(消すのではなくERRORではないと分かる形で残す)。
  Issue #59のprovider failure semantics(分類・retryability・送出条件)は不変。
"""

from __future__ import annotations

import logging

import pytest

from jstock_advisor.providers.market_data._yfinance_log_filter import (
    YFINANCE_LOGGER_NAME,
    YfinanceExpectedMissingLogFilter,
    install_yfinance_expected_missing_log_filter,
)

# 実際にyfinanceが出力する恒久missingの文言。
_DELISTED_NO_TZ = "$1234.T: possibly delisted; no timezone found"
_DELISTED_NO_PRICE = "$5678.T: possibly delisted; no price data found (period=1d)"
_QUOTE_404 = (
    "Failed to get ticker '9999.T' reason: "
    "404 Client Error: Not Found for url: https://example.invalid/v7/finance/quote"
)

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
_UNEXPECTED = "Failed to get ticker '1234.T' reason: KeyError('Close')"


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


def _apply(message: str, *, level: int = logging.ERROR) -> logging.LogRecord:
    record = _record(message, level=level)
    assert YfinanceExpectedMissingLogFilter().filter(record) is True
    return record


# --- A/B/C: 期待される恒久missingはstructured WARNINGへ降格される -------------


@pytest.mark.parametrize(
    ("message", "signature", "ticker", "stock_code"),
    [
        (_DELISTED_NO_TZ, "PERMANENT_MISSING_SYMBOL", "1234.T", "1234"),
        (_DELISTED_NO_PRICE, "PERMANENT_MISSING_SYMBOL", "5678.T", "5678"),
        (_QUOTE_404, "QUOTE_NOT_FOUND_404", "9999.T", "9999"),
    ],
)
def test_expected_permanent_missing_is_downgraded_to_structured_warning(
    message: str, signature: str, ticker: str, stock_code: str
) -> None:
    record = _apply(message)

    assert record.levelno == logging.WARNING
    assert record.levelname == "WARNING"
    assert record.jstock_expected_permanent_missing is True
    assert record.jstock_provider == "yfinance"
    assert record.jstock_signature == signature
    assert record.jstock_ticker == ticker
    assert record.jstock_stock_code == stock_code
    assert record.jstock_original_level == "ERROR"

    rendered = record.getMessage()
    assert f"stock_code={stock_code}" in rendered
    assert f"signature={signature}" in rendered
    assert "expected=true" in rendered
    assert "original_level=ERROR" in rendered


# --- D/E/F/G: 真の障害はERRORのまま一切改変しない ----------------------------


@pytest.mark.parametrize(
    "message",
    [_TIMEOUT, _CONNECTION_ERROR, _SERVER_ERROR, _UNEXPECTED],
)
def test_real_provider_failure_stays_error_untouched(message: str) -> None:
    record = _apply(message)

    assert record.levelno == logging.ERROR
    assert record.levelname == "ERROR"
    assert record.getMessage() == message
    assert not hasattr(record, "jstock_expected_permanent_missing")


def test_failed_to_get_ticker_prefix_alone_is_not_sufficient() -> None:
    """接頭辞は例外種別を問わない共通経路から出るため、単独では降格根拠にならない。"""
    record = _apply("Failed to get ticker '1234.T' reason: something went wrong")

    assert record.levelno == logging.ERROR
    assert record.getMessage().endswith("something went wrong")


# --- H: 一致しないレコード・非ERRORは改変しない ------------------------------


def test_unrelated_error_record_is_untouched() -> None:
    message = "Yahoo API request failed for an unrelated reason"
    record = _apply(message)

    assert record.levelno == logging.ERROR
    assert record.getMessage() == message


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO, logging.WARNING])
def test_non_error_records_are_untouched(level: int) -> None:
    record = _apply(_DELISTED_NO_TZ, level=level)

    assert record.levelno == level
    assert record.getMessage() == _DELISTED_NO_TZ
    assert not hasattr(record, "jstock_expected_permanent_missing")


def test_lazy_formatted_record_is_matched_after_rendering() -> None:
    record = logging.LogRecord(
        name=YFINANCE_LOGGER_NAME,
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="$%s: possibly delisted; %s",
        args=("1234.T", "no timezone found"),
        exc_info=None,
    )
    assert YfinanceExpectedMissingLogFilter().filter(record) is True

    assert record.levelno == logging.WARNING
    assert record.jstock_stock_code == "1234"


# --- I: 登録は冪等。多重登録でも変換・出力が重複しない ------------------------


def test_installation_is_idempotent(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("jstock_test.yfinance_filter.idempotent")
    logger.filters.clear()

    assert install_yfinance_expected_missing_log_filter(logger) is True
    assert install_yfinance_expected_missing_log_filter(logger) is False
    assert install_yfinance_expected_missing_log_filter(logger) is False
    assert sum(isinstance(f, YfinanceExpectedMissingLogFilter) for f in logger.filters) == 1

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        logger.error(_DELISTED_NO_TZ)

    records = [r for r in caplog.records if r.name == logger.name]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].getMessage().count("stock_code=1234") == 1


def test_double_applied_filter_does_not_transform_twice() -> None:
    """万一同じレコードへ2回作用しても、降格済みなら再変換しない。"""
    log_filter = YfinanceExpectedMissingLogFilter()
    record = _record(_DELISTED_NO_TZ)

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
        logger.error(_DELISTED_NO_TZ)
        logger.error(_TIMEOUT)

    records = [r for r in caplog.records if r.name == YFINANCE_LOGGER_NAME]
    assert len(records) == 2, "レコードは抑止せず必ず残す"
    assert records[0].levelno == logging.WARNING
    assert records[1].levelno == logging.ERROR
    assert records[1].getMessage() == _TIMEOUT


# --- J: Issue #59 の provider failure semantics は不変 -----------------------


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
        logger.error(_DELISTED_NO_TZ)

    records = [r for r in caplog.records if r.name == logger.name]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].getMessage() == _DELISTED_NO_TZ
