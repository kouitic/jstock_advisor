"""保有銘柄SQS worker Lambda(Issue #533。#319 Phase 2)のテスト。

`holdings_watchlist_worker_handler.handler()`が、既存の非同期再帰呼び出し
経路(`holdings_watchlist_handler.handler()`のtask=="holding"分岐)と等価な
処理を行うことを固定する。
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.lambda_handlers import holdings_watchlist_handler
from jstock_advisor.lambda_handlers import holdings_watchlist_worker_handler as worker_module

_NOW = dt.datetime(2026, 8, 24, 7, 0, tzinfo=dt.UTC)


def _sqs_event(body: dict[str, object]) -> dict[str, object]:
    return {"Records": [{"body": json.dumps(body)}]}


class _FakeMarketData:
    def get_latest_price(self, stock_code: str) -> object | None:
        return None


class _FakeProviders:
    market_data = _FakeMarketData()


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


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker_module, "build_real_provider_bundle", lambda now, config: _FakeProviders()
    )
    monkeypatch.setattr(worker_module, "build_line_client_for_run", lambda **kw: object())
    monkeypatch.setattr(
        worker_module,
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


def test_handler_processes_one_record_via_process_single_holding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    target = _holding("2914")
    monkeypatch.setattr(
        holdings_watchlist_handler.HoldingRepository,
        "get",
        lambda self, holding_id: target,
    )
    monkeypatch.setattr(
        holdings_watchlist_handler,
        "build_stock_snapshot",
        lambda *a, **kw: (None, "テストエラー"),
    )

    event = _sqs_event({"task": "holding", "holding_id": build_holding_id(DEFAULT_OWNER, "2914")})

    result = worker_module.handler(event, object())

    assert result == {"processed": 1}


def test_process_one_matches_direct_task_branch_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """workerが呼ぶ`_process_single_holding()`は、既存の
    `holdings_watchlist_handler.handler()`のtask=="holding"分岐が呼ぶものと
    完全に同一の関数であるため、同じ入力に対して同じ戻り値になることを固定する。
    """
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        holdings_watchlist_handler,
        "build_real_provider_bundle",
        lambda now, config: _FakeProviders(),
    )
    monkeypatch.setattr(
        holdings_watchlist_handler, "build_line_client_for_run", lambda **kw: object()
    )
    monkeypatch.setattr(
        holdings_watchlist_handler,
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
    monkeypatch.setattr(
        holdings_watchlist_handler,
        "TradeCooldownService",
        lambda **kw: type(
            "_TC",
            (),
            {
                "detect_and_apply": lambda self, *a, **kw: type(
                    "_Outcome", (), {"confirmed": True, "events": []}
                )()
            },
        )(),
    )
    target = _holding("2914")
    monkeypatch.setattr(
        holdings_watchlist_handler.HoldingRepository,
        "get",
        lambda self, holding_id: target,
    )
    monkeypatch.setattr(
        holdings_watchlist_handler,
        "build_stock_snapshot",
        lambda *a, **kw: (None, "テストエラー"),
    )

    holding_id = build_holding_id(DEFAULT_OWNER, "2914")
    body = {"holding_id": holding_id, "batch_id": None}
    worker_result = worker_module._process_one(body)

    direct_result = holdings_watchlist_handler.handler(
        {"task": "holding", **body}, type("_Ctx", (), {"function_name": "x"})()
    )

    assert (
        worker_result
        == direct_result
        == {
            "holding_id": holding_id,
            "recommended": False,
            "notified": False,
            "evaluation_status": "DATA_INSUFFICIENT",
            "notification_status": "DATA_INSUFFICIENT",
        }
    )
