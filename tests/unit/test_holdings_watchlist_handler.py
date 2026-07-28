import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.lambda_handlers import holdings_watchlist_handler as handler_module

_NOW = dt.datetime(2026, 7, 29, 7, 0, tzinfo=dt.UTC)


class _FakeContext:
    function_name = "jstock-advisor-holdings-watchlist"


def _holding(stock_code: str) -> Holding:
    return Holding(
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


def _watchlist_item(stock_code: str) -> WatchlistItem:
    return WatchlistItem(
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler_module, "build_real_provider_bundle", lambda now, config: object())
    monkeypatch.setattr(handler_module, "build_line_client_from_env", lambda: object())
    monkeypatch.setattr(
        handler_module,
        "LineNotificationService",
        lambda **kwargs: type(
            "_Svc",
            (),
            {
                "notify_data_error": lambda self, *a, **kw: False,
                "notify_recommendation": lambda self, *a, **kw: False,
            },
        )(),
    )


def test_dispatch_mode_dispatches_one_call_per_holding_and_watchlist_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    holdings = [_holding("2914"), _holding("8136")]
    items = [_watchlist_item("7203")]

    monkeypatch.setattr(
        handler_module.PortfolioService, "list_holdings", lambda self: holdings
    )
    monkeypatch.setattr(handler_module.WatchlistService, "list_items", lambda self: items)

    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        handler_module,
        "dispatch_async",
        lambda function_name, payload: dispatched.append({"fn": function_name, **payload}),
    )

    result = handler_module.handler({}, _FakeContext())

    assert result == {"dispatched_holdings": 2, "dispatched_watchlist": 1}
    assert {"fn": "jstock-advisor-holdings-watchlist", "task": "holding", "stock_code": "2914"} in (
        dispatched
    )
    assert {"fn": "jstock-advisor-holdings-watchlist", "task": "holding", "stock_code": "8136"} in (
        dispatched
    )
    assert {
        "fn": "jstock-advisor-holdings-watchlist",
        "task": "watchlist",
        "stock_code": "7203",
    } in dispatched
    assert len(dispatched) == 3


def test_task_holding_processes_only_requested_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)
    target = _holding("2914")

    def _get_holding(self: object, stock_code: str) -> Holding | None:
        return target if stock_code == "2914" else None

    monkeypatch.setattr(handler_module.PortfolioService, "get_holding", _get_holding)
    monkeypatch.setattr(
        handler_module, "build_stock_snapshot", lambda *a, **kw: (None, "テストエラー")
    )

    result = handler_module.handler({"task": "holding", "stock_code": "2914"}, _FakeContext())

    assert result == {"stock_code": "2914", "recommended": False, "notified": False}


def test_task_holding_not_found_reports_found_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(handler_module.PortfolioService, "get_holding", lambda self, code: None)

    result = handler_module.handler({"task": "holding", "stock_code": "9999"}, _FakeContext())

    assert result == {
        "stock_code": "9999",
        "recommended": False,
        "notified": False,
        "found": False,
    }
