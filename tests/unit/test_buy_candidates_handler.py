import datetime as dt

import pytest

from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.lambda_handlers import buy_candidates_handler as handler_module

_NOW = dt.datetime(2026, 7, 29, 7, 0, tzinfo=dt.UTC)


class _FakeContext:
    function_name = "jstock-advisor-buy-candidates"


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


def test_dispatch_mode_dispatches_one_call_per_watchlist_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    items = [_watchlist_item("2914"), _watchlist_item("8136")]
    monkeypatch.setattr(handler_module.WatchlistService, "list_items", lambda self: items)

    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        handler_module,
        "dispatch_async",
        lambda function_name, payload: dispatched.append({"fn": function_name, **payload}),
    )

    result = handler_module.handler({}, _FakeContext())

    assert result == {"dispatched": 2}
    assert {
        "fn": "jstock-advisor-buy-candidates",
        "task": "buy_candidate",
        "stock_code": "2914",
    } in dispatched
    assert {
        "fn": "jstock-advisor-buy-candidates",
        "task": "buy_candidate",
        "stock_code": "8136",
    } in dispatched


def test_task_buy_candidate_processes_only_requested_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(handler_module.WatchlistService, "get_item", lambda self, code: None)

    class _FakeOutcome:
        data_error = "テストエラー"
        recommendation = None

    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: _FakeOutcome()
    )

    result = handler_module.handler(
        {"task": "buy_candidate", "stock_code": "2914"}, _FakeContext()
    )

    assert result == {"stock_code": "2914", "recommended": False, "notified": False}
