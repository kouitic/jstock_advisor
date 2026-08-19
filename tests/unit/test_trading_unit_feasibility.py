"""一部売却の実行可能性・数量決定の単体テスト(2026-08、コードレビュー対応Part B)。"""

from __future__ import annotations

import pytest

from jstock_advisor.domain.signals.trading_unit_feasibility import (
    compute_suggested_sell_shares,
    evaluate_trading_unit_feasibility,
)

_TRADING_UNIT = 100


# --- partial_sale_executable(N) ---


def test_100_shares_partial_sale_not_executable() -> None:
    feasibility = evaluate_trading_unit_feasibility(100, _TRADING_UNIT, False)
    assert feasibility.partial_sale_executable is False


def test_100_shares_odd_lot_available_makes_it_executable() -> None:
    feasibility = evaluate_trading_unit_feasibility(100, _TRADING_UNIT, True)
    assert feasibility.partial_sale_executable is True


def test_200_shares_partial_sale_executable() -> None:
    feasibility = evaluate_trading_unit_feasibility(200, _TRADING_UNIT, False)
    assert feasibility.partial_sale_executable is True


# --- 数量(O, P, Q-T: サンリオ8136相当500株) ---


def test_200_shares_any_intensity_suggests_100() -> None:
    for ratio in (0.25, 0.50, 0.60, 0.80):
        result = compute_suggested_sell_shares(200, _TRADING_UNIT, False, ratio)
        assert result is not None
        assert result.shares == 100
        assert 200 - result.shares >= _TRADING_UNIT  # 最低1単元残る


def test_300_shares_boundary() -> None:
    # LIGHT(0.25): raw=75 -> floor 0単元 -> 最低1単元(100)へ切り上げ
    light = compute_suggested_sell_shares(300, _TRADING_UNIT, False, 0.25)
    assert light is not None and light.shares == 100
    # STANDARD(0.50): raw=150 -> floor 1単元 -> 100
    standard = compute_suggested_sell_shares(300, _TRADING_UNIT, False, 0.50)
    assert standard is not None and standard.shares == 100
    # VERY_STRONG(0.80): raw=240 -> floor 2単元 -> 200(残り1単元は保証)
    very_strong = compute_suggested_sell_shares(300, _TRADING_UNIT, False, 0.80)
    assert very_strong is not None and very_strong.shares == 200
    assert 300 - very_strong.shares >= _TRADING_UNIT


def test_500_shares_light_suggests_100() -> None:
    result = compute_suggested_sell_shares(500, _TRADING_UNIT, False, 0.25)
    assert result is not None
    assert result.shares == 100
    assert result.ratio == pytest.approx(0.2)


def test_500_shares_standard_suggests_200() -> None:
    result = compute_suggested_sell_shares(500, _TRADING_UNIT, False, 0.50)
    assert result is not None
    assert result.shares == 200
    assert result.ratio == pytest.approx(0.4)


def test_500_shares_strong_suggests_300() -> None:
    result = compute_suggested_sell_shares(500, _TRADING_UNIT, False, 0.60)
    assert result is not None
    assert result.shares == 300
    assert result.ratio == pytest.approx(0.6)


def test_500_shares_very_strong_suggests_400() -> None:
    result = compute_suggested_sell_shares(500, _TRADING_UNIT, False, 0.80)
    assert result is not None
    assert result.shares == 400
    assert result.ratio == pytest.approx(0.8)


# --- 制約(U, V, W, X) ---


@pytest.mark.parametrize("shares", [200, 300, 400, 500, 600, 1000])
@pytest.mark.parametrize("ratio", [0.25, 0.50, 0.60, 0.80, 0.99])
def test_suggested_never_sells_all_and_leaves_one_unit(shares: int, ratio: float) -> None:
    result = compute_suggested_sell_shares(shares, _TRADING_UNIT, False, ratio)
    assert result is not None
    # U: PARTIALで全量売却しない
    assert result.shares < shares
    # V: 単元未満数量を出さない
    assert result.shares % _TRADING_UNIT == 0
    assert result.shares >= _TRADING_UNIT
    # W: remaining>=1単元
    assert shares - result.shares >= _TRADING_UNIT
    # X: 保有数量超過しない
    assert result.shares <= shares


def test_100_shares_defensive_guard_returns_none() -> None:
    """partial_sale_executable=Falseの状態で呼び出された場合の防御的ガード
    (呼び出し前提違反、単元未満の売却は決して提案しない)。"""
    result = compute_suggested_sell_shares(100, _TRADING_UNIT, False, 0.5)
    assert result is None


def test_1000_shares_strong_suggests_600() -> None:
    result = compute_suggested_sell_shares(1000, _TRADING_UNIT, False, 0.60)
    assert result is not None
    assert result.shares == 600
    assert 1000 - result.shares >= _TRADING_UNIT


def test_odd_lot_trading_available_uses_raw_ratio() -> None:
    result = compute_suggested_sell_shares(150, _TRADING_UNIT, True, 0.5)
    assert result is not None
    assert result.shares == 75
    assert result.shares < 150
