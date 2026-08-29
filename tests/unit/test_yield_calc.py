import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.common import BenefitUtilityCoefficients, DataSourceReference
from jstock_advisor.domain.entities.enums import BenefitUtilityCategory
from jstock_advisor.domain.valuation.yield_calc import (
    BenefitProgramState,
    compute_annual_benefit_valuation,
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


# --- Issue #55 Phase B-1: 総合利回りの欠測semantics ---------------------------
# 「値が0」と「値が不明」を区別する。Noneと0をtruthinessで判定しないこと。


def test_case_a_dividend_known_no_benefit_program_is_dividend_only() -> None:
    """A: 配当既知 + 優待制度なし → 配当のみ / EVALUATED。

    優待制度が無い銘柄は市場の大多数を占めるため、これを「欠測」として
    Noneにすると総合利回りがほぼ全銘柄で評価不能になってしまう。
    """
    assert (
        compute_total_yield_pct(3.0, None, benefit_state=BenefitProgramState.NO_PROGRAM)
        == 3.0
    )


def test_case_b_both_known_are_summed() -> None:
    assert (
        compute_total_yield_pct(3.0, 1.5, benefit_state=BenefitProgramState.VALUED) == 4.5
    )


def test_case_c_benefit_unvaluable_is_unknown() -> None:
    """C: 配当既知 + 優待制度あり評価不能 → None。

    総合利回りは加算量であり、片方が不明なら「閾値未満」の断定ができない。
    """
    assert (
        compute_total_yield_pct(3.0, None, benefit_state=BenefitProgramState.UNVALUABLE)
        is None
    )


def test_case_d_dividend_unknown_no_benefit_program_is_unknown() -> None:
    assert (
        compute_total_yield_pct(None, None, benefit_state=BenefitProgramState.NO_PROGRAM)
        is None
    )


def test_case_e_dividend_unknown_benefit_known_is_unknown() -> None:
    assert (
        compute_total_yield_pct(None, 1.5, benefit_state=BenefitProgramState.VALUED) is None
    )


def test_case_f_both_unknown_is_unknown() -> None:
    assert (
        compute_total_yield_pct(None, None, benefit_state=BenefitProgramState.UNVALUABLE)
        is None
    )


def test_case_g_explicit_zero_dividend_no_benefit_program_is_zero() -> None:
    """G: 信頼できる配当0(DividendInfo上に0が明示) + 制度なし → 0.0 / EVALUATED。

    0.0は「確定0%」であり、Noneの「不明」とは異なる。
    """
    result = compute_total_yield_pct(
        0.0, None, benefit_state=BenefitProgramState.NO_PROGRAM
    )
    assert result == 0.0
    assert result is not None


def test_case_h_explicit_zero_dividend_with_benefit_is_benefit_only() -> None:
    assert (
        compute_total_yield_pct(0.0, 1.5, benefit_state=BenefitProgramState.VALUED) == 1.5
    )


def test_valued_but_yield_not_computable_is_unknown() -> None:
    """評価額は出せたが利回りへ変換できない(株価・最低取得株数が不正)場合はunknown。"""
    assert (
        compute_total_yield_pct(3.0, None, benefit_state=BenefitProgramState.VALUED) is None
    )


# --- Issue #55 Phase B-1: 優待の3状態 -----------------------------------------


def _benefit_with(details: list[BenefitDetail]) -> ShareholderBenefit:
    return ShareholderBenefit(
        stock_code="9861",
        min_shares_required=100,
        benefits=details,
        frequency_per_year=1,
        source=_SOURCE,
    )


def test_benefit_valuation_no_program_when_absent() -> None:
    coeffs = BenefitUtilityCoefficients()
    result = compute_annual_benefit_valuation(None, coeffs)
    assert result.state is BenefitProgramState.NO_PROGRAM
    assert result.annual_value is None


def test_benefit_valuation_unvaluable_when_applicable_tier_has_no_estimated_value() -> None:
    """制度はあるが適用対象ティアの評価額が不明 → UNVALUABLE(0円確定にしない)。"""
    coeffs = BenefitUtilityCoefficients(in_house_service=1.0)
    benefit = _benefit_with(
        [
            BenefitDetail(
                category=BenefitUtilityCategory.IN_HOUSE_SERVICE,
                description="内容不明の優待",
                estimated_value=None,
                min_shares_for_tier=100,
            )
        ]
    )
    result = compute_annual_benefit_valuation(benefit, coeffs)
    assert result.state is BenefitProgramState.UNVALUABLE
    assert result.annual_value is None
    # 従来はDecimal("0")(=価値0円確定)を返していた
    assert compute_annual_benefit_value(benefit, coeffs) is None


def test_benefit_valuation_valued_zero_when_explicitly_zero() -> None:
    """評価額0円が明示されている場合はVALUEDかつ0円(不明ではない)。"""
    coeffs = BenefitUtilityCoefficients(in_house_service=1.0)
    benefit = _benefit_with(
        [
            BenefitDetail(
                category=BenefitUtilityCategory.IN_HOUSE_SERVICE,
                description="価値0円の優待",
                estimated_value=Decimal("0"),
                min_shares_for_tier=100,
            )
        ]
    )
    result = compute_annual_benefit_valuation(benefit, coeffs)
    assert result.state is BenefitProgramState.VALUED
    assert result.annual_value == Decimal("0")


def test_benefit_valuation_valued_zero_when_only_higher_tier_exists() -> None:
    """最低取得株数では対象外のティアしか無い場合は「受け取れないことが確定」= VALUED 0円。"""
    coeffs = BenefitUtilityCoefficients(in_house_service=1.0)
    benefit = _benefit_with(
        [
            BenefitDetail(
                category=BenefitUtilityCategory.IN_HOUSE_SERVICE,
                description="1000株以上の優待",
                estimated_value=Decimal("5000"),
                min_shares_for_tier=1000,
            )
        ]
    )
    result = compute_annual_benefit_valuation(benefit, coeffs)
    assert result.state is BenefitProgramState.VALUED
    assert result.annual_value == Decimal("0")


def test_benefit_valuation_unvaluable_takes_precedence_over_valued_tier() -> None:
    """一部ティアが値付け不能なら、他が値付けできても合計は確定できない。"""
    coeffs = BenefitUtilityCoefficients(in_house_service=1.0)
    benefit = _benefit_with(
        [
            BenefitDetail(
                category=BenefitUtilityCategory.IN_HOUSE_SERVICE,
                description="値付け済み",
                estimated_value=Decimal("1000"),
                min_shares_for_tier=100,
            ),
            BenefitDetail(
                category=BenefitUtilityCategory.IN_HOUSE_SERVICE,
                description="値付け不能",
                estimated_value=None,
                min_shares_for_tier=100,
            ),
        ]
    )
    assert (
        compute_annual_benefit_valuation(benefit, coeffs).state
        is BenefitProgramState.UNVALUABLE
    )
