"""総合利回り(配当利回り+株主優待利回り)の算出(要求仕様7節)。

総合利回り = 予想年間配当金 ÷ 現在株価 + 年間株主優待評価額 ÷ 優待取得に必要な投資金額
"""

from __future__ import annotations

from decimal import Decimal

from jstock_advisor.domain.entities.common import BenefitUtilityCoefficients
from jstock_advisor.domain.entities.enums import BenefitUtilityCategory
from jstock_advisor.interfaces.types import ShareholderBenefit

COEFFICIENT_FIELD_BY_CATEGORY: dict[BenefitUtilityCategory, str] = {
    BenefitUtilityCategory.CASH_EQUIVALENT: "cash_equivalent",
    BenefitUtilityCategory.VERSATILE_POINT: "versatile_point",
    BenefitUtilityCategory.IN_HOUSE_SERVICE: "in_house_service",
    BenefitUtilityCategory.IN_HOUSE_PRODUCT: "in_house_product",
    BenefitUtilityCategory.DISCOUNT_VOUCHER: "discount_voucher",
    BenefitUtilityCategory.LOTTERY_OR_COMMEMORATIVE: "lottery_or_commemorative",
}


def compute_dividend_yield_pct(
    forecast_annual_dividend_per_share: Decimal | None, current_price: Decimal
) -> float | None:
    if forecast_annual_dividend_per_share is None or current_price <= 0:
        return None
    return float(forecast_annual_dividend_per_share / current_price * 100)


def compute_annual_benefit_value(
    benefit: ShareholderBenefit | None,
    utility_coefficients: BenefitUtilityCoefficients,
    include_long_term_conditional: bool = False,
) -> Decimal | None:
    """最低取得株数(min_shares_required)を保有した場合の年間株主優待評価額。

    利用可能性を考慮した評価係数(要求仕様7節)を適用する。long_term_holding_condition_months
    が設定されている優待は、新規購入時点ではまだ条件を満たしていないとみなし、
    include_long_term_conditional=False(既定)の場合は含めない。
    """
    if benefit is None or not benefit.benefits:
        return None

    total = Decimal("0")
    for detail in benefit.benefits:
        if detail.min_shares_for_tier > benefit.min_shares_required:
            continue  # 最低取得株数では対象外のより上位ティア
        if detail.long_term_holding_condition_months and not include_long_term_conditional:
            continue
        if detail.estimated_value is None:
            continue
        field_name = COEFFICIENT_FIELD_BY_CATEGORY[detail.category]
        coefficient = getattr(utility_coefficients, field_name)
        total += detail.estimated_value * Decimal(str(coefficient))

    return total * benefit.frequency_per_year


def compute_benefit_yield_pct(
    annual_benefit_value: Decimal | None,
    min_shares_required: int,
    current_price: Decimal,
) -> float | None:
    if annual_benefit_value is None or min_shares_required <= 0 or current_price <= 0:
        return None
    investment_required = Decimal(min_shares_required) * current_price
    if investment_required <= 0:
        return None
    return float(annual_benefit_value / investment_required * 100)


def compute_total_yield_pct(
    dividend_yield_pct: float | None, benefit_yield_pct: float | None
) -> float:
    return (dividend_yield_pct or 0.0) + (benefit_yield_pct or 0.0)
