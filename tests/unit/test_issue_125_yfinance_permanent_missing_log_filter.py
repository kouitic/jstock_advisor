"""Issue #125: yfinanceの「期待される恒久missing」ログ降格フィルタの契約。

契約の骨子:
  降格するのは``ERROR``ちょうどのみ。``CRITICAL``は決して降格しない。
  Production実形式(HTTP Error 404 + 応答body)は、bodyを意味的に解釈し、
  抽出tickerが取得中のtickerと一致する場合にのみ降格する。
  旧形式(Failed to get ticker ...)の判定は後方互換として維持する。
  `possibly delisted` は**単独では降格しない**。同一取得コンテキスト内の
  確定404と相関が取れた場合に限り二次ノイズとして降格する。
  レコードは削除しない(消すのではなくERRORではないと分かる形で残す)。
  Issue #59のprovider failure semantics(分類・retryability・送出条件)は不変。

Production実ログはPUBLIC_SANITIZEDのため、銘柄コードを一般化したfixtureで表現する
(実在の対象銘柄・log stream・batch id・request idはテストへ持ち込まない)。
"""

from __future__ import annotations

import json
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

_TICKER = "1234.T"
_OTHER_TICKER = "5678.T"


def _quote_not_found_body(ticker: str = _TICKER) -> str:
    return (
        '{"quoteSummary":{"result":null,"error":{"code":"Not Found",'
        f'"description":"Quote not found for symbol: {ticker}"}}}}}}'
    )


def _http_404(body: str, *, reason_phrase: str = "") -> str:
    """Production実形式: `str(例外)` と応答bodyの単純連結(区切り文字なし)。"""
    return f"HTTP Error 404: {reason_phrase}{body}"


_PROD_404 = _http_404(_quote_not_found_body())
_PROD_404_WITH_REASON = _http_404(_quote_not_found_body(), reason_phrase="Not Found")

# 曖昧な文言。恒久missingでも一時障害でも同じ形になる。
_DELISTED_NO_TZ = f"${_TICKER}: possibly delisted; no timezone found"
_DELISTED_OTHER = f"${_OTHER_TICKER}: possibly delisted; no timezone found"
_DELISTED_NO_PRICE = (
    f"${_TICKER}: possibly delisted; no price data found (1d 2026-08-01 -> 2026-09-04)"
)

# 旧経路の確定404(後方互換)。
_LEGACY_404 = (
    f"Failed to get ticker '{_TICKER}' reason: "
    "404 Client Error: Not Found for url: https://example.invalid/v8/finance/chart"
)

# 真の障害。いずれも降格してはならない。
_TIMEOUT = (
    f"Failed to get ticker '{_TICKER}' reason: "
    "HTTPSConnectionPool(host='example.invalid', port=443): Read timed out. (read timeout=30)"
)
_CONNECTION_ERROR = (
    f"Failed to get ticker '{_TICKER}' reason: "
    "HTTPSConnectionPool(host='example.invalid', port=443): Max retries exceeded "
    "(Caused by NewConnectionError('Failed to establish a new connection'))"
)
_UNEXPECTED = f"Failed to get ticker '{_TICKER}' reason: KeyError('Close')"


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


def _apply_in_context(
    message: str,
    *,
    ticker: str = _TICKER,
    level: int = logging.ERROR,
) -> logging.LogRecord:
    with yfinance_fetch_context(ticker):
        return _apply(message, level=level)


def _assert_untouched(record: logging.LogRecord, message: str) -> None:
    assert record.levelno == logging.ERROR
    assert record.levelname == "ERROR"
    assert record.getMessage() == message
    assert not hasattr(record, "jstock_expected_permanent_missing")


# --- 1〜5: Production実形式の陽性 --------------------------------------------


def test_production_real_format_is_downgraded_with_structured_fields() -> None:
    record = _apply_in_context(_PROD_404)

    assert record.levelno == logging.WARNING
    assert record.levelname == "WARNING"
    assert record.jstock_expected_permanent_missing is True
    assert record.jstock_provider == "yfinance"
    assert record.jstock_signature == SIGNATURE_QUOTE_NOT_FOUND
    assert record.jstock_ticker == _TICKER
    assert record.jstock_stock_code == "1234"
    assert record.jstock_reason == f"Quote not found for symbol: {_TICKER}"
    assert record.jstock_original_level == "ERROR"

    rendered = record.getMessage()
    assert "provider=yfinance" in rendered
    assert "stock_code=1234" in rendered
    assert f"ticker={_TICKER}" in rendered
    assert f"signature={SIGNATURE_QUOTE_NOT_FOUND}" in rendered
    assert "expected=true" in rendered
    assert "original_level=ERROR" in rendered


def test_reason_phrase_presence_does_not_matter() -> None:
    """HTTP/2ではreason句が空になり得るため、その有無に依存してはならない。"""
    for message in (_PROD_404, _PROD_404_WITH_REASON):
        record = _apply_in_context(message)
        assert record.levelno == logging.WARNING
        assert record.jstock_signature == SIGNATURE_QUOTE_NOT_FOUND


def test_json_whitespace_variation_is_matched() -> None:
    body = json.dumps(
        {
            "quoteSummary": {
                "result": None,
                "error": {
                    "code": "Not Found",
                    "description": f"Quote not found for symbol: {_TICKER}",
                },
            }
        },
        indent=2,
    )
    record = _apply_in_context(_http_404(body))

    assert record.levelno == logging.WARNING
    assert record.jstock_ticker == _TICKER


def test_json_key_order_variation_is_matched() -> None:
    body = (
        '{"quoteSummary":{"error":{"description":"Quote not found for symbol: '
        f'{_TICKER}","code":"Not Found"}},"result":null}}}}'
    )
    record = _apply_in_context(_http_404(body))

    assert record.levelno == logging.WARNING
    assert record.jstock_ticker == _TICKER


def test_different_ticker_is_matched_in_its_own_context() -> None:
    record = _apply_in_context(
        _http_404(_quote_not_found_body(_OTHER_TICKER)), ticker=_OTHER_TICKER
    )

    assert record.levelno == logging.WARNING
    assert record.jstock_ticker == _OTHER_TICKER
    assert record.jstock_stock_code == "5678"


# --- 6〜18: fail-safe ---------------------------------------------------------


def test_malformed_json_stays_error() -> None:
    message = _http_404('{"quoteSummary":{"error":{"code":"Not Found",')
    _assert_untouched(_apply_in_context(message), message)


def test_html_body_stays_error() -> None:
    message = _http_404("<html><body><h1>404 Not Found</h1></body></html>")
    _assert_untouched(_apply_in_context(message), message)


def test_plain_text_body_stays_error() -> None:
    message = _http_404("Not Found", reason_phrase="Not Found: ")
    _assert_untouched(_apply_in_context(message), message)


def test_body_without_quote_summary_stays_error() -> None:
    message = _http_404(
        '{"quoteResponse":{"result":[],"error":{"code":"Not Found",'
        f'"description":"Quote not found for symbol: {_TICKER}"}}}}}}'
    )
    _assert_untouched(_apply_in_context(message), message)


def test_body_without_error_object_stays_error() -> None:
    message = _http_404('{"quoteSummary":{"result":null,"error":null}}')
    _assert_untouched(_apply_in_context(message), message)


def test_code_other_than_not_found_stays_error() -> None:
    message = _http_404(
        '{"quoteSummary":{"result":null,"error":{"code":"Unauthorized",'
        f'"description":"Quote not found for symbol: {_TICKER}"}}}}}}'
    )
    _assert_untouched(_apply_in_context(message), message)


def test_description_mismatch_stays_error() -> None:
    message = _http_404(
        '{"quoteSummary":{"result":null,"error":{"code":"Not Found",'
        '"description":"Invalid crumb"}}}'
    )
    _assert_untouched(_apply_in_context(message), message)


def test_ticker_extraction_failure_stays_error() -> None:
    message = _http_404(
        '{"quoteSummary":{"result":null,"error":{"code":"Not Found",'
        '"description":"Quote not found for symbol: "}}}'
    )
    _assert_untouched(_apply_in_context(message), message)


def test_message_ticker_not_matching_fetch_context_stays_error() -> None:
    """取得中の銘柄と一致しない404は降格しない(取り違えの構造的防止)。"""
    message = _http_404(_quote_not_found_body(_OTHER_TICKER))
    _assert_untouched(_apply_in_context(message, ticker=_TICKER), message)


def test_production_format_outside_any_context_stays_error() -> None:
    _assert_untouched(_apply(_PROD_404), _PROD_404)


@pytest.mark.parametrize("status", ["400", "401", "403", "429", "500", "503"])
def test_non_404_statuses_stay_error(status: str) -> None:
    message = f"HTTP Error {status}: {_quote_not_found_body()}"
    _assert_untouched(_apply_in_context(message), message)


@pytest.mark.parametrize("message", [_TIMEOUT, _CONNECTION_ERROR, _UNEXPECTED])
def test_real_provider_failure_stays_error(message: str) -> None:
    _assert_untouched(_apply_in_context(message), message)


def test_oversized_body_is_not_parsed_and_stays_error() -> None:
    padding = " " * 70_000
    message = _http_404(_quote_not_found_body(), reason_phrase=padding)
    _assert_untouched(_apply_in_context(message), message)


# --- 19: CRITICAL は決して降格しない -----------------------------------------


@pytest.mark.parametrize("message", [_PROD_404, _LEGACY_404, _DELISTED_NO_TZ])
def test_critical_is_never_downgraded(message: str) -> None:
    with yfinance_fetch_context(_TICKER):
        _apply(_PROD_404)  # 相関条件を満たす状態にする
        record = _apply(message, level=logging.CRITICAL)

    assert record.levelno == logging.CRITICAL
    assert record.levelname == "CRITICAL"
    assert record.getMessage() == message
    assert not hasattr(record, "jstock_expected_permanent_missing")


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO, logging.WARNING])
def test_non_error_records_are_untouched(level: int) -> None:
    record = _apply_in_context(_PROD_404, level=level)

    assert record.levelno == level
    assert record.getMessage() == _PROD_404
    assert not hasattr(record, "jstock_expected_permanent_missing")


# --- 20〜21: 旧 signature の後方互換 -----------------------------------------


def test_legacy_confirmed_404_is_still_downgraded_without_context() -> None:
    """旧契約は取得コンテキストを要求せず、従来どおり降格する。"""
    record = _apply(_LEGACY_404)

    assert record.levelno == logging.WARNING
    assert record.jstock_signature == SIGNATURE_QUOTE_NOT_FOUND
    assert record.jstock_ticker == _TICKER


def test_legacy_prefix_without_markers_stays_error() -> None:
    message = f"Failed to get ticker '{_TICKER}' reason: something went wrong"
    _assert_untouched(_apply(message), message)


@pytest.mark.parametrize("message", [_DELISTED_NO_TZ, _DELISTED_NO_PRICE])
def test_possibly_delisted_standalone_stays_error(message: str) -> None:
    """恒久missingでもtimeout等でも同じ文言になるため、単独では降格根拠にならない。"""
    _assert_untouched(_apply(message), message)
    _assert_untouched(_apply_in_context(message), message)


# --- 22〜26: 二次相関 ---------------------------------------------------------


def test_new_format_confirmation_downgrades_same_ticker_secondary() -> None:
    log_filter = YfinanceExpectedMissingLogFilter()
    with yfinance_fetch_context(_TICKER):
        confirmed = _apply(_PROD_404, log_filter=log_filter)
        secondary = _apply(_DELISTED_NO_TZ, log_filter=log_filter)

    assert confirmed.jstock_signature == SIGNATURE_QUOTE_NOT_FOUND
    assert secondary.levelno == logging.WARNING
    assert secondary.jstock_signature == SIGNATURE_SECONDARY_MISSING
    assert secondary.jstock_stock_code == "1234"


def test_secondary_for_different_ticker_stays_error() -> None:
    log_filter = YfinanceExpectedMissingLogFilter()
    with yfinance_fetch_context(_TICKER):
        _apply(_PROD_404, log_filter=log_filter)
        _assert_untouched(_apply(_DELISTED_OTHER, log_filter=log_filter), _DELISTED_OTHER)


def test_secondary_in_different_context_stays_error() -> None:
    log_filter = YfinanceExpectedMissingLogFilter()
    with yfinance_fetch_context(_TICKER):
        _apply(_PROD_404, log_filter=log_filter)

    with yfinance_fetch_context(_TICKER):
        _assert_untouched(_apply(_DELISTED_NO_TZ, log_filter=log_filter), _DELISTED_NO_TZ)


def test_timeout_derived_possibly_delisted_stays_error() -> None:
    """★ timeoutは同一コンテキスト内で `possibly delisted` を誘発するが降格しない。"""
    log_filter = YfinanceExpectedMissingLogFilter()
    with yfinance_fetch_context(_TICKER):
        _assert_untouched(_apply(_TIMEOUT, log_filter=log_filter), _TIMEOUT)
        _assert_untouched(_apply(_DELISTED_NO_TZ, log_filter=log_filter), _DELISTED_NO_TZ)


def test_no_correlation_remains_after_context_exit() -> None:
    log_filter = YfinanceExpectedMissingLogFilter()
    with yfinance_fetch_context(_TICKER):
        _apply(_PROD_404, log_filter=log_filter)

    _assert_untouched(_apply(_DELISTED_NO_TZ, log_filter=log_filter), _DELISTED_NO_TZ)


def test_nested_context_does_not_inherit_outer_confirmation() -> None:
    log_filter = YfinanceExpectedMissingLogFilter()
    with yfinance_fetch_context(_TICKER):
        _apply(_PROD_404, log_filter=log_filter)
        with yfinance_fetch_context(_TICKER):
            _assert_untouched(_apply(_DELISTED_NO_TZ, log_filter=log_filter), _DELISTED_NO_TZ)
        assert _apply(_DELISTED_NO_TZ, log_filter=log_filter).levelno == logging.WARNING


# --- 27〜30: 安全性 -----------------------------------------------------------


def test_warm_reuse_does_not_leak_state() -> None:
    """warm再利用の模擬: コンテキストを抜けた後に状態が残らない。"""
    log_filter = YfinanceExpectedMissingLogFilter()
    for _ in range(3):
        with yfinance_fetch_context(_TICKER):
            _apply(_PROD_404, log_filter=log_filter)
        _assert_untouched(_apply(_DELISTED_NO_TZ, log_filter=log_filter), _DELISTED_NO_TZ)


def test_state_is_reset_when_context_exits_with_exception() -> None:
    log_filter = YfinanceExpectedMissingLogFilter()
    with pytest.raises(RuntimeError), yfinance_fetch_context(_TICKER):
        _apply(_PROD_404, log_filter=log_filter)
        raise RuntimeError("fetch failed")

    _assert_untouched(_apply(_DELISTED_NO_TZ, log_filter=log_filter), _DELISTED_NO_TZ)


def test_concurrent_contexts_do_not_share_state() -> None:
    """並行実行の模擬: 別スレッドの確定404が漏れ込まない。"""
    log_filter = YfinanceExpectedMissingLogFilter()
    released = threading.Event()
    confirmed_done = threading.Event()
    results: dict[str, int] = {}

    def confirming() -> None:
        with yfinance_fetch_context(_TICKER):
            _apply(_PROD_404, log_filter=log_filter)
            confirmed_done.set()
            released.wait(timeout=5)

    def observing() -> None:
        confirmed_done.wait(timeout=5)
        with yfinance_fetch_context(_TICKER):
            results["level"] = _apply(_DELISTED_NO_TZ, log_filter=log_filter).levelno
        released.set()

    threads = [threading.Thread(target=confirming), threading.Thread(target=observing)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert results["level"] == logging.ERROR


def test_record_whose_formatting_raises_is_passed_through() -> None:
    """解析前段の整形が失敗してもログ経路を壊さず、レコードをそのまま通す。"""

    class _Exploding:
        def __str__(self) -> str:
            raise ValueError("boom")

    record = logging.LogRecord(
        name=YFINANCE_LOGGER_NAME,
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="HTTP Error 404: %s",
        args=(_Exploding(),),
        exc_info=None,
    )
    assert YfinanceExpectedMissingLogFilter().filter(record) is True

    assert record.levelno == logging.ERROR
    assert not hasattr(record, "jstock_expected_permanent_missing")


def test_double_applied_filter_does_not_transform_twice() -> None:
    log_filter = YfinanceExpectedMissingLogFilter()
    with yfinance_fetch_context(_TICKER):
        record = _record(_PROD_404)
        assert log_filter.filter(record) is True
        first = record.getMessage()
        assert log_filter.filter(record) is True

    assert record.getMessage() == first
    assert record.jstock_original_level == "ERROR"


def test_installation_is_idempotent(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("jstock_test.yfinance_filter.idempotent")
    logger.filters.clear()

    assert install_yfinance_expected_missing_log_filter(logger) is True
    assert install_yfinance_expected_missing_log_filter(logger) is False
    assert sum(isinstance(f, YfinanceExpectedMissingLogFilter) for f in logger.filters) == 1

    with caplog.at_level(logging.DEBUG, logger=logger.name), yfinance_fetch_context(_TICKER):
        logger.error(_PROD_404)

    records = [r for r in caplog.records if r.name == logger.name]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].getMessage().count("stock_code=1234") == 1


# --- 統合: 実 logger / provider 経路 -----------------------------------------


def test_yfinance_logger_reclassifies_without_suppressing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import jstock_advisor.providers.market_data.yfinance_impl  # noqa: F401  登録の副作用

    logger = logging.getLogger(YFINANCE_LOGGER_NAME)
    assert any(isinstance(f, YfinanceExpectedMissingLogFilter) for f in logger.filters)

    with caplog.at_level(logging.DEBUG, logger=YFINANCE_LOGGER_NAME):
        with yfinance_fetch_context(_TICKER):
            logger.error(_PROD_404)
            logger.error(_DELISTED_NO_TZ)
        logger.error(_TIMEOUT)

    records = [r for r in caplog.records if r.name == YFINANCE_LOGGER_NAME]
    assert len(records) == 3, "レコードは抑止せず必ず残す"
    assert records[0].levelno == logging.WARNING
    assert records[1].levelno == logging.WARNING
    assert records[1].jstock_signature == SIGNATURE_SECONDARY_MISSING
    assert records[2].levelno == logging.ERROR
    assert records[2].getMessage() == _TIMEOUT


def test_provider_fetch_reproduces_production_sequence(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Production で観測された `404 -> possibly delisted` の並びを provider 経路で再現する。"""
    from jstock_advisor.providers.market_data import yfinance_impl as module

    yf_logger = logging.getLogger(YFINANCE_LOGGER_NAME)

    class _LoggingTicker:
        def history(self, **_: object) -> None:
            yf_logger.error(_PROD_404)
            yf_logger.error(_DELISTED_NO_TZ)
            return None

    monkeypatch.setattr(module.yf, "Ticker", lambda _s: _LoggingTicker())

    with caplog.at_level(logging.DEBUG, logger=YFINANCE_LOGGER_NAME):
        assert module.YFinanceMarketDataProvider().get_latest_price("1234") is None

    records = [r for r in caplog.records if r.name == YFINANCE_LOGGER_NAME]
    assert [r.levelno for r in records] == [logging.WARNING, logging.WARNING]
    assert records[0].jstock_signature == SIGNATURE_QUOTE_NOT_FOUND
    assert records[1].jstock_signature == SIGNATURE_SECONDARY_MISSING


def test_provider_fetch_keeps_uncorrelated_possibly_delisted_as_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Production の「404 を伴わない銘柄」に相当する系列は ERROR のまま残る。"""
    from jstock_advisor.providers.market_data import yfinance_impl as module

    yf_logger = logging.getLogger(YFINANCE_LOGGER_NAME)

    class _LoggingTicker:
        def history(self, **_: object) -> None:
            yf_logger.error(_DELISTED_NO_TZ)
            return None

    monkeypatch.setattr(module.yf, "Ticker", lambda _s: _LoggingTicker())

    with caplog.at_level(logging.DEBUG, logger=YFINANCE_LOGGER_NAME):
        assert module.YFinanceMarketDataProvider().get_latest_price("1234") is None

    records = [r for r in caplog.records if r.name == YFINANCE_LOGGER_NAME]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR


# --- 31: Issue #59 の provider failure semantics は不変 -----------------------


def test_issue_59_provider_failure_semantics_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        logger.error(_PROD_404)

    records = [r for r in caplog.records if r.name == logger.name]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].getMessage() == _PROD_404
