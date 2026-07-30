import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.common import BenefitUtilityCoefficients, DataSourceReference
from jstock_advisor.domain.entities.enums import BenefitUtilityCategory
from jstock_advisor.domain.valuation.shareholder_benefit_matching import (
    compute_annual_benefit_value_for_holding,
    compute_holding_duration_months,
    compute_next_record_date,
    select_effective_benefit_details,
)
from jstock_advisor.interfaces.types import BenefitDetail, ShareholderBenefit

_SOURCE = DataSourceReference(provider="test", fetched_at=dt.datetime.now(dt.UTC))


def _detail(
    *,
    shares: int,
    months: int | None = None,
    max_months: int | None = None,
    value: str = "1000",
    group: str | None = None,
    category: BenefitUtilityCategory = BenefitUtilityCategory.CASH_EQUIVALENT,
) -> BenefitDetail:
    return BenefitDetail(
        category=category,
        description=f"tier {shares}/{months}",
        estimated_value=Decimal(value),
        min_shares_for_tier=shares,
        long_term_holding_condition_months=months,
        long_term_holding_condition_max_months=max_months,
        tier_group=group,
    )


class TestComputeHoldingDurationMonths:
    def test_exact_month_boundary(self) -> None:
        assert compute_holding_duration_months(dt.date(2024, 1, 31), dt.date(2024, 3, 1)) == 1

    def test_full_two_months(self) -> None:
        assert compute_holding_duration_months(dt.date(2024, 1, 31), dt.date(2024, 3, 31)) == 2

    def test_before_purchase_returns_zero(self) -> None:
        assert compute_holding_duration_months(dt.date(2024, 5, 1), dt.date(2024, 1, 1)) == 0

    def test_day_31_purchase_completes_on_shorter_months_own_last_day(self) -> None:
        # 4月は30日までしかないため、1/31購入は4/30時点で3ヶ月経過とみなす
        # (4/30を待たずに5/1になるまでカウントが遅れてはいけない)
        assert compute_holding_duration_months(dt.date(2024, 1, 31), dt.date(2024, 4, 30)) == 3
        assert compute_holding_duration_months(dt.date(2024, 1, 31), dt.date(2024, 5, 1)) == 3
        assert compute_holding_duration_months(dt.date(2024, 1, 31), dt.date(2024, 5, 31)) == 4

    def test_leap_day_purchase_reaches_full_year_on_non_leap_february_end(self) -> None:
        # 2/29購入は、翌年が閏年でなければ2/28時点で満12ヶ月とみなす
        assert compute_holding_duration_months(dt.date(2024, 2, 29), dt.date(2025, 2, 28)) == 12
        assert compute_holding_duration_months(dt.date(2024, 2, 29), dt.date(2025, 3, 1)) == 12
        assert compute_holding_duration_months(dt.date(2024, 2, 29), dt.date(2025, 3, 29)) == 13

    def test_same_day_returns_zero(self) -> None:
        assert compute_holding_duration_months(dt.date(2024, 5, 1), dt.date(2024, 5, 1)) == 0


class TestSelectEffectiveBenefitDetails:
    def test_matrix_picks_single_best_cell_per_group(self) -> None:
        # openwork型: 株数×保有期間のマトリクス。1000株・30ヶ月保有なら
        # 「1000株/24ヶ月」の枠のみが有効(100株枠や6ヶ月枠と重複加算しない)。
        details = [
            _detail(shares=100, months=6, value="500", group="digital_gift"),
            _detail(shares=100, months=24, value="1000", group="digital_gift"),
            _detail(shares=1000, months=6, value="5000", group="digital_gift"),
            _detail(shares=1000, months=24, value="10000", group="digital_gift"),
            _detail(shares=5000, months=6, value="25000", group="digital_gift"),
        ]
        effective = select_effective_benefit_details(
            details, shares_held=1000, holding_duration_months=30
        )
        assert len(effective) == 1
        assert effective[0].estimated_value == Decimal("10000")

    def test_insufficient_duration_falls_back_to_lower_tier(self) -> None:
        details = [
            _detail(shares=1000, months=6, value="5000", group="digital_gift"),
            _detail(shares=1000, months=24, value="10000", group="digital_gift"),
        ]
        effective = select_effective_benefit_details(
            details, shares_held=1000, holding_duration_months=10
        )
        assert len(effective) == 1
        assert effective[0].estimated_value == Decimal("5000")

    def test_below_minimum_shares_yields_no_group_match(self) -> None:
        details = [_detail(shares=1000, months=6, value="5000", group="digital_gift")]
        effective = select_effective_benefit_details(
            details, shares_held=500, holding_duration_months=100
        )
        assert effective == []

    def test_ungrouped_details_all_sum_independently(self) -> None:
        # haseko型: 同一株数条件で複数の独立した優待が同時に付与される。
        details = [
            _detail(shares=100, value="1", group=None),
            _detail(shares=100, value="2", group=None),
        ]
        effective = select_effective_benefit_details(
            details, shares_held=100, holding_duration_months=0
        )
        assert len(effective) == 2

    def test_upper_bound_excludes_holder_past_the_qualifying_window(self) -> None:
        # NTT型: 「2年以上3年未満」「5年以上6年未満」のように上限つきの期間区分。
        # 上限を超えた保有者(例: 4年保有)はどちらの区分にも該当しないはず。
        details = [
            _detail(shares=100, months=24, max_months=35, value="1500", group="dpoint_gift"),
            _detail(shares=100, months=60, max_months=71, value="3000", group="dpoint_gift"),
        ]
        effective = select_effective_benefit_details(
            details, shares_held=100, holding_duration_months=48
        )
        assert effective == []

    def test_upper_bound_still_matches_within_window(self) -> None:
        details = [
            _detail(shares=100, months=24, max_months=35, value="1500", group="dpoint_gift"),
            _detail(shares=100, months=60, max_months=71, value="3000", group="dpoint_gift"),
        ]
        effective = select_effective_benefit_details(
            details, shares_held=100, holding_duration_months=30
        )
        assert len(effective) == 1
        assert effective[0].estimated_value == Decimal("1500")


class TestComputeAnnualBenefitValueForHolding:
    def test_applies_coefficient_and_frequency_to_selected_tier(self) -> None:
        benefit = ShareholderBenefit(
            stock_code="5139",
            min_shares_required=100,
            benefits=[
                _detail(shares=100, months=6, value="500", group="digital_gift"),
                _detail(shares=1000, months=6, value="5000", group="digital_gift"),
            ],
            frequency_per_year=2,
            source=_SOURCE,
        )
        coeffs = BenefitUtilityCoefficients(cash_equivalent=1.0)
        value = compute_annual_benefit_value_for_holding(
            benefit, shares_held=1000, holding_duration_months=12, utility_coefficients=coeffs
        )
        # 5000 * 1.0(係数) * 2回/年 = 10000
        assert value == Decimal("10000")

    def test_none_when_no_benefit(self) -> None:
        coeffs = BenefitUtilityCoefficients()
        assert (
            compute_annual_benefit_value_for_holding(None, 100, 12, coeffs) is None
        )


class TestComputeNextRecordDate:
    def test_february_leap_year_month_end(self) -> None:
        assert compute_next_record_date([2], dt.date(2024, 1, 1)) == dt.date(2024, 2, 29)

    def test_february_non_leap_year_month_end(self) -> None:
        assert compute_next_record_date([2], dt.date(2026, 1, 1)) == dt.date(2026, 2, 28)

    def test_thirty_day_month_end(self) -> None:
        assert compute_next_record_date([4], dt.date(2026, 1, 1)) == dt.date(2026, 4, 30)

    def test_rolls_over_to_next_year_when_month_already_passed(self) -> None:
        assert compute_next_record_date([3], dt.date(2026, 7, 30)) == dt.date(2027, 3, 31)

    def test_picks_earliest_of_multiple_recurrence_months(self) -> None:
        assert compute_next_record_date([3, 9], dt.date(2026, 7, 30)) == dt.date(2026, 9, 30)

    def test_reference_date_equal_to_month_end_counts_as_upcoming(self) -> None:
        assert compute_next_record_date([3], dt.date(2026, 3, 31)) == dt.date(2026, 3, 31)

    def test_empty_recurrence_returns_none(self) -> None:
        assert compute_next_record_date([], dt.date(2026, 1, 1)) is None
