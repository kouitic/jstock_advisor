import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.valuation.buy_price import compute_recommended_buy_prices
from jstock_advisor.domain.valuation.fair_value import (
    aggregate_fair_value,
    compute_historical_range_price,
    compute_pbr_price,
    compute_per_price,
    compute_target_total_yield_price,
    compute_target_yield_price,
)
from jstock_advisor.interfaces.types import PriceBar


def test_compute_target_yield_price() -> None:
    price = compute_target_yield_price(Decimal("100"), 4.0)
    assert price == Decimal("2500")


def test_compute_target_yield_price_none_when_no_dividend() -> None:
    assert compute_target_yield_price(Decimal("0"), 4.0) is None


def test_compute_target_total_yield_price_matches_manual_solve() -> None:
    # dividend=100, benefit=3000(100株あたり), target=4% -> price = (100 + 30) / 0.04 = 3250
    price = compute_target_total_yield_price(
        forecast_annual_dividend_per_share=Decimal("100"),
        annual_benefit_value_at_min_lot=Decimal("3000"),
        min_shares_required=100,
        target_total_yield_pct=4.0,
    )
    assert price == Decimal("3250")

    # 検算: そのpriceで実際に総合利回りを計算するとtarget近似になる
    dividend_yield = Decimal("100") / price
    benefit_yield = Decimal("3000") / (100 * price)
    assert dividend_yield + benefit_yield == Decimal("0.04")


def test_compute_per_pbr_price() -> None:
    assert compute_per_price(Decimal("100"), Decimal("15")) == Decimal("1500")
    assert compute_pbr_price(Decimal("500"), Decimal("2")) == Decimal("1000")
    assert compute_per_price(None, Decimal("15")) is None
    assert compute_per_price(Decimal("0"), Decimal("15")) is None


def _bar(date: dt.date, low: str, close: str) -> PriceBar:
    return PriceBar(
        date=date,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(low),
        close=Decimal(close),
        volume=1000,
    )


def test_compute_historical_range_price_uses_available_lows() -> None:
    as_of = dt.date(2026, 7, 24)
    bars = [
        _bar(dt.date(2026, 1, 1), "3000", "3100"),  # 52週内
        _bar(dt.date(2024, 6, 1), "2000", "2100"),  # 3年内、52週外
    ]
    price = compute_historical_range_price(bars, as_of, lookback_years=3, use_52_week_low=True)
    assert price == (Decimal("3000") + Decimal("2000")) / 2


def test_compute_historical_range_price_none_when_no_bars() -> None:
    assert compute_historical_range_price([], dt.date(2026, 7, 24), 3) is None


def test_aggregate_fair_value_median_ignores_missing() -> None:
    result = aggregate_fair_value(
        {
            "target_yield": Decimal("2500"),
            "per": None,
            "pbr": Decimal("3000"),
            "historical_range": None,
        },
        "median",
    )
    assert result == Decimal("2750")  # (2500+3000)/2


def test_aggregate_fair_value_returns_none_when_all_missing() -> None:
    assert aggregate_fair_value({"target_yield": None, "per": None}, "median") is None


def test_aggregate_fair_value_weighted() -> None:
    result = aggregate_fair_value(
        {"target_yield": Decimal("2000"), "per": Decimal("4000")},
        "weighted",
        {"target_yield": 0.25, "per": 0.25, "pbr": 0.25, "historical_range": 0.25},
    )
    assert result == Decimal("3000")


class _Ratios:
    tentative_buy_ratio = 0.95
    standard_buy_ratio = 0.90
    aggressive_buy_ratio = 0.85


def test_compute_recommended_buy_prices_ordering() -> None:
    levels = compute_recommended_buy_prices(Decimal("10000"), _Ratios())
    assert (
        levels.aggressive.price < levels.standard.price < levels.tentative.price < Decimal("10000")
    )
    assert levels.tentative.price == Decimal("9500")
    assert levels.standard.price == Decimal("9000")
    assert levels.aggressive.price == Decimal("8500")
