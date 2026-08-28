from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.entities.valuation import (
    FairValueMethodResult,
    FairValueUnusableReasonCode,
)
from jstock_advisor.domain.valuation.fair_value import compute_dcf_price
from jstock_advisor.domain.valuation.fair_value_usability import build_fair_value_range

_CONFIG = load_config().valuation


def test_dcf_price_computes_positive_value_for_healthy_cashflow() -> None:
    price = compute_dcf_price(
        operating_cashflow=Decimal("100000000"),
        capital_expenditure=Decimal("-20000000"),
        shares_outstanding=Decimal("1000000"),
        discount_rate_pct=8.0,
        terminal_growth_rate_pct=1.0,
        projection_years=5,
    )
    assert price is not None
    assert price > 0


def test_dcf_price_none_when_fcf_negative() -> None:
    price = compute_dcf_price(
        operating_cashflow=Decimal("10000000"),
        capital_expenditure=Decimal("-50000000"),  # 設備投資が営業CFを上回る
        shares_outstanding=Decimal("1000000"),
        discount_rate_pct=8.0,
        terminal_growth_rate_pct=1.0,
        projection_years=5,
    )
    assert price is None


def test_dcf_price_none_when_missing_inputs() -> None:
    assert (
        compute_dcf_price(
            operating_cashflow=None,
            capital_expenditure=Decimal("-1"),
            shares_outstanding=Decimal("100"),
            discount_rate_pct=8.0,
            terminal_growth_rate_pct=1.0,
            projection_years=5,
        )
        is None
    )


def _method(
    method: str, fair_value: Decimal | None, confidence: ConfidenceLevel = ConfidenceLevel.HIGH
) -> FairValueMethodResult:
    return FairValueMethodResult(
        method=method,
        fair_value=fair_value,
        confidence=confidence,
        exclusion_reason=None if fair_value is not None else "算出不能",
    )


def test_fair_value_range_usable_with_close_methods() -> None:
    results = [
        _method("target_yield", Decimal("1000")),
        _method("historical_range", Decimal("1100")),
    ]
    range_ = build_fair_value_range(
        results,
        _CONFIG.fair_value_methods.aggregation_method,
        _CONFIG.fair_value_methods.method_weights,
        _CONFIG.fair_value_usability,
    )
    assert range_.usable_for_trading_judgment is True
    assert range_.bear == Decimal("1000")
    assert range_.bull == Decimal("1100")
    # Issue #21: usable=True時はcode/reasonともNoneを保証する
    assert range_.unusable_reason is None
    assert range_.unusable_reason_code is None


def test_fair_value_range_unusable_when_methods_diverge_too_much() -> None:
    results = [
        _method("target_yield", Decimal("500")),
        _method("historical_range", Decimal("2000")),  # 4倍乖離
    ]
    range_ = build_fair_value_range(
        results,
        _CONFIG.fair_value_methods.aggregation_method,
        _CONFIG.fair_value_methods.method_weights,
        _CONFIG.fair_value_usability,
    )
    assert range_.usable_for_trading_judgment is False
    assert range_.unusable_reason is not None
    # Issue #21: 乖離過大は構造化codeでも区別できる
    assert range_.unusable_reason_code is FairValueUnusableReasonCode.METHOD_SPREAD_TOO_WIDE
    assert range_.overall_confidence == ConfidenceLevel.LOW


def test_fair_value_range_unusable_when_too_few_methods() -> None:
    results = [_method("target_yield", Decimal("1000"))]
    range_ = build_fair_value_range(
        results,
        _CONFIG.fair_value_methods.aggregation_method,
        _CONFIG.fair_value_methods.method_weights,
        _CONFIG.fair_value_usability,
    )
    assert range_.usable_for_trading_judgment is False
    # Issue #21: 手法不足は構造化codeでも区別できる
    assert range_.unusable_reason_code is FairValueUnusableReasonCode.TOO_FEW_METHODS
    assert range_.unusable_reason is not None


def test_fair_value_range_no_usable_methods() -> None:
    results = [_method("target_yield", None), _method("per", None)]
    range_ = build_fair_value_range(
        results,
        _CONFIG.fair_value_methods.aggregation_method,
        _CONFIG.fair_value_methods.method_weights,
        _CONFIG.fair_value_usability,
    )
    assert range_.bear is None
    assert range_.usable_for_trading_judgment is False
    # Issue #21: 有効手法0件は構造化codeでも区別できる
    assert range_.unusable_reason_code is FairValueUnusableReasonCode.NO_VALID_METHODS
    assert range_.unusable_reason is not None


def test_fair_value_range_low_confidence_method_caps_overall_confidence() -> None:
    results = [
        _method("target_yield", Decimal("1000"), confidence=ConfidenceLevel.HIGH),
        _method("dcf", Decimal("1050"), confidence=ConfidenceLevel.MEDIUM),
    ]
    range_ = build_fair_value_range(
        results,
        _CONFIG.fair_value_methods.aggregation_method,
        _CONFIG.fair_value_methods.method_weights,
        _CONFIG.fair_value_usability,
    )
    assert range_.usable_for_trading_judgment is True
    assert range_.overall_confidence == ConfidenceLevel.MEDIUM
