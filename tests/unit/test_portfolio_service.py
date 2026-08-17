import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import AccountType, CorporateActionType
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.interfaces.types import CorporateActionEvent
from jstock_advisor.services.corporate_action_service import CorporateActionService
from jstock_advisor.services.portfolio_service import PortfolioService

_NOW = dt.datetime(2026, 7, 27, tzinfo=dt.UTC)
_SOURCE = DataSourceReference(provider="test", fetched_at=_NOW)


class _FixedCorporateActionProvider:
    def __init__(self, events: list[CorporateActionEvent]) -> None:
        self._events = events

    def get_corporate_actions(self, stock_code: str, since: dt.date) -> list[CorporateActionEvent]:
        return [e for e in self._events if e.stock_code == stock_code]


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


def test_sell_shares_consumes_oldest_lot_first(portfolio_service: PortfolioService) -> None:
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
    holding = portfolio_service.sell_shares("8136", 60)
    assert holding is not None
    assert holding.shares == 140
    remaining_lots = portfolio_service.list_lots("8136")
    assert len(remaining_lots) == 2
    oldest = next(lot for lot in remaining_lots if lot.purchase_date == dt.date(2025, 4, 1))
    assert oldest.shares == 40


def test_sell_shares_removes_fully_consumed_lot_and_spills_to_next(
    portfolio_service: PortfolioService,
) -> None:
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
    holding = portfolio_service.sell_shares("8136", 150)
    assert holding is not None
    assert holding.shares == 50
    remaining_lots = portfolio_service.list_lots("8136")
    assert len(remaining_lots) == 1
    assert remaining_lots[0].purchase_date == dt.date(2025, 9, 1)
    assert remaining_lots[0].shares == 50


def test_sell_all_shares_removes_holding(portfolio_service: PortfolioService) -> None:
    portfolio_service.register_purchase(
        stock_code="8136",
        stock_name="サンリオ",
        shares=100,
        purchase_price=Decimal("3775"),
        purchase_date=dt.date(2025, 4, 1),
        account_type=AccountType.NISA,
    )
    result = portfolio_service.sell_shares("8136", 100)
    assert result is None
    assert portfolio_service.get_holding("8136") is None
    assert portfolio_service.list_lots("8136") == []


def test_sell_shares_rejects_oversell(portfolio_service: PortfolioService) -> None:
    portfolio_service.register_purchase(
        stock_code="8136",
        stock_name="サンリオ",
        shares=100,
        purchase_price=Decimal("3775"),
        purchase_date=dt.date(2025, 4, 1),
        account_type=AccountType.NISA,
    )
    with pytest.raises(ValueError, match="保有株数"):
        portfolio_service.sell_shares("8136", 101)


def test_sell_shares_unheld_stock_raises(portfolio_service: PortfolioService) -> None:
    with pytest.raises(ValueError, match="購入ロットがありません"):
        portfolio_service.sell_shares("9999", 10)


def test_recompute_all_adjusts_shares_and_price_for_past_split(tmp_path: Path) -> None:
    """5401日本製鉄相当のケース: 分割前に買った保有銘柄を、分割後基準で遡及調整する。"""
    store_dir = tmp_path / "store"
    holding_repo = HoldingRepository(store_dir=store_dir)
    lot_repo = PurchaseLotRepository(store_dir=store_dir)

    # 分割調整なしでまず登録(通常のCLI登録相当)
    plain_service = PortfolioService(holding_repository=holding_repo, lot_repository=lot_repo)
    plain_service.register_purchase(
        stock_code="5401",
        stock_name="日本製鉄",
        shares=100,
        purchase_price=Decimal("3500"),
        purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.GENERAL,
    )
    before = plain_service.get_holding("5401")
    assert before is not None
    assert before.shares == 100
    assert before.average_purchase_price == Decimal("3500")
    assert before.shares_and_price_adjustment_basis_date is None

    # 1:5分割が2025-10-01に発生していたことが後から判明 → 遡及調整
    events = [
        CorporateActionEvent(
            stock_code="5401",
            event_type=CorporateActionType.SPLIT,
            announced_date=dt.date(2025, 10, 1),
            effective_date=dt.date(2025, 10, 1),
            ratio=Decimal("5"),
            source=_SOURCE,
        )
    ]
    corporate_action_service = CorporateActionService(
        _FixedCorporateActionProvider(events), now=_NOW
    )
    adjusting_service = PortfolioService(
        holding_repository=holding_repo,
        lot_repository=lot_repo,
        corporate_action_service=corporate_action_service,
    )
    after = adjusting_service._recompute_holding("5401")  # noqa: SLF001 - 遡及調整の直接検証

    assert after.shares == 500  # 100株 * 5
    assert after.average_purchase_price == Decimal("700")  # 3500円 / 5
    assert after.total_purchase_amount == Decimal("350000")  # 支出総額は不変
    # _recompute_holding()は実時刻(dt.datetime.now)を基準日として使うため、_NOWではなく
    # 実行時の日付と比較する(_NOWはCorporateActionServiceのイベント有効性判定にのみ使用)。
    # JST暦日(evaluation_date_jst)基準であり、UTC生日付(.date())とは異なりうる。
    assert after.shares_and_price_adjustment_basis_date == evaluation_date_jst(
        dt.datetime.now(dt.UTC)
    )

    # PurchaseLot(購入時の生データ)自体は書き換えられていないことを確認
    lots = adjusting_service.list_lots("5401")
    assert len(lots) == 1
    assert lots[0].shares == 100
    assert lots[0].purchase_price == Decimal("3500")
