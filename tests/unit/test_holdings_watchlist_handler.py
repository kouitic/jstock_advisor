import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.enums import AccountType, RecommendationType
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


class _FakeMarketData:
    def get_latest_price(self, stock_code: str) -> object | None:
        return None


class _FakeProviders:
    market_data = _FakeMarketData()


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handler_module, "build_real_provider_bundle", lambda now, config: _FakeProviders()
    )
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
    assert len(dispatched) == 3
    # 全ディスパッチが同一のbatch_idを共有していることを確認する
    batch_ids = {d["batch_id"] for d in dispatched}
    assert len(batch_ids) == 1

    def _without_batch_id(payload: dict[str, object]) -> dict[str, object]:
        return {k: v for k, v in payload.items() if k != "batch_id"}

    stripped = [_without_batch_id(d) for d in dispatched]
    # 保有銘柄タスクにはポートフォリオ集中リスク判定用の全体集計値が付与される
    # (要求仕様§14)。フェイクのmarket_dataは常にNoneを返すため時価総額ベースは
    # 算出不能(None)、取得価格ベースは2銘柄分(10万円×2)が合算される。
    assert {
        "fn": "jstock-advisor-holdings-watchlist",
        "task": "holding",
        "stock_code": "2914",
        "portfolio_total_market_value": None,
        "portfolio_total_acquisition_cost": "200000",
    } in stripped
    assert {
        "fn": "jstock-advisor-holdings-watchlist",
        "task": "holding",
        "stock_code": "8136",
        "portfolio_total_market_value": None,
        "portfolio_total_acquisition_cost": "200000",
    } in stripped
    assert {
        "fn": "jstock-advisor-holdings-watchlist",
        "task": "watchlist",
        "stock_code": "7203",
    } in stripped


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

    # データ取得エラー時は評価監査ステータス(要求仕様§12)も併せて返す
    assert result == {
        "stock_code": "2914",
        "recommended": False,
        "notified": False,
        "evaluation_status": "DATA_INSUFFICIENT",
        "notification_status": "DATA_INSUFFICIENT",
    }


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


@dataclass(frozen=True)
class _FakeSnapshot:
    current_price: Decimal


class _NoSignalOutcome:
    recommendation = None
    data_error = None


def test_task_holding_hold_category_and_portfolio_concentration_notified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sell/profit_takingがともに無シグナルでも、単一銘柄で取得価格ベースの保有比率が
    閾値(20%)を超える場合はPORTFOLIO_CONCENTRATION_REVIEW通知が別途送られ(要求仕様§14)、
    かつ評価監査上のカテゴリはNO_SIGNAL相当の"hold"になる(要求仕様§12・§13)。
    """
    _patch_common(monkeypatch)
    target = _holding("2914")
    monkeypatch.setattr(handler_module.PortfolioService, "get_holding", lambda self, code: target)
    monkeypatch.setattr(
        handler_module,
        "build_stock_snapshot",
        lambda *a, **kw: (_FakeSnapshot(current_price=Decimal("1200")), None),
    )
    monkeypatch.setattr(
        handler_module.SellSignalService, "analyze", lambda self, *a, **kw: _NoSignalOutcome()
    )
    monkeypatch.setattr(
        handler_module.ProfitTakingService, "analyze", lambda self, *a, **kw: _NoSignalOutcome()
    )

    notified: list[object] = []
    monkeypatch.setattr(
        handler_module,
        "LineNotificationService",
        lambda **kwargs: type(
            "_Svc",
            (),
            {
                "notify_data_error": lambda self, *a, **kw: False,
                "notify_recommendation": lambda self, rec, now: notified.append(rec) or True,
                "notify_recommendation_with_status": lambda self, *a, **kw: None,
            },
        )(),
    )

    result = handler_module.handler(
        {
            "task": "holding",
            "stock_code": "2914",
            # 単一銘柄で全体の取得価格を占めるため取得価格ベースの比率は100%になる
            "portfolio_total_market_value": None,
            "portfolio_total_acquisition_cost": "100000",
        },
        _FakeContext(),
    )

    assert result["evaluation_status"] == "COMPLETED"
    assert len(notified) == 1
    concentration_recommendation = notified[0]
    assert concentration_recommendation.recommendation_type == (
        RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW
    )
    assert concentration_recommendation.portfolio_acquisition_cost_weight_pct == pytest.approx(
        100.0
    )
