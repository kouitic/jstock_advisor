"""Issue #692(HF系列): holdings_watchlist_handlerのportfolio total estimation
(`_estimate_portfolio_totals()`)での価格取得failureが、技術的failure(例外送出)と
業務上の「価格データなし」(`get_latest_price()`が例外を投げず正常にNoneを返す)を
混同せずUSER通知することの確認。

`_estimate_portfolio_totals()`は`evaluate_household_concentration_and_notify()`
(親Lambdaの1回のLambda実行内で1回だけ呼ばれる)の内側で完結するため、#667/#668
(HANDLED_FAILURE_CORE)とは異なりcross-worker集約(DynamoDB等)を必要としない。
関数の戻り値(4番目の`price_fetch_failed_count`)をそのまま使う。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.interfaces.types import PriceSnapshot
from jstock_advisor.lambda_handlers import holdings_watchlist_handler as handler_module

_NOW = dt.datetime(2026, 10, 2, 0, 0, tzinfo=dt.UTC)


def _holding(stock_code: str) -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        shares=100,
        average_purchase_price=Decimal("1000"),
        total_purchase_amount=Decimal("100000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


class _MarketDataProvider:
    """stock_codeごとに例外送出/None返却/正常応答を切り替えるフェイク。"""

    def __init__(
        self,
        *,
        raise_for: frozenset[str] = frozenset(),
        none_for: frozenset[str] = frozenset(),
    ) -> None:
        self._raise_for = raise_for
        self._none_for = none_for

    def get_latest_price(self, stock_code: str) -> PriceSnapshot | None:
        if stock_code in self._raise_for:
            raise RuntimeError("yfinance boom")
        if stock_code in self._none_for:
            return None
        return PriceSnapshot(
            stock_code=stock_code,
            as_of_date=_NOW.date(),
            close_price=Decimal("1000"),
            source=DataSourceReference(provider="fake-market-data", fetched_at=_NOW),
        )


class _Providers:
    def __init__(self, market_data: _MarketDataProvider) -> None:
        self.market_data = market_data


def test_technical_failure_increments_price_fetch_failed_count() -> None:
    holdings = [_holding("2914"), _holding("8136")]
    providers = _Providers(_MarketDataProvider(raise_for=frozenset({"2914"})))

    _total_mv, _total_cost, _positions, price_fetch_failed_count = (
        handler_module._estimate_portfolio_totals(holdings, providers)
    )

    assert price_fetch_failed_count == 1


def test_business_level_none_does_not_increment_price_fetch_failed_count() -> None:
    """★必須回帰: get_latest_price()が例外を投げずNoneを返す(業務上のデータなし)
    場合は技術的failureとしてカウントしない(#692 ROOT_CAUSEの核心)。"""
    holdings = [_holding("2914"), _holding("8136")]
    providers = _Providers(_MarketDataProvider(none_for=frozenset({"2914"})))

    _total_mv, _total_cost, _positions, price_fetch_failed_count = (
        handler_module._estimate_portfolio_totals(holdings, providers)
    )

    assert price_fetch_failed_count == 0


def test_no_failures_leaves_count_at_zero() -> None:
    holdings = [_holding("2914"), _holding("8136")]
    providers = _Providers(_MarketDataProvider())

    _total_mv, _total_cost, _positions, price_fetch_failed_count = (
        handler_module._estimate_portfolio_totals(holdings, providers)
    )

    assert price_fetch_failed_count == 0


def test_multiple_technical_failures_in_one_run_are_summed() -> None:
    holdings = [_holding("2914"), _holding("8136"), _holding("7203")]
    providers = _Providers(_MarketDataProvider(raise_for=frozenset({"2914", "8136"})))

    _total_mv, _total_cost, _positions, price_fetch_failed_count = (
        handler_module._estimate_portfolio_totals(holdings, providers)
    )

    assert price_fetch_failed_count == 3 - 1  # 3銘柄中2銘柄が失敗(1銘柄のみ成功)


# --- evaluate_household_concentration_and_notify()からの通知発行 ----------------------


def test_evaluate_household_concentration_notifies_handled_failure_when_price_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notified: list[tuple[str, str]] = []
    monkeypatch.setattr(
        handler_module,
        "_notify_handled_failure_safely",
        lambda stage, reason, now: notified.append((stage, reason)),
    )
    monkeypatch.setattr(
        handler_module,
        "_estimate_portfolio_totals",
        lambda holdings, providers: (None, Decimal("0"), [], 2),
    )

    from jstock_advisor.config.loader import load_config
    from jstock_advisor.domain.entities.execution_context import ExecutionContext
    from jstock_advisor.services.rule_version_service import RuleVersionService

    handler_module.evaluate_household_concentration_and_notify(
        [],
        object(),
        load_config(),
        object(),
        object(),
        RuleVersionService(),
        _NOW,
        True,
        ExecutionContext.normal(),
    )

    assert notified == [
        ("PORTFOLIO_TOTAL_ESTIMATION", "HOLDINGS_WATCHLIST_PORTFOLIO_PRICE_FETCH_FAILED")
    ]


def test_evaluate_household_concentration_does_not_notify_when_no_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notified: list[tuple[str, str]] = []
    monkeypatch.setattr(
        handler_module,
        "_notify_handled_failure_safely",
        lambda stage, reason, now: notified.append((stage, reason)),
    )
    monkeypatch.setattr(
        handler_module,
        "_estimate_portfolio_totals",
        lambda holdings, providers: (Decimal("0"), Decimal("0"), [], 0),
    )

    from jstock_advisor.config.loader import load_config
    from jstock_advisor.domain.entities.execution_context import ExecutionContext
    from jstock_advisor.services.rule_version_service import RuleVersionService

    handler_module.evaluate_household_concentration_and_notify(
        [],
        object(),
        load_config(),
        object(),
        object(),
        RuleVersionService(),
        _NOW,
        True,
        ExecutionContext.normal(),
    )

    assert notified == []
