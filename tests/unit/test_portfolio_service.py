import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.services.portfolio_service import PortfolioService


def test_register_purchase_creates_holding(portfolio_service: PortfolioService) -> None:
    holding = portfolio_service.register_purchase(
        stock_code="8136",
        stock_name="サンリオ",
        shares=100,
        purchase_price=Decimal("3775"),
        purchase_date=dt.date(2025, 4, 1),
        account_type=AccountType.NISA,
    )
    assert holding.shares == 100
    assert holding.average_purchase_price == Decimal("3775")


def test_register_purchase_twice_recomputes_average(portfolio_service: PortfolioService) -> None:
    portfolio_service.register_purchase(
        stock_code="8136",
        stock_name="サンリオ",
        shares=100,
        purchase_price=Decimal("3775"),
        purchase_date=dt.date(2025, 4, 1),
        account_type=AccountType.NISA,
    )
    holding = portfolio_service.register_purchase(
        stock_code="8136",
        stock_name=None,
        shares=100,
        purchase_price=Decimal("4025"),
        purchase_date=dt.date(2025, 9, 1),
        account_type=AccountType.NISA,
    )
    assert holding.shares == 200
    assert holding.average_purchase_price == Decimal("3900")
    # stock_nameが未指定の追加購入では既存の銘柄名を保持する
    assert holding.stock_name == "サンリオ"


def test_update_holding_meta_preserves_derived_fields(portfolio_service: PortfolioService) -> None:
    portfolio_service.register_purchase(
        stock_code="8136",
        stock_name="サンリオ",
        shares=100,
        purchase_price=Decimal("3775"),
        purchase_date=dt.date(2025, 4, 1),
        account_type=AccountType.NISA,
    )
    updated = portfolio_service.update_holding_meta(
        "8136", industry="その他製品", profit_target_rate=30.0
    )
    assert updated.industry == "その他製品"
    assert updated.profit_target_rate == 30.0
    # ロット由来のフィールドは変化しない
    assert updated.shares == 100
    assert updated.average_purchase_price == Decimal("3775")


def test_update_holding_meta_missing_stock_raises(portfolio_service: PortfolioService) -> None:
    with pytest.raises(ValueError, match="見つかりません"):
        portfolio_service.update_holding_meta("9999", memo="x")


def test_delete_lot_recomputes_remaining_holding(portfolio_service: PortfolioService) -> None:
    portfolio_service.register_purchase(
        stock_code="8136",
        stock_name="サンリオ",
        shares=100,
        purchase_price=Decimal("3775"),
        purchase_date=dt.date(2025, 4, 1),
        account_type=AccountType.NISA,
    )
    portfolio_service.register_purchase(
        stock_code="8136",
        stock_name=None,
        shares=100,
        purchase_price=Decimal("4025"),
        purchase_date=dt.date(2025, 9, 1),
        account_type=AccountType.NISA,
    )
    lots = portfolio_service.list_lots("8136")
    assert len(lots) == 2

    remaining = portfolio_service.delete_lot("8136", lots[0].lot_id)
    assert remaining is not None
    assert remaining.shares == 100
    assert remaining.average_purchase_price == Decimal("4025")


def test_delete_last_lot_removes_holding(portfolio_service: PortfolioService) -> None:
    portfolio_service.register_purchase(
        stock_code="8136",
        stock_name="サンリオ",
        shares=100,
        purchase_price=Decimal("3775"),
        purchase_date=dt.date(2025, 4, 1),
        account_type=AccountType.NISA,
    )
    lot = portfolio_service.list_lots("8136")[0]
    result = portfolio_service.delete_lot("8136", lot.lot_id)
    assert result is None
    assert portfolio_service.get_holding("8136") is None


def test_delete_holding_removes_lots_too(portfolio_service: PortfolioService) -> None:
    portfolio_service.register_purchase(
        stock_code="8136",
        stock_name="サンリオ",
        shares=100,
        purchase_price=Decimal("3775"),
        purchase_date=dt.date(2025, 4, 1),
        account_type=AccountType.NISA,
    )
    assert portfolio_service.delete_holding("8136") is True
    assert portfolio_service.get_holding("8136") is None
    assert portfolio_service.list_lots("8136") == []
