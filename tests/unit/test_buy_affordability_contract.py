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

import ast
import dataclasses
import datetime as dt
import random
import typing
from decimal import Decimal
from fractions import Fraction
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
from jstock_advisor.domain.signals import buy_affordability as ba
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


# =====================================================================================
# 2 compute_buy_affordability の契約
# =====================================================================================

_MODULE_PATH = _REPO / "src" / "jstock_advisor" / "domain" / "signals" / "buy_affordability.py"
D = Decimal
R = ba.BuyAffordabilityReason


def call(**over: object) -> ba.BuyAffordability:
    kwargs: dict[str, object] = {
        "available_cash": D(1_000_000),
        "candidate_price": D(1000),
        "minimum_trading_unit": 100,
        "current_position_value": D(0),
    }
    kwargs.update(over)
    return ba.compute_buy_affordability(**kwargs)  # type: ignore[arg-type]


def oracle(
    *,
    cash: D,
    price: D,
    unit: int,
    position: D,
    total: D | None,
    amount_cap: D | None,
    ratio_cap: D | None,
) -> int:
    """独立な全探索: 制約を全て満たす最大の購入株数(unit の倍数)。式を使わず不等式で検査する。"""
    best = 0
    n = unit
    while True:
        a = Fraction(price) * n
        if a > Fraction(cash):
            break
        ok = True
        if amount_cap is not None and Fraction(position) + a > Fraction(amount_cap):
            ok = False
        if ratio_cap is not None and ratio_cap < 1:
            assert total is not None
            if (Fraction(position) + a) / (Fraction(total) + a) > Fraction(ratio_cap):
                ok = False
        if ok:
            best = n
        n += unit
        if n > 5000 * unit:
            break
    return best


# --- 2.1 買付余力の意味論: 未登録 / 0 円 / 1 単元未満 / ちょうど / 1 円不足 -----------------


def test_unregistered_cash_is_unavailable_and_is_not_treated_as_zero() -> None:
    got = call(available_cash=None)
    assert got.reason_code is R.AVAILABLE_CASH_UNAVAILABLE
    assert got.max_affordable_shares == 0 and got.feasible is False
    assert got.reason_code is not R.ZERO_AVAILABLE_CASH


def test_unregistered_cash_wins_over_every_other_condition() -> None:
    got = call(
        available_cash=None,
        max_single_stock_ratio=D("0.1"),
        portfolio_value=None,
        max_single_stock_amount=D(0),
    )
    assert got.reason_code is R.AVAILABLE_CASH_UNAVAILABLE


def test_registered_zero_cash_has_its_own_reason() -> None:
    got = call(available_cash=D(0))
    assert got.reason_code is R.ZERO_AVAILABLE_CASH
    assert got.max_affordable_shares == 0 and got.feasible is False


def test_zero_cash_is_distinguishable_from_unregistered_cash() -> None:
    assert call(available_cash=D(0)).reason_code != call(available_cash=None).reason_code


def test_cash_below_one_unit_is_insufficient() -> None:
    got = call(available_cash=D(99_999))  # 1 単元 = 100 株 × 1,000 円 = 100,000 円
    assert got.reason_code is R.INSUFFICIENT_CASH_FOR_MINIMUM_UNIT
    assert got.max_affordable_shares == 0 and got.feasible is False


def test_cash_equal_to_exactly_one_unit_buys_one_unit() -> None:
    got = call(available_cash=D(100_000))
    assert got.max_affordable_shares == 100 and got.max_affordable_units == 1
    assert got.feasible is True and got.reason_code is R.AFFORDABLE


def test_one_yen_short_of_two_units_buys_one_unit() -> None:
    got = call(available_cash=D(199_999))
    assert got.max_affordable_shares == 100


def test_cash_for_many_units_is_floored_to_whole_units() -> None:
    got = call(available_cash=D(812_400), candidate_price=D(2340))  # 234,000 円/単元 -> 3 単元
    assert got.max_affordable_shares == 300 and got.max_affordable_units == 3


def test_the_purchase_total_never_exceeds_the_cash() -> None:
    price, unit = D("1234.5678"), 100
    for cash in (D(1), D(123_456), D("123456.78"), D(9_999_999), D("0.01")):
        got = call(available_cash=cash, candidate_price=price, minimum_trading_unit=unit)
        assert price * got.max_affordable_shares <= cash
        assert got.max_affordable_shares % unit == 0


# --- 2.2 売買単位 -----------------------------------------------------------------------


@pytest.mark.parametrize("unit", [1, 10, 100, 1000])
def test_the_result_is_always_a_whole_number_of_units(unit: int) -> None:
    got = call(available_cash=D(987_654), candidate_price=D(321), minimum_trading_unit=unit)
    assert got.max_affordable_shares % unit == 0
    assert got.max_affordable_units * unit == got.max_affordable_shares
    # 最大性: 次の 1 単元を足すと余力を超える
    assert D(321) * (got.max_affordable_shares + unit) > D(987_654)


def test_odd_lot_purchase_is_only_possible_when_the_caller_passes_a_unit_of_one() -> None:
    assert call(available_cash=D(5_000), candidate_price=D(1000)).max_affordable_shares == 0
    assert (
        call(
            available_cash=D(5_000), candidate_price=D(1000), minimum_trading_unit=1
        ).max_affordable_shares
        == 5
    )


# --- 2.3 1 銘柄最大投資額 ---------------------------------------------------------------


def test_amount_cap_limits_the_quantity_including_the_current_position() -> None:
    # 上限 500,000 円・保有 200,000 円 -> 追加できるのは 300,000 円 = 3 単元(余力は 10,000,000 円)
    got = call(
        available_cash=D(10_000_000),
        current_position_value=D(200_000),
        max_single_stock_amount=D(500_000),
    )
    assert got.max_affordable_shares == 300
    assert got.reason_code is R.POSITION_AMOUNT_CAP_APPLIED


def test_amount_cap_already_reached_gives_zero_with_the_cap_reason() -> None:
    for position in (D(500_000), D(600_000)):
        got = call(
            available_cash=D(10_000_000),
            current_position_value=position,
            max_single_stock_amount=D(500_000),
        )
        assert got.max_affordable_shares == 0 and got.feasible is False
        assert got.reason_code is R.POSITION_AMOUNT_CAP_APPLIED


def test_amount_cap_below_one_unit_gives_zero() -> None:
    got = call(available_cash=D(10_000_000), max_single_stock_amount=D(99_999))
    assert got.max_affordable_shares == 0
    assert got.reason_code is R.POSITION_AMOUNT_CAP_APPLIED


def test_amount_cap_equal_to_exactly_one_unit_buys_one_unit() -> None:
    got = call(available_cash=D(10_000_000), max_single_stock_amount=D(100_000))
    assert got.max_affordable_shares == 100


def test_amount_cap_larger_than_the_cash_does_not_change_the_result() -> None:
    got = call(available_cash=D(300_000), max_single_stock_amount=D(9_000_000))
    assert got.max_affordable_shares == 300 and got.reason_code is R.AFFORDABLE


def test_zero_amount_cap_means_no_purchase() -> None:
    got = call(available_cash=D(1_000_000), max_single_stock_amount=D(0))
    assert got.max_affordable_shares == 0
    assert got.reason_code is R.POSITION_AMOUNT_CAP_APPLIED


# --- 2.4 1 銘柄最大保有比率(add_on_risk の投影と一致) -------------------------------------


def _add_on_projection_passes(
    *, shares: int, price: D, position: D, total: D, ratio: float
) -> bool:
    """既存の add_on_risk に、shares 株を買い増すと仮定して判定させる(集中度の上限を超えないか)。"""
    assessment, _ = evaluate_add_on_eligibility(
        current_market_value=position,
        current_price=price,
        trading_unit=shares,
        portfolio_total_market_value=total,
        sector_total_market_value=None,
        portfolio_valuation_basis=PortfolioValuationBasis.MARKET_VALUE,
        conflicting_holding_action=None,
        holding_data_inconsistent=False,
        holding_is_odd_lot=False,
        config=_add_on_config(ratio),
    )
    return not assessment.position_limit_exceeded


@pytest.mark.parametrize("ratio", [0.1, 0.2, 0.25, 0.3, 0.5])
@pytest.mark.parametrize("position", [D(0), D(50_000), D(200_000)])
def test_the_ratio_cap_matches_the_existing_add_on_risk_projection(
    ratio: float, position: D
) -> None:
    """M2 が返す株数は add_on_risk の判定を通り、次の 1 単元を足すと通らない(境界が完全に一致)。"""
    total, price = D(1_000_000), D(700)
    got = call(
        available_cash=D(100_000_000),
        candidate_price=price,
        current_position_value=position,
        portfolio_value=total,
        max_single_stock_ratio=D(str(ratio)),
    )
    n = got.max_affordable_shares
    if n > 0:
        assert _add_on_projection_passes(
            shares=n, price=price, position=position, total=total, ratio=ratio
        )
    assert not _add_on_projection_passes(
        shares=n + 100, price=price, position=position, total=total, ratio=ratio
    )


def test_the_ratio_cap_uses_the_post_purchase_denominator() -> None:
    """分母に購入額を含む式。分母に含めない近似(比率 × 総額 − 保有額)より、許す株数が多い。"""
    # 保有 0・総額 1,000,000・比率 0.5・価格 1,000: (a) / (1,000,000 + a) <= 0.5 -> a <= 1,000,000
    got = call(
        available_cash=D(100_000_000),
        portfolio_value=D(1_000_000),
        max_single_stock_ratio=D("0.5"),
    )
    assert got.max_affordable_shares == 1000  # 1,000,000 円分(ちょうど境界。等しいのは許す)
    # 近似式なら 500,000 円分(500 株)で止まる
    assert got.max_affordable_shares > 500
    # 既存の add_on_risk でも、1000 株は通り、次の 100 株を足すと通らない
    args = {"price": D(1000), "position": D(0), "total": D(1_000_000), "ratio": 0.5}
    assert _add_on_projection_passes(shares=1000, **args)  # type: ignore[arg-type]
    assert not _add_on_projection_passes(shares=1100, **args)  # type: ignore[arg-type]


def test_ratio_cap_already_exceeded_gives_zero_with_the_cap_reason() -> None:
    got = call(
        available_cash=D(10_000_000),
        current_position_value=D(600_000),
        portfolio_value=D(1_000_000),
        max_single_stock_ratio=D("0.5"),
    )
    assert got.max_affordable_shares == 0
    assert got.reason_code is R.POSITION_RATIO_CAP_APPLIED


def test_ratio_of_one_means_no_cap_and_does_not_need_the_portfolio_value() -> None:
    got = call(available_cash=D(300_000), max_single_stock_ratio=D(1), portfolio_value=None)
    assert got.max_affordable_shares == 300 and got.reason_code is R.AFFORDABLE


def test_no_ratio_cap_does_not_need_the_portfolio_value() -> None:
    assert call(available_cash=D(300_000), portfolio_value=None).max_affordable_shares == 300


@pytest.mark.parametrize(
    "total",
    [None, D(0), D(500)],  # None / 0 / 保有評価額より小さい(矛盾)
)
def test_a_ratio_cap_without_a_usable_portfolio_value_fails_closed(total: D | None) -> None:
    """上限を無視して買わせない(add_on_risk の fail-close と同じ向き)。"""
    got = call(
        available_cash=D(10_000_000),
        current_position_value=D(1_000),
        portfolio_value=total,
        max_single_stock_ratio=D("0.2"),
    )
    assert got.reason_code is R.PORTFOLIO_VALUE_UNAVAILABLE
    assert got.max_affordable_shares == 0 and got.feasible is False


def test_a_zero_portfolio_value_with_no_position_is_still_unusable_for_a_ratio_cap() -> None:
    """総額 0 は『使えない』(比率が定義できない)。保有 0 でも購入不可にする。"""
    got = call(
        available_cash=D(10_000_000),
        current_position_value=D(0),
        portfolio_value=D(0),
        max_single_stock_ratio=D("0.2"),
    )
    assert got.reason_code is R.PORTFOLIO_VALUE_UNAVAILABLE


def test_registered_cash_checks_come_before_the_portfolio_check() -> None:
    got = call(available_cash=D(0), portfolio_value=None, max_single_stock_ratio=D("0.2"))
    assert got.reason_code is R.ZERO_AVAILABLE_CASH


def test_the_portfolio_check_comes_before_the_one_unit_check() -> None:
    got = call(available_cash=D(1), portfolio_value=None, max_single_stock_ratio=D("0.2"))
    assert got.reason_code is R.PORTFOLIO_VALUE_UNAVAILABLE


# --- 2.5 理由の優先順位と同点の解決 -------------------------------------------------------


def test_a_cap_that_equals_the_cash_limit_is_reported_as_affordable() -> None:
    got = call(available_cash=D(300_000), max_single_stock_amount=D(300_000))
    assert got.max_affordable_shares == 300 and got.reason_code is R.AFFORDABLE


def test_a_cap_that_narrows_the_cash_limit_is_reported() -> None:
    got = call(available_cash=D(500_000), max_single_stock_amount=D(300_000))
    assert got.max_affordable_shares == 300 and got.reason_code is R.POSITION_AMOUNT_CAP_APPLIED


def test_amount_and_ratio_caps_that_tie_are_reported_as_the_amount_cap() -> None:
    # 比率で 1,000,000 円分、金額で 1,000,000 円分 -> 同点 -> 金額を理由にする
    got = call(
        available_cash=D(100_000_000),
        portfolio_value=D(1_000_000),
        max_single_stock_ratio=D("0.5"),
        max_single_stock_amount=D(1_000_000),
    )
    assert got.max_affordable_shares == 1000
    assert got.reason_code is R.POSITION_AMOUNT_CAP_APPLIED


def test_the_tighter_of_the_two_caps_is_reported() -> None:
    by_ratio = call(
        available_cash=D(100_000_000),
        portfolio_value=D(1_000_000),
        max_single_stock_ratio=D("0.2"),
        max_single_stock_amount=D(9_000_000),
    )
    assert by_ratio.reason_code is R.POSITION_RATIO_CAP_APPLIED
    by_amount = call(
        available_cash=D(100_000_000),
        portfolio_value=D(1_000_000),
        max_single_stock_ratio=D("0.5"),
        max_single_stock_amount=D(200_000),
    )
    assert by_amount.reason_code is R.POSITION_AMOUNT_CAP_APPLIED
    assert by_amount.max_affordable_shares == 200


def test_cash_shortage_wins_over_caps_when_less_than_one_unit_is_affordable() -> None:
    got = call(available_cash=D(50_000), max_single_stock_amount=D(0))
    assert got.reason_code is R.INSUFFICIENT_CASH_FOR_MINIMUM_UNIT


def test_cash_binding_with_larger_caps_is_affordable() -> None:
    got = call(
        available_cash=D(200_000),
        portfolio_value=D(10_000_000),
        max_single_stock_ratio=D("0.9"),
        max_single_stock_amount=D(9_000_000),
    )
    assert got.max_affordable_shares == 200 and got.reason_code is R.AFFORDABLE


# --- 2.6 独立な全探索との一致(式に依存しない検証) ---------------------------------------


def _decimal(rng: random.Random, low: int, high: int, places: int) -> D:
    return D(rng.randint(low * 10**places, high * 10**places)).scaleb(-places)


def test_the_result_matches_an_independent_exhaustive_search() -> None:
    rng = random.Random(20261011)
    checked = 0
    for _ in range(3000):
        unit = rng.choice([1, 10, 100])
        price = _decimal(rng, 1, 9000, rng.choice([0, 1, 2, 4]))
        unit_cost = price * unit
        cash = unit_cost * D(rng.randint(0, 4000)) / 100
        position = unit_cost * D(rng.randint(0, 2000)) / 100 if rng.random() < 0.6 else D(0)
        total = (
            None if rng.random() < 0.15 else position + unit_cost * D(rng.randint(0, 100_000)) / 100
        )
        amount_cap = None if rng.random() < 0.5 else unit_cost * D(rng.randint(0, 5000)) / 100
        ratio_cap = None if rng.random() < 0.5 else D(rng.randint(1, 100)) / 100
        got = call(
            available_cash=cash,
            candidate_price=price,
            minimum_trading_unit=unit,
            current_position_value=position,
            portfolio_value=total,
            max_single_stock_amount=amount_cap,
            max_single_stock_ratio=ratio_cap,
        )
        ratio_active = ratio_cap is not None and ratio_cap < 1
        if cash == 0:
            assert got.reason_code is R.ZERO_AVAILABLE_CASH
            continue
        if ratio_active and (total is None or total <= 0 or total < position):
            assert got.reason_code is R.PORTFOLIO_VALUE_UNAVAILABLE
            assert got.max_affordable_shares == 0
            continue
        expected = oracle(
            cash=cash,
            price=price,
            unit=unit,
            position=position,
            total=total,
            amount_cap=amount_cap,
            ratio_cap=ratio_cap,
        )
        assert got.max_affordable_shares == expected, (cash, price, unit, position, total)
        checked += 1
    assert checked > 2000


def test_arithmetic_is_exact_beyond_the_decimal_context_precision() -> None:
    """Decimal の既定の演算精度(28 桁)に依存しない: 桁数の多い値でも境界が正確。
    (入力は文字列で厳密に作る。テスト側の Decimal の乗算で丸めが入らないようにする)"""
    price = D("1.000000000000000000000000000001")  # 31 桁
    exactly_seven = D("7.000000000000000000000000000007")  # = price * 7(厳密)
    one_short = D("7.000000000000000000000000000006")  # 最後の桁が 1 つ小さい
    kwargs = {"candidate_price": price, "minimum_trading_unit": 1}
    assert call(available_cash=exactly_seven, **kwargs).max_affordable_shares == 7
    assert call(available_cash=one_short, **kwargs).max_affordable_shares == 6
    huge = D("123456789012345678901234567890.5")
    got = call(available_cash=huge, candidate_price=D(3), minimum_trading_unit=1)
    assert got.max_affordable_shares == int(Fraction(huge) // 3)  # 期待値も厳密な有理数で


# --- 2.7 入力の拒否(専用の例外。値をメッセージに含めない) -----------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"available_cash": 1000},  # int
        {"available_cash": 1000.0},  # float
        {"available_cash": True},  # bool
        {"available_cash": "1000"},  # str
        {"available_cash": D("NaN")},
        {"available_cash": D("Infinity")},
        {"available_cash": D(-1)},
        {"candidate_price": 1000},
        {"candidate_price": 1000.5},
        {"candidate_price": D(0)},
        {"candidate_price": D(-5)},
        {"candidate_price": D("NaN")},
        {"minimum_trading_unit": 0},
        {"minimum_trading_unit": -100},
        {"minimum_trading_unit": True},
        {"minimum_trading_unit": 100.0},
        {"current_position_value": D(-1)},
        {"current_position_value": 5},
        {"portfolio_value": D(-1)},
        {"portfolio_value": 1_000_000},
        {"max_single_stock_amount": D(-1)},
        {"max_single_stock_amount": 500_000.0},
        {"max_single_stock_ratio": D(0)},
        {"max_single_stock_ratio": D("-0.1")},
        {"max_single_stock_ratio": D("1.0001")},
        {"max_single_stock_ratio": 0.5},
        {"max_single_stock_ratio": D("NaN")},
    ],
)
def test_malformed_inputs_are_rejected_with_the_dedicated_exception(
    over: dict[str, object],
) -> None:
    with pytest.raises(ba.BuyAffordabilityInputError) as info:
        call(**over)
    assert isinstance(info.value, ValueError)
    # メッセージに値(金額・株数)を含めない
    text = str(info.value)
    for value in over.values():
        if not isinstance(value, bool) and len(str(value)) >= 4:
            assert str(value) not in text


# 拒否の全経路で、渡した入力の値そのものがメッセージに現れないことを固定する(#909 SHOULD-1)。
# この関数は買付余力・保有評価額(利用者の資産情報)を受け、呼び出し側が例外を捕捉する前提のため、
# 値がメッセージに入ると、将来ログへ出す実装が入ったときに資産情報が漏れる。
# 値は他の文字列と衝突しない数字列にしてある(範囲表記『(0, 1]』のような固定の文言と混ざらない)。
# 『この文字列だけ特別扱いする』形にしない: 探すのは常に『渡した値の文字列』である。
_REJECTION_PATHS: list[tuple[str, dict[str, object]]] = [
    ("cash is not a Decimal (float)", {"available_cash": 12345.678}),
    ("cash is not a Decimal (int)", {"available_cash": 987654}),
    ("cash is not a Decimal (bool)", {"available_cash": True}),
    ("cash is not a Decimal (str)", {"available_cash": "S3CRET-424242"}),
    ("cash is not finite (NaN)", {"available_cash": D("NaN")}),
    ("cash is not finite (Infinity)", {"available_cash": D("Infinity")}),
    ("cash is negative", {"available_cash": D("-424242.5")}),
    ("price is not a Decimal", {"candidate_price": 313131}),
    ("price is not finite", {"candidate_price": D("NaN")}),
    ("price is not positive", {"candidate_price": D("-313131.5")}),
    ("unit is a bool", {"minimum_trading_unit": True}),
    ("unit is a float", {"minimum_trading_unit": 4321.5}),
    ("unit is not positive", {"minimum_trading_unit": -4321}),
    ("position value is negative", {"current_position_value": D("-717171.5")}),
    ("position value is an int", {"current_position_value": 717171}),
    ("portfolio value is negative", {"portfolio_value": D("-818181.5")}),
    ("portfolio value is an int", {"portfolio_value": 818181}),
    ("amount cap is negative", {"max_single_stock_amount": D("-919191.5")}),
    ("amount cap is a float", {"max_single_stock_amount": 919191.5}),
    ("ratio is above 1", {"max_single_stock_ratio": D("1.5551")}),
    ("ratio is negative", {"max_single_stock_ratio": D("-0.7771")}),
    ("ratio is a float", {"max_single_stock_ratio": 0.5551}),
    ("ratio is not finite", {"max_single_stock_ratio": D("NaN")}),
]


def _needles(value: object) -> set[str]:
    """渡した値が文言に現れうる形(str / repr / 数字だけを並べた形)。"""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    found = {str(value), repr(value)}
    if len(digits) >= 5:
        found.add(digits)
    return {n for n in found if n}


@pytest.mark.parametrize(("label", "over"), _REJECTION_PATHS, ids=[p[0] for p in _REJECTION_PATHS])
def test_no_rejection_message_contains_the_offending_value(
    label: str, over: dict[str, object]
) -> None:
    with pytest.raises(ba.BuyAffordabilityInputError) as info:
        call(**over)
    text = str(info.value)
    assert text, label
    for value in over.values():
        for needle in _needles(value):
            assert needle not in text, f"{label}: the message must not contain the input value"


def test_the_rejection_paths_cover_every_input_error_site_in_the_module() -> None:
    # 拒否の経路を足して一覧へ足し忘れると値の漏れを見逃す。ソースの raise の数と突き合わせる。
    tree = ast.parse(Path(ba.__file__).read_text(encoding="utf-8"))
    sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and getattr(node.exc.func, "id", None) == "BuyAffordabilityInputError"
    ]
    # _require_decimal の 3 経路 + 価格 + 単位 2 経路 + 比率の範囲
    assert len(sites) == 7
    # どの経路にも f-string の埋め込み(値の差し込み)が無い(name は変数名であり値ではない例外を除く)
    for node in sites:
        assert isinstance(node.exc, ast.Call)
        for arg in node.exc.args:
            if isinstance(arg, ast.JoinedStr):
                interpolated = [v for v in arg.values if isinstance(v, ast.FormattedValue)]
                assert all(
                    isinstance(v.value, ast.Name) and v.value.id == "name" for v in interpolated
                )


def test_an_invalid_input_is_rejected_even_when_the_cash_is_unregistered() -> None:
    with pytest.raises(ba.BuyAffordabilityInputError):
        call(available_cash=None, candidate_price=D(0))


# --- 2.8 結果の型の不変条件 --------------------------------------------------------------------


def test_the_result_is_immutable() -> None:
    got = call()
    with pytest.raises(dataclasses.FrozenInstanceError):
        got.max_affordable_shares = 1  # type: ignore[misc]


def test_the_recommended_upper_bound_equals_the_maximum_for_now() -> None:
    for cash in (None, D(0), D(50_000), D(5_000_000)):
        got = call(available_cash=cash)
        assert got.recommended_upper_bound_shares == got.max_affordable_shares


def test_feasible_means_a_positive_quantity_for_every_reachable_state() -> None:
    cases = [
        call(available_cash=None),
        call(available_cash=D(0)),
        call(available_cash=D(1)),
        call(available_cash=D(100_000)),
        call(max_single_stock_amount=D(0)),
        call(portfolio_value=None, max_single_stock_ratio=D("0.1")),
    ]
    for got in cases:
        assert got.feasible == (got.max_affordable_shares > 0)
        assert (got.max_affordable_units > 0) == got.feasible
        if got.reason_code is R.AFFORDABLE:
            assert got.feasible


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "max_affordable_shares": -1,
            "max_affordable_units": 0,
            "recommended_upper_bound_shares": -1,
            "feasible": False,
            "reason_code": R.ZERO_AVAILABLE_CASH,
        },
        {
            "max_affordable_shares": 100,
            "max_affordable_units": 1,
            "recommended_upper_bound_shares": 200,
            "feasible": True,
            "reason_code": R.AFFORDABLE,
        },
        {
            "max_affordable_shares": 100,
            "max_affordable_units": 1,
            "recommended_upper_bound_shares": 100,
            "feasible": False,
            "reason_code": R.AFFORDABLE,
        },
        {
            "max_affordable_shares": 0,
            "max_affordable_units": 0,
            "recommended_upper_bound_shares": 0,
            "feasible": False,
            "reason_code": R.AFFORDABLE,
        },
        {
            "max_affordable_shares": 100,
            "max_affordable_units": 1,
            "recommended_upper_bound_shares": 100,
            "feasible": True,
            "reason_code": R.AVAILABLE_CASH_UNAVAILABLE,
        },
        {
            "max_affordable_shares": 100,
            "max_affordable_units": 0,
            "recommended_upper_bound_shares": 100,
            "feasible": True,
            "reason_code": R.AFFORDABLE,
        },
        {
            "max_affordable_shares": 0,
            "max_affordable_units": 1,
            "recommended_upper_bound_shares": 0,
            "feasible": True,
            "reason_code": R.AFFORDABLE,
        },
    ],
)
def test_the_result_type_rejects_inconsistent_construction(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ba.BuyAffordability(**kwargs)  # type: ignore[arg-type]


# --- 2.9 純粋・決定的・dormant ----------------------------------------------------------------


def test_the_function_is_deterministic() -> None:
    kwargs = {
        "available_cash": D("812400.55"),
        "candidate_price": D("2340.5"),
        "minimum_trading_unit": 100,
        "current_position_value": D(120_000),
        "portfolio_value": D(3_000_000),
        "max_single_stock_amount": D(900_000),
        "max_single_stock_ratio": D("0.25"),
    }
    first = ba.compute_buy_affordability(**kwargs)  # type: ignore[arg-type]
    assert all(ba.compute_buy_affordability(**kwargs) == first for _ in range(20))  # type: ignore[arg-type]


def _module_tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def test_the_module_is_pure_and_uses_no_float() -> None:
    tree = _module_tree()
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    forbidden = (
        "jstock_advisor",
        "logging",
        "datetime",
        "time",
        "random",
        "os",
        "boto3",
        "json",
        "pathlib",
        "math",
    )
    assert not [m for m in imported if m.startswith(forbidden)], imported
    assert not [
        n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, float)
    ]
    calls = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "float" not in calls and "round" not in calls


def test_the_module_is_not_imported_from_anywhere_in_src() -> None:
    """dormant: 現行の判定・通知へ配線されていない(配線は #603 の optimizer 以降)。"""
    needles = ("signals.buy_affordability", "import buy_affordability")
    importers = [
        str(path.relative_to(_REPO))
        for path in (_REPO / "src").rglob("*.py")
        if path != _MODULE_PATH
        and any(needle in path.read_text(encoding="utf-8") for needle in needles)
    ]
    assert importers == []


def test_the_floor_is_the_only_rounding_used() -> None:
    """床以外の丸め(天井・四捨五入・quantize)を使わない = 計算方法で余力・上限の超過を防ぐ。"""
    source = _MODULE_PATH.read_text(encoding="utf-8")
    for token in ("ceil", "quantize", "ROUND_", "round("):
        assert token not in source
