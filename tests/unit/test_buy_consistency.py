from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import BuyAction, ConfidenceLevel
from jstock_advisor.domain.signals.buy_consistency import validate_buy_recommendation

_CONFIG = load_config().buy_decision

_BASE_KWARGS = dict(
    action=BuyAction.BUY,
    current_price=Decimal("900"),
    entry_price=Decimal("1000"),
    standard_price=Decimal("900"),
    strong_price=Decimal("800"),
    confidence=ConfidenceLevel.HIGH,
    business_days_to_earnings=30,
    valuation_dispersion_ratio=1.1,
    config=_CONFIG,
)


def test_no_violations_for_consistent_buy_recommendation() -> None:
    violations = validate_buy_recommendation(**_BASE_KWARGS)
    assert violations == []


def test_non_buy_action_skips_buy_specific_checks() -> None:
    kwargs = dict(_BASE_KWARGS, action=BuyAction.WATCH_FOR_PRICE, current_price=Decimal("5000"))
    violations = validate_buy_recommendation(**kwargs)
    assert violations == []


def test_price_ordering_violation_detected_even_for_non_buy_action() -> None:
    kwargs = dict(
        _BASE_KWARGS,
        action=BuyAction.WATCH_FOR_PRICE,
        entry_price=Decimal("800"),
        standard_price=Decimal("900"),
    )
    violations = validate_buy_recommendation(**kwargs)
    assert any(v.code == "PRICE_ORDER_VIOLATION_ENTRY_STANDARD" for v in violations)


def test_current_price_above_entry_with_buy_action_forces_violation() -> None:
    kwargs = dict(_BASE_KWARGS, current_price=Decimal("1100"))
    violations = validate_buy_recommendation(**kwargs)
    assert any(v.code == "CURRENT_PRICE_ABOVE_ENTRY_PRICE" for v in violations)


def test_low_confidence_with_buy_action_forces_violation() -> None:
    kwargs = dict(_BASE_KWARGS, confidence=ConfidenceLevel.LOW)
    violations = validate_buy_recommendation(**kwargs)
    assert any(v.code == "LOW_CONFIDENCE_BUY_ACTION" for v in violations)


def test_earnings_within_3_days_with_buy_action_forces_violation() -> None:
    kwargs = dict(_BASE_KWARGS, business_days_to_earnings=2)
    violations = validate_buy_recommendation(**kwargs)
    assert any(v.code == "EARNINGS_WINDOW_VIOLATION" for v in violations)


def test_dispersion_above_2_00_with_buy_action_forces_violation() -> None:
    kwargs = dict(_BASE_KWARGS, valuation_dispersion_ratio=2.5)
    violations = validate_buy_recommendation(**kwargs)
    assert any(v.code == "VALUATION_DISPERSION_TOO_HIGH" for v in violations)


def test_buy_action_without_price_levels_is_a_violation() -> None:
    kwargs = dict(_BASE_KWARGS, entry_price=None, standard_price=None, strong_price=None)
    violations = validate_buy_recommendation(**kwargs)
    assert any(v.code == "BUY_ACTION_WITHOUT_PRICE_LEVELS" for v in violations)
