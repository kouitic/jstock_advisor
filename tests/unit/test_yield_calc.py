import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.common import BenefitUtilityCoefficients, DataSourceReference
from jstock_advisor.domain.entities.enums import BenefitUtilityCategory
from jstock_advisor.domain.valuation.yield_calc import (
    compute_annual_benefit_value,
    compute_benefit_yield_pct,
    compute_dividend_yield_pct,
    compute_total_yield_pct,
)
from jstock_advisor.interfaces.types import BenefitDetail, ShareholderBenefit

_SOURCE = DataSourceReference(provider="test", fetched_at=dt.datetime.now(dt.UTC))


def test_compute_dividend_yield_pct() -> None:
    assert compute_dividend_yield_pct(Decimal("100"), Decimal("2000")) == 5.0


def test_compute_dividend_yield_pct_none_cases() -> None:
    assert compute_dividend_yield_pct(None, Decimal("2000")) is None
    assert compute_dividend_yield_pct(Decimal("100"), Decimal("0")) is None


def _benefit(*, long_term_months: int | None = None) -> ShareholderBenefit:
    return ShareholderBenefit(
        stock_code="9861",
        min_shares_required=100,
        benefits=[
            BenefitDetail(
                category=BenefitUtilityCategory.IN_HOUSE_SERVICE,
                description="優待食事券",
                estimated_value=Decimal("3000"),
                min_shares_for_tier=100,
                long_term_holding_condition_months=long_term_months,
            )
        ],
        frequency_per_year=2,
        source=_SOURCE,
    )


def test_compute_annual_benefit_value_applies_coefficient_and_frequency() -> None:
    coeffs = BenefitUtilityCoefficients(in_house_service=0.7)
    value = compute_annual_benefit_value(_benefit(), coeffs)
    # 3000 * 0.7(係数) * 2回/年 = 4200
    assert value == Decimal("4200")


def test_compute_annual_benefit_value_excludes_long_term_conditional_by_default() -> None:
    coeffs = BenefitUtilityCoefficients()
    value = compute_annual_benefit_value(_benefit(long_term_months=12), coeffs)
    assert value == Decimal("0")


def test_compute_annual_benefit_value_includes_long_term_when_requested() -> None:
    coeffs = BenefitUtilityCoefficients(in_house_service=1.0)
    value = compute_annual_benefit_value(
        _benefit(long_term_months=12), coeffs, include_long_term_conditional=True
    )
    assert value == Decimal("6000")


def test_compute_annual_benefit_value_none_when_no_benefits() -> None:
    coeffs = BenefitUtilityCoefficients()
    assert compute_annual_benefit_value(None, coeffs) is None


def test_compute_benefit_yield_pct() -> None:
    yield_pct = compute_benefit_yield_pct(Decimal("3000"), 100, Decimal("2000"))
    # 3000 / (100*2000) * 100 = 1.5%
    assert yield_pct == 1.5


def test_compute_total_yield_pct_handles_none() -> None:
    assert compute_total_yield_pct(3.0, None) == 3.0
    assert compute_total_yield_pct(None, 1.5) == 1.5
    assert compute_total_yield_pct(3.0, 1.5) == 4.5
