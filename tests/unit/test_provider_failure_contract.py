"""provider例外semanticsの契約(Issue #59 Phase B1)。

検証対象:
  - `classify_provider_failure` の分類(**従来テストが1件も無かった**)
  - `ProviderDataError` による短絡(分類ロジックを二重実装しない)
  - provider が取得失敗を欠測(None / [] / 0)へ潰さないこと
  - 正常応答での欠測(SUCCESS + missing)は従来どおり値を返すこと
  - 失敗ログに秘密情報が含まれないこと
  - **E-2**: 会計期末を取得できない場合に取得日で代用しないこと
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pandas as pd
import pytest

from jstock_advisor.infrastructure.local_repository.stock_name_override_repository import (
    StockNameOverrideRepository,
)
from jstock_advisor.interfaces.provider_errors import (
    ProviderDataError,
    ProviderFailureCategory,
)
from jstock_advisor.providers._failure import raise_provider_data_error
from jstock_advisor.providers.financial_data.yfinance_impl import YFinanceFinancialDataProvider
from jstock_advisor.services.provider_failure_classifier import classify_provider_failure

_NOW = dt.datetime(2026, 8, 29, 1, 0, tzinfo=dt.UTC)


# --- classify_provider_failure(既存テスト0件だった) ------------------------


class _ResponseWithStatus:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _HttpError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("http error")
        self.response = _ResponseWithStatus(status_code)


@pytest.mark.parametrize("status_code", [403, 429, 500, 502, 503, 504])
def test_classifier_treats_provider_status_codes_as_failure(status_code: int) -> None:
    assert classify_provider_failure(_HttpError(status_code)) is True


@pytest.mark.parametrize(
    "message",
    [
        "429 Too Many Requests",
        "rate limit exceeded",
        "connection reset by peer",
        "read timed out",
        "Invalid crumb",
        "cookie error",
    ],
)
def test_classifier_treats_known_failure_messages_as_failure(message: str) -> None:
    assert classify_provider_failure(RuntimeError(message)) is True


def test_classifier_treats_timeout_type_as_failure() -> None:
    assert classify_provider_failure(TimeoutError("boom")) is True


@pytest.mark.parametrize(
    "exc",
    [
        KeyError("Operating Income"),
        ValueError("no data found for this stock"),
        AttributeError("'NoneType' object has no attribute 'empty'"),
    ],
)
def test_classifier_does_not_treat_normal_missing_data_as_failure(exc: Exception) -> None:
    """「正常な応答だがデータが無い」を障害へ算入しない(既存方針の固定)。"""
    assert classify_provider_failure(exc) is False


# --- ProviderDataError による短絡(二重分類しない) --------------------------


def _provider_error(retryable: bool) -> ProviderDataError:
    return ProviderDataError(
        provider_name="yfinance",
        operation="info",
        retryable=retryable,
        failure_category=(
            ProviderFailureCategory.RETRYABLE_PROVIDER_FAILURE
            if retryable
            else ProviderFailureCategory.NON_RETRYABLE_PROVIDER_FAILURE
        ),
        error_type="RuntimeError",
        error_summary="boom",
    )


@pytest.mark.parametrize("retryable", [True, False])
def test_classifier_short_circuits_on_provider_data_error(retryable: bool) -> None:
    """ラップにより元例外の型名・response・メッセージが失われても判定が壊れないこと。"""
    assert classify_provider_failure(_provider_error(retryable)) is retryable


def test_raise_provider_data_error_marks_retryable_and_preserves_cause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    original = _HttpError(429)

    with caplog.at_level(logging.WARNING), pytest.raises(ProviderDataError) as excinfo:
        raise_provider_data_error(original, provider_name="yfinance", operation="info")

    err = excinfo.value
    assert err.retryable is True
    assert err.failure_category is ProviderFailureCategory.RETRYABLE_PROVIDER_FAILURE
    assert err.provider_name == "yfinance"
    assert err.operation == "info"
    assert err.__cause__ is original
    assert any("provider data fetch failed" in r.getMessage() for r in caplog.records)


def test_raise_provider_data_error_marks_non_retryable() -> None:
    with pytest.raises(ProviderDataError) as excinfo:
        raise_provider_data_error(
            KeyError("Operating Income"), provider_name="yfinance", operation="income_stmt"
        )

    assert excinfo.value.retryable is False
    assert (
        excinfo.value.failure_category
        is ProviderFailureCategory.NON_RETRYABLE_PROVIDER_FAILURE
    )


def test_failure_log_contains_no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    """ログに認証情報・URLクエリ等を出さない(安全要約のみ)。"""
    secret = "Subscription-Key=SUPERSECRET123"
    with caplog.at_level(logging.WARNING), pytest.raises(ProviderDataError):
        raise_provider_data_error(
            RuntimeError("boom"), provider_name="yfinance", operation="info"
        )

    for record in caplog.records:
        message = record.getMessage()
        assert secret not in message
        assert "api_key" not in message.lower()
        assert "subscription-key" not in message.lower()
        assert "token" not in message.lower()


def test_error_summary_is_truncated() -> None:
    with pytest.raises(ProviderDataError) as excinfo:
        raise_provider_data_error(
            RuntimeError("x" * 1000), provider_name="yfinance", operation="info"
        )

    assert len(excinfo.value.error_summary) == 200


# --- financial provider: FAILURE を欠測へ潰さない ---------------------------


class _RaisingTicker:
    """属性アクセスで例外を送出するfake。"""

    def __init__(self, exc: Exception, attrs: tuple[str, ...]) -> None:
        self._exc = exc
        self._attrs = attrs

    def __getattr__(self, name: str) -> object:
        if name in self._attrs:
            raise self._exc
        raise AttributeError(name)

    def get_earnings_history(self) -> pd.DataFrame:
        if "get_earnings_history" in self._attrs:
            raise self._exc
        return pd.DataFrame()


def _provider(tmp_path: Path) -> YFinanceFinancialDataProvider:
    return YFinanceFinancialDataProvider(
        now=_NOW, stock_name_override_repository=StockNameOverrideRepository(store_dir=tmp_path)
    )


def _patch_ticker(monkeypatch: pytest.MonkeyPatch, ticker: object) -> None:
    import jstock_advisor.providers.financial_data.yfinance_impl as module

    monkeypatch.setattr(module.yf, "Ticker", lambda symbol: ticker)


@pytest.mark.parametrize(
    ("method", "attrs"),
    [
        ("get_financial_summary", ("info",)),
        ("get_cashflow_decomposition", ("income_stmt",)),
        ("get_earnings_surprise_history", ("get_earnings_history",)),
    ],
)
def test_financial_provider_raises_instead_of_returning_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, method: str, attrs: tuple[str, ...]
) -> None:
    """取得失敗を None / [] へ潰さず ProviderDataError を送出すること。"""
    _patch_ticker(monkeypatch, _RaisingTicker(_HttpError(429), attrs))

    with pytest.raises(ProviderDataError) as excinfo:
        getattr(_provider(tmp_path), method)("7203")

    assert excinfo.value.retryable is True
    assert excinfo.value.provider_name == "yfinance"


def test_historical_valuation_raises_instead_of_returning_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_ticker(monkeypatch, _RaisingTicker(_HttpError(503), ("income_stmt", "balance_sheet")))

    with pytest.raises(ProviderDataError):
        _provider(tmp_path).get_historical_valuation("7203", 5)


def test_non_retryable_exception_is_also_propagated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """non-retryable でも欠測へ変換しない(failure ≠ missing)。"""
    _patch_ticker(monkeypatch, _RaisingTicker(RuntimeError("unexpected"), ("info",)))

    with pytest.raises(ProviderDataError) as excinfo:
        _provider(tmp_path).get_financial_summary("7203")

    assert excinfo.value.retryable is False


# --- SUCCESS + missing は従来どおり ----------------------------------------


class _EmptyTicker:
    """応答は成立するがデータが無いfake(SUCCESS + missing / empty)。"""

    info: dict[str, object] = {}
    income_stmt = pd.DataFrame()
    balance_sheet = pd.DataFrame()

    def get_earnings_history(self) -> pd.DataFrame:
        return pd.DataFrame()


def test_success_missing_still_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_ticker(monkeypatch, _EmptyTicker())

    assert _provider(tmp_path).get_financial_summary("7203") is None


def test_success_empty_still_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_ticker(monkeypatch, _EmptyTicker())

    assert _provider(tmp_path).get_earnings_surprise_history("7203") == []
    assert _provider(tmp_path).get_historical_valuation("7203", 5) == []


# --- E-2: period_end に取得日を代用しない -----------------------------------


class _CashflowTickerWithoutPeriodEnd:
    """pretax income は取得できるが、income_stmt に期末日を持たないfake。"""

    info: dict[str, object] = {}

    def __init__(self) -> None:
        self.income_stmt = pd.DataFrame({"unknown-column": [1.0]}, index=["Pretax Income"])
        self.cashflow = pd.DataFrame()
        self.quarterly_income_stmt = pd.DataFrame()
        self.quarterly_cashflow = pd.DataFrame()
        self.balance_sheet = pd.DataFrame()


def test_cashflow_period_end_is_none_when_not_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """E-2: 会計期末を取得できない場合、取得日(当日)で代用しない。"""
    _patch_ticker(monkeypatch, _CashflowTickerWithoutPeriodEnd())

    result = _provider(tmp_path).get_cashflow_decomposition("7203")

    assert result is not None
    assert result.period_end is None
    assert result.period_end != _NOW.date()


# --- consumer policy: retry と FAILED / DATA_INSUFFICIENT の分離 --------------


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    import jstock_advisor.services.yfinance_rate_limit as rate_limit_module

    monkeypatch.setattr(rate_limit_module.time, "sleep", lambda seconds: None)


def test_retryable_provider_error_is_retried_and_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retryable な ProviderDataError で実際に再試行が起動し、尽きたら障害疑いとなる。

    従来は provider が例外を握り潰していたため、この経路自体が再現不能だった。
    """
    from jstock_advisor.services.yfinance_rate_limit import call_with_rate_limit_retry

    _no_sleep(monkeypatch)
    calls = {"n": 0}

    def _always_fail() -> None:
        calls["n"] += 1
        raise_provider_data_error(
            _HttpError(429), provider_name="yfinance", operation="info"
        )

    result = call_with_rate_limit_retry(_always_fail)

    assert calls["n"] > 1, "retryable failure must be retried"
    assert result.is_provider_failure_suspected is True
    assert isinstance(result.error, ProviderDataError)
    assert result.value is None


def test_retryable_provider_error_recovers_on_second_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jstock_advisor.services.yfinance_rate_limit import call_with_rate_limit_retry

    _no_sleep(monkeypatch)
    calls = {"n": 0}

    def _fail_once() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise_provider_data_error(
                _HttpError(503), provider_name="yfinance", operation="info"
            )
        return "ok"

    result = call_with_rate_limit_retry(_fail_once)

    assert result.value == "ok"
    assert result.is_provider_failure_suspected is False
    assert result.error is None


def test_non_retryable_provider_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """non-retryable は再試行せず即座に failure として呼び出し側へ伝播する。"""
    from jstock_advisor.services.yfinance_rate_limit import call_with_rate_limit_retry

    _no_sleep(monkeypatch)
    calls = {"n": 0}

    def _fail() -> None:
        calls["n"] += 1
        raise_provider_data_error(
            KeyError("Operating Income"), provider_name="yfinance", operation="income_stmt"
        )

    with pytest.raises(ProviderDataError):
        call_with_rate_limit_retry(_fail)

    assert calls["n"] == 1


@pytest.mark.parametrize(
    "module_name",
    [
        "jstock_advisor.lambda_handlers.buy_candidates_handler",
        "jstock_advisor.lambda_handlers.holdings_watchlist_handler",
    ],
)
def test_batch_handlers_wrap_snapshot_build_with_retry(module_name: str) -> None:
    """BUY / holdings が build_stock_snapshot を再試行ヘルパーで包んでいること。

    包んでいないと、一過性障害が再試行されないまま即 FAILED として記録され、
    従来より情報が失われる(provider の再送出化と同一Phaseで成立させる必要がある)。
    """
    import importlib
    import inspect

    source = inspect.getsource(importlib.import_module(module_name))

    assert "call_with_rate_limit_retry" in source
    assert "build_stock_snapshot(" in source
