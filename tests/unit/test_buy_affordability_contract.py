"""BUY 側の購入可能株数の純粋関数(Issue #603 M2。#128 Child 4 の部品。dormant)の契約テスト。

## この file の構成

    1 再利用する既存の意味論の固定(先行テスト)   ← 最初の commit。新しい module はまだ無い
    2 compute_buy_affordability の契約             ← 以降の commit

## 1 の目的

USER の指示(#122 issuecomment-6100082651): 『既存の売買単位、現在価格、買付余力の意味論を再利用
する。未登録余力を 0 円扱いしない』。M2 が前提にする既存の意味論を、新しい関数を書く前に固定する
(既存の意味論が動いたら、M2 の前提が崩れたことに気づく)。

    ・売買単位: profit_taking_rules.yaml の default_trading_unit = 100(単元未満株は既定 False)
    ・買付余力: 未登録(レコードなし = None)と 0 円(正当な状態)は別。負値は作れない
    ・銘柄集中度: add_on_risk の投影は『購入後の構成比 = (保有額 + 購入額) / (総額 + 購入額)』
      (分母に購入額を含む)。M2 の比率上限は、この式に揃える
"""

from __future__ import annotations

import datetime as dt
import typing
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from jstock_advisor.config.models import AddOnRulesConfig
from jstock_advisor.domain.entities.available_cash import AvailableCash
from jstock_advisor.domain.entities.enums import (
    AvailableCashUpdateType,
    PortfolioValuationBasis,
)
from jstock_advisor.domain.signals.add_on_risk import evaluate_add_on_eligibility
from jstock_advisor.services.available_cash_service import AvailableCashService

_REPO = Path(__file__).resolve().parents[2]
_NOW = dt.datetime(2026, 10, 11, 0, 0, tzinfo=dt.UTC)


# --- 1 再利用する既存の意味論の固定 ----------------------------------------------------


def test_the_shipped_trading_unit_is_100_and_odd_lot_trading_is_not_assumed() -> None:
    with (_REPO / "config" / "profit_taking_rules.yaml").open(encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    assert loaded["trading_unit"]["default_trading_unit"] == 100
    assert loaded["trading_unit"]["default_odd_lot_trading_available"] is False


def _add_on_config(ratio: float) -> AddOnRulesConfig:
    return AddOnRulesConfig(
        version=1,
        enabled=True,
        block_add_on_single_stock_ratio=ratio,
        block_add_on_sector_ratio=1.0,
        block_on_sell_signal=True,
        require_holding_data_consistency=True,
        block_add_on_on_odd_lot=False,
    )


@pytest.mark.parametrize(
    ("position", "total", "price", "unit"),
    [
        (Decimal(0), Decimal(1_000_000), Decimal(500), 100),
        (Decimal(200_000), Decimal(1_000_000), Decimal(1234), 100),
        (Decimal(990_000), Decimal(1_000_000), Decimal(9000), 100),
        (Decimal(300_000), Decimal(300_000), Decimal(100), 100),
    ],
)
def test_the_existing_concentration_projection_has_the_purchase_in_both_numerator_and_denominator(
    position: Decimal, total: Decimal, price: Decimal, unit: int
) -> None:
    assessment, _ = evaluate_add_on_eligibility(
        current_market_value=position,
        current_price=price,
        trading_unit=unit,
        portfolio_total_market_value=total,
        sector_total_market_value=None,
        portfolio_valuation_basis=PortfolioValuationBasis.MARKET_VALUE,
        conflicting_holding_action=None,
        holding_data_inconsistent=False,
        holding_is_odd_lot=False,
        config=_add_on_config(0.5),
    )
    added = price * unit
    assert assessment.projected_add_on_amount == added
    assert assessment.projected_position_ratio == (position + added) / (total + added)


def test_the_existing_projection_is_unavailable_without_a_usable_portfolio_total() -> None:
    """総額が使えない(None / 0 以下)と比率を計算しない = fail-close(Issue #82)。M2 も同じ向き。"""
    for total in (None, Decimal(0)):
        assessment, _ = evaluate_add_on_eligibility(
            current_market_value=Decimal(100_000),
            current_price=Decimal(1000),
            trading_unit=100,
            portfolio_total_market_value=total,
            sector_total_market_value=None,
            portfolio_valuation_basis=PortfolioValuationBasis.MARKET_VALUE,
            conflicting_holding_action=None,
            holding_data_inconsistent=False,
            holding_is_odd_lot=False,
            config=_add_on_config(0.5),
        )
        assert assessment.portfolio_data_reliable is False
        assert assessment.projected_position_ratio is None


def test_available_cash_zero_is_a_valid_registered_state_and_negative_is_rejected() -> None:
    zero = AvailableCash(
        owner="owner-a",
        available_cash=Decimal(0),
        updated_at=_NOW,
        last_update_type=AvailableCashUpdateType.USER_RECONCILIATION,
    )
    assert zero.available_cash == Decimal(0)
    with pytest.raises((ValidationError, ValueError)):
        AvailableCash(
            owner="owner-a",
            available_cash=Decimal(-1),
            updated_at=_NOW,
            last_update_type=AvailableCashUpdateType.USER_RECONCILIATION,
        )


def test_the_service_reports_an_unregistered_owner_as_none_not_as_zero() -> None:
    """『未登録』は戻り値 None で表す(0 円ではない)。M2 は None を UNAVAILABLE として扱う。"""
    hints = typing.get_type_hints(AvailableCashService.get)
    assert type(None) in typing.get_args(hints["return"])
