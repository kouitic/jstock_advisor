from decimal import Decimal

from jstock_advisor.config.models import NearBuyConfig
from jstock_advisor.domain.entities.enums import BuyAction
from jstock_advisor.domain.signals.near_buy import (
    compute_best_distance_pct,
    compute_consecutive_business_days,
    evaluate_stale,
    meets_near_buy_continue_conditions,
    meets_near_buy_start_conditions,
)

_CONFIG = NearBuyConfig(
    start_required_decline_pct=10.0,
    continue_required_decline_pct=12.0,
    min_company_quality_score=60.0,
    daily_max_notifications=5,
    max_stale_business_days=5,
)


def test_start_conditions_met_at_exactly_10_pct() -> None:
    assert meets_near_buy_start_conditions(
        BuyAction.WATCH_FOR_PRICE, 60.0, Decimal("10.0"), _CONFIG
    )


def test_start_conditions_not_met_above_10_pct() -> None:
    assert not meets_near_buy_start_conditions(
        BuyAction.WATCH_FOR_PRICE, 60.0, Decimal("10.1"), _CONFIG
    )


def test_start_conditions_not_met_below_quality_threshold() -> None:
    assert not meets_near_buy_start_conditions(
        BuyAction.WATCH_FOR_PRICE, 59.9, Decimal("5.0"), _CONFIG
    )


def test_start_conditions_not_met_when_not_watch_for_price() -> None:
    assert not meets_near_buy_start_conditions(
        BuyAction.BUY, 80.0, Decimal("5.0"), _CONFIG
    )


def test_start_conditions_not_met_without_entry_price() -> None:
    assert not meets_near_buy_start_conditions(
        BuyAction.WATCH_FOR_PRICE, 80.0, None, _CONFIG
    )


def test_continue_conditions_hold_within_12_pct() -> None:
    assert meets_near_buy_continue_conditions(
        BuyAction.WATCH_FOR_PRICE, Decimal("12.0"), _CONFIG
    )


def test_continue_conditions_end_above_12_pct() -> None:
    assert not meets_near_buy_continue_conditions(
        BuyAction.WATCH_FOR_PRICE, Decimal("12.1"), _CONFIG
    )


def test_continue_conditions_end_when_promoted_or_excluded() -> None:
    assert not meets_near_buy_continue_conditions(BuyAction.BUY, Decimal("2.0"), _CONFIG)
    assert not meets_near_buy_continue_conditions(
        BuyAction.NOT_ATTRACTIVE, Decimal("2.0"), _CONFIG
    )


def test_consecutive_business_days_increments_on_truly_consecutive_day() -> None:
    assert compute_consecutive_business_days(1, 2) == 3


def test_consecutive_business_days_resets_to_one_on_gap() -> None:
    assert compute_consecutive_business_days(2, 3) == 1
    assert compute_consecutive_business_days(5, 10) == 1


def test_best_distance_pct_keeps_smallest_observed() -> None:
    assert compute_best_distance_pct(Decimal("8.0"), Decimal("6.0")) == Decimal("6.0")
    assert compute_best_distance_pct(Decimal("6.0"), Decimal("8.0")) == Decimal("6.0")
    assert compute_best_distance_pct(None, Decimal("9.0")) == Decimal("9.0")


def test_evaluate_stale_true_past_threshold() -> None:
    assert evaluate_stale(6, 5)
    assert not evaluate_stale(5, 5)
    assert not evaluate_stale(1, 5)
