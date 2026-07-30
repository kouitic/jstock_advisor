from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.valuation.buy_price_levels import compute_buy_price_levels
from jstock_advisor.domain.valuation.margin_of_safety import compute_margin_of_safety
from jstock_advisor.domain.valuation.valuation_confidence import determine_valuation_confidence

_CONFIG = load_config().buy_decision.margin_of_safety


def test_high_confidence_base_margins_no_adjustments() -> None:
    result = compute_margin_of_safety(ConfidenceLevel.HIGH, [], _CONFIG)
    assert result.allowed is True
    assert result.entry_margin == Decimal("0.10")
    assert result.standard_margin == Decimal("0.15")
    assert result.strong_margin == Decimal("0.20")
    assert result.adjustments == ()


def test_medium_confidence_is_more_conservative_than_high() -> None:
    high = compute_margin_of_safety(ConfidenceLevel.HIGH, [], _CONFIG)
    medium = compute_margin_of_safety(ConfidenceLevel.MEDIUM, [], _CONFIG)
    assert medium.entry_margin > high.entry_margin
    assert medium.standard_margin > high.standard_margin
    assert medium.strong_margin > high.strong_margin


def test_low_confidence_does_not_generate_automatic_price() -> None:
    result = compute_margin_of_safety(ConfidenceLevel.LOW, [], _CONFIG)
    assert result.allowed is False
    assert result.entry_margin is None


def test_adjustments_stack_and_are_recorded_with_reasons() -> None:
    result = compute_margin_of_safety(
        ConfidenceLevel.HIGH,
        ["industry_model_not_applied", "earnings_within_7_business_days"],
        _CONFIG,
    )
    assert result.entry_margin == Decimal("0.10") + Decimal("0.05") + Decimal("0.03")
    assert len(result.adjustments) == 2
    codes = {a.code for a in result.adjustments}
    assert codes == {"industry_model_not_applied", "earnings_within_7_business_days"}
    for adjustment in result.adjustments:
        assert adjustment.reason


def test_margin_capped_at_maximum() -> None:
    all_codes = list(_CONFIG.adjustments.model_dump().keys())
    result = compute_margin_of_safety(ConfidenceLevel.MEDIUM, all_codes, _CONFIG)
    assert result.entry_margin == Decimal("0.45")
    assert result.standard_margin == Decimal("0.45")
    assert result.strong_margin == Decimal("0.45")


def test_buy_price_levels_ordering_from_margins() -> None:
    margin_result = compute_margin_of_safety(ConfidenceLevel.HIGH, [], _CONFIG)
    levels = compute_buy_price_levels(Decimal("1000"), margin_result)
    assert levels.entry.price == Decimal("900")  # 1000 * (1-0.10)
    assert levels.standard.price == Decimal("850")  # 1000 * (1-0.15)
    assert levels.strong.price == Decimal("800")  # 1000 * (1-0.20)
    assert levels.entry.price >= levels.standard.price >= levels.strong.price


def test_buy_price_levels_none_when_valuation_anchor_none() -> None:
    margin_result = compute_margin_of_safety(ConfidenceLevel.HIGH, [], _CONFIG)
    levels = compute_buy_price_levels(None, margin_result)
    assert levels.entry is None
    assert levels.standard is None
    assert levels.strong is None


def test_buy_price_levels_none_when_confidence_low() -> None:
    margin_result = compute_margin_of_safety(ConfidenceLevel.LOW, [], _CONFIG)
    levels = compute_buy_price_levels(Decimal("1000"), margin_result)
    assert levels.entry is None


def test_valuation_confidence_high_when_no_negative_factors() -> None:
    result = determine_valuation_confidence(
        methods_used_count=3,
        dispersion_ratio=1.1,
        dispersion_medium_max=1.60,
        dispersion_auto_buy_block=2.00,
        industry_model_applied=True,
        uses_simplified_dcf=False,
        normalized_eps_confidence=None,
    )
    assert result.level == ConfidenceLevel.HIGH
    assert result.reasons_not_high == []


def test_valuation_confidence_medium_when_industry_model_not_applied() -> None:
    result = determine_valuation_confidence(
        methods_used_count=3,
        dispersion_ratio=1.1,
        dispersion_medium_max=1.60,
        dispersion_auto_buy_block=2.00,
        industry_model_applied=False,
        uses_simplified_dcf=False,
        normalized_eps_confidence=None,
    )
    assert result.level == ConfidenceLevel.MEDIUM
    assert "業種別適正価格モデル未適用" in result.reasons_not_high


def test_valuation_confidence_medium_when_dispersion_above_1_60() -> None:
    result = determine_valuation_confidence(
        methods_used_count=3,
        dispersion_ratio=1.7,
        dispersion_medium_max=1.60,
        dispersion_auto_buy_block=2.00,
        industry_model_applied=True,
        uses_simplified_dcf=False,
        normalized_eps_confidence=None,
    )
    assert result.level == ConfidenceLevel.MEDIUM


def test_valuation_confidence_low_when_dispersion_above_2_00() -> None:
    result = determine_valuation_confidence(
        methods_used_count=3,
        dispersion_ratio=2.5,
        dispersion_medium_max=1.60,
        dispersion_auto_buy_block=2.00,
        industry_model_applied=True,
        uses_simplified_dcf=False,
        normalized_eps_confidence=None,
    )
    assert result.level == ConfidenceLevel.LOW


def test_valuation_confidence_low_when_fewer_than_2_methods() -> None:
    result = determine_valuation_confidence(
        methods_used_count=1,
        dispersion_ratio=None,
        dispersion_medium_max=1.60,
        dispersion_auto_buy_block=2.00,
        industry_model_applied=True,
        uses_simplified_dcf=False,
        normalized_eps_confidence=None,
    )
    assert result.level == ConfidenceLevel.LOW
