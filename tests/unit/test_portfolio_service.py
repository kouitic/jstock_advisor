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


def test_partial_sale_does_not_change_last_purchase_date(
    portfolio_service: PortfolioService,
) -> None:
    """FIFO売却は最も古いロットから消費するため、部分売却では
    holding.last_purchase_date(最終購入日)は変化しない(コードレビュー対応
    2026-08、指摘1: Profit Protectionのpeak探索基準日にlast_purchase_dateを
    使ってよいことの実証)。average_purchase_priceは残存ロット構成の変化に
    伴い再計算されて変わりうるが、それは常にlast_purchase_date以前に存在した
    取得原価の再構成であり、last_purchase_dateより新しい取得原価を新たに
    生成することはない。
    """
    portfolio_service.register_purchase(
        stock_code="8136",
        stock_name="サンリオ",
        shares=100,
        purchase_price=Decimal("3775"),
        purchase_date=dt.date(2025, 4, 1),
        account_type=AccountType.NISA,
    )
    holding_before = portfolio_service.register_purchase(
        stock_code="8136",
        stock_name=None,
        shares=100,
        purchase_price=Decimal("4025"),
        purchase_date=dt.date(2025, 9, 1),
        account_type=AccountType.NISA,
    )
    assert holding_before.last_purchase_date == dt.date(2025, 9, 1)
    assert holding_before.last_sale_date is None

    # 最古ロット(2025-4-1、100株)のうち60株を部分売却する。
    holding_after = portfolio_service.sell_shares("8136", 60)
    assert holding_after is not None
    assert holding_after.last_purchase_date == dt.date(2025, 9, 1)  # 不変
    # average_purchase_priceは残存構成(40株@3775 + 100株@4025)へ再計算される。
    assert holding_after.average_purchase_price != holding_before.average_purchase_price
    # last_sale_dateは売却実行時に設定される(コードレビュー対応2026-08、
    # Profit Protectionのpeak探索基準日再リセット用)。
    assert holding_after.last_sale_date is not None


def test_full_lot_sale_does_not_change_last_purchase_date(
    portfolio_service: PortfolioService,
) -> None:
    """最古ロットを全量消費する売却でも、last_purchase_date(最終購入日)は
    変化しない(コードレビュー対応2026-08、指摘1の追加実証。この場合は
    first_purchase_dateの方が繰り上がるが、Profit Protectionが基準日として
    使うのはlast_purchase_dateであり、これは常にどちらの売却パターンでも
    不変であることを確認する)。
    """
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

    # 最古ロット(2025-4-1、100株)を全量消費する売却。
    holding_after = portfolio_service.sell_shares("8136", 100)
    assert holding_after is not None
    assert holding_after.last_purchase_date == dt.date(2025, 9, 1)  # 不変
    assert holding_after.last_sale_date is not None


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


# --- LINEボタン起点会話型UI: build_purchase_write_plan/build_sale_write_plan ---
# (実装プランv2 3節。書き込みを行わず計画のみ返すことを検証する)


def test_build_purchase_write_plan_does_not_write_anything(
    portfolio_service: PortfolioService,
) -> None:
    plan = portfolio_service.build_purchase_write_plan(
        stock_code="8136",
        stock_name="サンリオ",
        shares=100,
        purchase_price=Decimal("3775"),
        purchase_date=dt.date(2025, 4, 1),
        account_type=AccountType.NISA,
        now=_NOW,
    )

    assert portfolio_service.get_holding("8136") is None
    assert portfolio_service.list_lots("8136") == []
    assert plan.lot_put.expected_data is None  # 新規ロット
    assert plan.holding_put.expected_data is None  # 新規Holding
    assert plan.resulting_holding.shares == 100


def test_build_purchase_write_plan_for_additional_buy_captures_existing_raw_data(
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
    before_lots = portfolio_service.list_lots("8136")
    assert len(before_lots) == 1

    plan = portfolio_service.build_purchase_write_plan(
        stock_code="8136",
        stock_name=None,
        shares=100,
        purchase_price=Decimal("4025"),
        purchase_date=dt.date(2025, 9, 1),
        account_type=AccountType.NISA,
        now=_NOW,
    )

    # 計画構築だけでは追加ロットは書き込まれない(既存の1件のみ)。
    assert len(portfolio_service.list_lots("8136")) == 1
    assert plan.lot_put.expected_data is None  # 追加ロット自体は新規
    assert plan.holding_put.expected_data is not None  # 既存Holdingの楽観ロック対象
    assert plan.resulting_holding.shares == 200


def test_build_sale_write_plan_does_not_write_anything(
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

    plan = portfolio_service.build_sale_write_plan("8136", 40, now=_NOW)

    # 部分売却の計画構築だけではロット・Holdingとも変更されない。
    assert portfolio_service.get_holding("8136").shares == 100  # type: ignore[union-attr]
    assert portfolio_service.list_lots("8136")[0].shares == 100
    assert plan.lot_deletes == []
    assert len(plan.lot_puts) == 1
    assert plan.lot_puts[0].expected_data is not None
    assert plan.holding_put is not None
    assert plan.holding_delete is None
    assert plan.resulting_holding.shares == 60  # type: ignore[union-attr]


def test_build_sale_write_plan_full_sell_produces_deletes(
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

    plan = portfolio_service.build_sale_write_plan("8136", 100, now=_NOW)

    assert portfolio_service.get_holding("8136") is not None  # まだ書き込まれていない
    assert len(plan.lot_deletes) == 1
    assert plan.lot_puts == []
    assert plan.holding_put is None
    assert plan.holding_delete is not None
    assert plan.resulting_holding is None


def test_register_purchase_and_sell_shares_behavior_unchanged_via_plan_refactor(
    portfolio_service: PortfolioService,
) -> None:
    """register_purchase()/sell_shares()がbuild_*_write_plan()を呼ぶ薄い
    ラッパーへ再構成された後も、戻り値・実際の永続化結果が従来と一致すること。"""
    holding = portfolio_service.register_purchase(
        stock_code="8136",
        stock_name="サンリオ",
        shares=100,
        purchase_price=Decimal("3775"),
        purchase_date=dt.date(2025, 4, 1),
        account_type=AccountType.NISA,
    )
    assert holding.shares == 100
    assert portfolio_service.get_holding("8136") is not None

    remaining = portfolio_service.sell_shares("8136", 40)
    assert remaining is not None
    assert remaining.shares == 60
    assert portfolio_service.list_lots("8136")[0].shares == 60

    result = portfolio_service.sell_shares("8136", 60)
    assert result is None
    assert portfolio_service.get_holding("8136") is None
    assert portfolio_service.list_lots("8136") == []
