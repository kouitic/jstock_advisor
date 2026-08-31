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
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import pandas as pd
import pytest

from jstock_advisor.infrastructure.local_repository.stock_name_override_repository import (
    StockNameOverrideRepository,
)
from jstock_advisor.interfaces.candidate_universe import CandidateUniverseProvider
from jstock_advisor.interfaces.corporate_action import CorporateActionProvider
from jstock_advisor.interfaces.disclosure import DisclosureProvider
from jstock_advisor.interfaces.dividend_data import DividendDataProvider
from jstock_advisor.interfaces.financial_data import FinancialDataProvider
from jstock_advisor.interfaces.market_data import MarketDataProvider
from jstock_advisor.interfaces.news import NewsProvider
from jstock_advisor.interfaces.provider_errors import (
    ProviderDataError,
    ProviderFailureCategory,
)
from jstock_advisor.interfaces.shareholder_benefit import ShareholderBenefitProvider
from jstock_advisor.providers._failure import (
    REDACTED,
    raise_provider_data_error,
    sanitize_error_summary,
)
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


_SECRET = "SUPERSECRET123"


@pytest.mark.parametrize(
    "message",
    [
        f"Subscription-Key={_SECRET}",
        f"https://x.test/path?token={_SECRET}",
        f"https://x.test/path?date=2026-08-29&apikey={_SECRET}",
        f"Authorization: Bearer {_SECRET}",
        f"Authorization: Basic {_SECRET}",
        f"cookie={_SECRET}",
        f"crumb={_SECRET}",
        f"access_token={_SECRET}",
        f"api_key: {_SECRET}",
        # 引用符付きの値(regexが値の先頭quoteで取りこぼさないこと)
        f'token="{_SECRET}"',
        f"token='{_SECRET}'",
        f'api_key="{_SECRET}"',
        f'Authorization: "{_SECRET}"',
        f'Authorization: Bearer "{_SECRET}"',
        f"cookie='{_SECRET}'",
    ],
)
def test_secret_is_redacted_everywhere(
    caplog: pytest.LogCaptureFixture, message: str
) -> None:
    """既知のcredential patternはログ・属性・例外メッセージの3箇所すべてで伏せる。

    従来のテストはsecretを例外メッセージへ実際に含めていなかったため、
    redactionを一切検証できていなかった(Issue #59 の safe logging contract 未達)。
    """
    original = RuntimeError(message)

    with caplog.at_level(logging.WARNING), pytest.raises(ProviderDataError) as excinfo:
        raise_provider_data_error(original, provider_name="yfinance", operation="info")

    err = excinfo.value
    # 1) ログ
    for record in caplog.records:
        assert _SECRET not in record.getMessage()
    # 2) ProviderDataError.error_summary 属性
    assert _SECRET not in err.error_summary
    # 3) 例外メッセージ(str)
    assert _SECRET not in str(err)
    assert REDACTED in err.error_summary
    # 元例外は原因として保持する(ログへは自動出力しない)
    assert err.__cause__ is original
    assert _SECRET in str(original)


def test_quoted_value_with_spaces_is_fully_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """引用符内に空白があっても値全体を伏せる(閉じ引用符までを1つの値とみなす)。"""
    secret_with_spaces = "SUPER SECRET 123"

    with caplog.at_level(logging.WARNING), pytest.raises(ProviderDataError) as excinfo:
        raise_provider_data_error(
            RuntimeError(f'token="{secret_with_spaces}"'),
            provider_name="yfinance",
            operation="info",
        )

    err = excinfo.value
    assert secret_with_spaces not in err.error_summary
    assert secret_with_spaces not in str(err)
    for record in caplog.records:
        assert secret_with_spaces not in record.getMessage()
    assert err.error_summary == f"token={REDACTED}"


def test_sanitize_keeps_non_secret_context() -> None:
    """安全な情報まで過剰に消さない(障害切り分けを妨げない)。"""
    summary = sanitize_error_summary(
        "HTTPError: 429 Too Many Requests for url https://x.test/v8/finance/chart/7203.T"
    )

    assert "429" in summary
    assert "Too Many Requests" in summary
    assert "7203.T" in summary
    assert REDACTED not in summary


def test_error_summary_is_sanitized_before_truncation() -> None:
    """sanitize → truncate の順序(逆だと壊れた断片が伏せられずに残る)。"""
    padding = "x" * 400
    with pytest.raises(ProviderDataError) as excinfo:
        raise_provider_data_error(
            RuntimeError(f"token={_SECRET} {padding}"),
            provider_name="yfinance",
            operation="info",
        )

    summary = excinfo.value.error_summary
    assert _SECRET not in summary
    assert REDACTED in summary
    assert len(summary) == 200


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


# =============================================================================
# Issue #85 Phase B2 / Group 2: provider failure-vs-empty **completeness**
#
# 既存テストは provider method を**手書きで列挙**していたため、
# 「新しい provider method が追加され、取得失敗時に None / [] / 0 へ縮退しても、
#  手書き一覧に入らないので見逃す」という穴があった。
#
# ここでは provider Protocol から method を**機械的に列挙**し、
# すべての method が下記のいずれかへ明示的に分類されていることを強制する。
# 分類漏れ(= Protocol へ method を足しただけ)はテスト失敗になる。
#
#   ENFORCED       : network 由来の取得失敗を ProviderDataError へ変換する
#                    (実際に失敗を注入して振る舞いを検証する)
#   DOMAIN_ERROR   : provider 固有の型付きエラーを送出する契約
#                    (ProviderDataError ではないが、欠測へは縮退しない)
#   NO_REMOTE_FETCH: ローカル登録簿・固定スタブ等で取得失敗が発生しない
#   KNOWN_GAP      : 契約違反が残っている。**必ず related_issue を伴う**
#
# KNOWN_GAP は「見えなくする」ための仕組みではない。#85 では production code を
# 修正しない代わりに、違反を Issue 付きで台帳へ残して追跡可能にする。
# =============================================================================


class _ContractKind(StrEnum):
    ENFORCED = "ENFORCED"
    DOMAIN_ERROR = "DOMAIN_ERROR"
    NO_REMOTE_FETCH = "NO_REMOTE_FETCH"
    KNOWN_GAP = "KNOWN_GAP"


@dataclass(frozen=True)
class _ProviderContract:
    kind: _ContractKind
    reason: str
    #: KNOWN_GAP のときは必須(追跡可能性の担保)。
    related_issue: str | None = None


_PROVIDER_PROTOCOLS: tuple[type, ...] = (
    CandidateUniverseProvider,
    CorporateActionProvider,
    DisclosureProvider,
    DividendDataProvider,
    FinancialDataProvider,
    MarketDataProvider,
    NewsProvider,
    ShareholderBenefitProvider,
)

#: provider Protocol の全 method に対する契約台帳。
#: **Protocol へ method を追加したらここへも登録しないとテストが落ちる。**
_PROVIDER_FAILURE_CONTRACT: dict[str, _ProviderContract] = {
    "MarketDataProvider.get_latest_price": _ProviderContract(
        _ContractKind.ENFORCED, "yfinance history 失敗を ProviderDataError へ変換"
    ),
    "MarketDataProvider.get_average_trading_value": _ProviderContract(
        _ContractKind.ENFORCED, "同上(出来高代金)"
    ),
    "MarketDataProvider.get_price_history": _ProviderContract(
        _ContractKind.ENFORCED, "同上(株価ヒストリー)"
    ),
    "MarketDataProvider.get_benchmark_price_history": _ProviderContract(
        _ContractKind.ENFORCED, "同上(ベンチマーク)"
    ),
    "FinancialDataProvider.get_financial_summary": _ProviderContract(
        _ContractKind.ENFORCED, "Issue #59 B1 で ProviderDataError 化"
    ),
    "FinancialDataProvider.get_cashflow_decomposition": _ProviderContract(
        _ContractKind.ENFORCED, "同上"
    ),
    "FinancialDataProvider.get_earnings_surprise_history": _ProviderContract(
        _ContractKind.ENFORCED, "同上"
    ),
    "FinancialDataProvider.get_historical_valuation": _ProviderContract(
        _ContractKind.ENFORCED, "同上(空listへ縮退しない)"
    ),
    "DividendDataProvider.get_dividend_info": _ProviderContract(
        _ContractKind.ENFORCED, "yfinance 失敗を ProviderDataError へ変換"
    ),
    "CorporateActionProvider.get_corporate_actions": _ProviderContract(
        _ContractKind.ENFORCED, "yfinance splits 失敗を ProviderDataError へ変換"
    ),
    "DisclosureProvider.get_disclosures": _ProviderContract(
        _ContractKind.DOMAIN_ERROR,
        "Issue #53: EdinetFetchStatus.FETCH_FAILED を DisclosureQueryResult へ載せて返す"
        "(SUCCESS_EMPTY と別状態として保持されるため欠測へ縮退しない)",
    ),
    "DisclosureProvider.get_next_earnings_date": _ProviderContract(
        _ContractKind.DOMAIN_ERROR,
        "EarningsDateStatus(CONFIRMED/STALE_PAST_DATE/UNAVAILABLE)で取得不能を表現する",
    ),
    "CandidateUniverseProvider.get_candidate_universe": _ProviderContract(
        _ContractKind.DOMAIN_ERROR,
        "CandidateUniverseError を送出する(空ユニバースへ縮退しない)",
    ),
    "ShareholderBenefitProvider.get_shareholder_benefit": _ProviderContract(
        _ContractKind.NO_REMOTE_FETCH,
        "ローカル優待レジストリ / unavailable スタブのみ。remote fetch を持たない",
    ),
    "NewsProvider.get_news": _ProviderContract(
        _ContractKind.NO_REMOTE_FETCH,
        "現行実装は remote fetch を持たない(将来 remote 化する場合は ENFORCED へ移すこと)",
    ),
}


def _protocol_method_keys() -> list[str]:
    """provider Protocol 群から method を機械的に列挙する。"""
    keys: list[str] = []
    for proto in _PROVIDER_PROTOCOLS:
        for name in dir(proto):
            if name.startswith("_"):
                continue
            if not callable(getattr(proto, name, None)):
                continue
            keys.append(f"{proto.__name__}.{name}")
    return sorted(keys)


def test_every_provider_protocol_method_has_a_declared_failure_contract() -> None:
    """**inventory の完全性**: Protocol の全 method が契約台帳に登録されていること。

    新しい provider method を追加したのに登録を忘れると、ここで落ちる
    (= 手書き列挙の見逃しを構造的に防ぐ)。
    """
    discovered = set(_protocol_method_keys())
    registered = set(_PROVIDER_FAILURE_CONTRACT)

    unregistered = sorted(discovered - registered)
    assert not unregistered, (
        "provider Protocol へ method が追加されたが失敗時契約が宣言されていない: "
        f"{unregistered}。_PROVIDER_FAILURE_CONTRACT へ ENFORCED / DOMAIN_ERROR / "
        "NO_REMOTE_FETCH / KNOWN_GAP のいずれかで登録すること"
    )
    stale = sorted(registered - discovered)
    assert not stale, f"Protocol から消えた method が台帳に残っている: {stale}"


def test_known_gaps_are_always_tracked_by_an_issue() -> None:
    """KNOWN_GAP は必ず Issue 番号を伴うこと(黙って除外しないための歯止め)。"""
    untracked = sorted(
        key
        for key, contract in _PROVIDER_FAILURE_CONTRACT.items()
        if contract.kind is _ContractKind.KNOWN_GAP
        and not (contract.related_issue or "").startswith("#")
    )
    assert not untracked, (
        f"KNOWN_GAP に related_issue(#NN)が無い: {untracked}。"
        "契約違反を追跡不能な形で除外してはならない"
    )


class _RaisingTickerAll:
    """どの属性アクセス・メソッド呼び出しでも provider 障害を送出する fake。"""

    def __getattr__(self, name: str) -> object:
        raise _HttpError(429)

    def history(self, *args: object, **kwargs: object) -> object:
        raise _HttpError(429)

    def get_earnings_history(self) -> object:
        raise _HttpError(429)


def _enforced_invocations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Callable[[], object]]:
    """ENFORCED な method を実際に呼び出すための invocation を組み立てる。

    provider ごとに underlying client の差し込み方が異なるため、
    Protocol の signature から自動生成せず明示的に構築する
    (自動生成は引数の意味を取り違えたときに誤検知を生むため)。
    """
    import jstock_advisor.providers.corporate_action.yfinance_impl as ca_module
    import jstock_advisor.providers.dividend_data.yfinance_impl as dv_module
    import jstock_advisor.providers.financial_data.yfinance_impl as fd_module
    import jstock_advisor.providers.market_data.yfinance_impl as md_module
    from jstock_advisor.services.corporate_action_service import CorporateActionService

    for module in (md_module, fd_module, dv_module, ca_module):
        monkeypatch.setattr(module.yf, "Ticker", lambda _symbol: _RaisingTickerAll())

    market = md_module.YFinanceMarketDataProvider(now=_NOW)
    financial = fd_module.YFinanceFinancialDataProvider(
        now=_NOW, stock_name_override_repository=StockNameOverrideRepository(store_dir=tmp_path)
    )
    corporate = ca_module.YFinanceCorporateActionProvider(now=_NOW)
    dividend = dv_module.YFinanceDividendDataProvider(
        CorporateActionService(corporate, _NOW), now=_NOW
    )
    start, end = _NOW.date() - dt.timedelta(days=30), _NOW.date()
    return {
        "MarketDataProvider.get_latest_price": lambda: market.get_latest_price("7203"),
        "MarketDataProvider.get_average_trading_value": (
            lambda: market.get_average_trading_value("7203", 20)
        ),
        "MarketDataProvider.get_price_history": (
            lambda: market.get_price_history("7203", start, end)
        ),
        "MarketDataProvider.get_benchmark_price_history": (
            lambda: market.get_benchmark_price_history("^N225", start, end)
        ),
        "FinancialDataProvider.get_financial_summary": (
            lambda: financial.get_financial_summary("7203")
        ),
        "FinancialDataProvider.get_cashflow_decomposition": (
            lambda: financial.get_cashflow_decomposition("7203")
        ),
        "FinancialDataProvider.get_earnings_surprise_history": (
            lambda: financial.get_earnings_surprise_history("7203")
        ),
        "FinancialDataProvider.get_historical_valuation": (
            lambda: financial.get_historical_valuation("7203", 5)
        ),
        "DividendDataProvider.get_dividend_info": lambda: dividend.get_dividend_info("7203"),
        "CorporateActionProvider.get_corporate_actions": (
            lambda: corporate.get_corporate_actions("7203", start)
        ),
    }


def test_every_enforced_method_has_an_executable_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENFORCED と宣言した method には必ず実行可能な検証が存在すること。

    宣言だけして検証していない(= 実質 KNOWN_GAP を ENFORCED と偽る)ことを防ぐ。
    """
    enforced = {
        key
        for key, contract in _PROVIDER_FAILURE_CONTRACT.items()
        if contract.kind is _ContractKind.ENFORCED
    }
    invocations = set(_enforced_invocations(tmp_path, monkeypatch))

    assert enforced == invocations, (
        "ENFORCED 宣言と実行可能な検証が一致しない: "
        f"検証欠落={sorted(enforced - invocations)} / 宣言欠落={sorted(invocations - enforced)}"
    )


@pytest.mark.parametrize("method_key", sorted(_PROVIDER_FAILURE_CONTRACT))
def test_enforced_provider_method_raises_instead_of_degrading_to_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method_key: str
) -> None:
    """ENFORCED な全 method が、取得失敗を None / [] / 0 へ縮退させないこと。

    ENFORCED 以外(DOMAIN_ERROR / NO_REMOTE_FETCH / KNOWN_GAP)は
    ここでは検証せず skip する。KNOWN_GAP は
    `test_known_gaps_are_always_tracked_by_an_issue` で追跡性のみ担保する。
    """
    contract = _PROVIDER_FAILURE_CONTRACT[method_key]
    if contract.kind is not _ContractKind.ENFORCED:
        pytest.skip(f"{contract.kind.value}: {contract.reason}")

    invoke = _enforced_invocations(tmp_path, monkeypatch)[method_key]
    with pytest.raises(ProviderDataError):
        invoke()
