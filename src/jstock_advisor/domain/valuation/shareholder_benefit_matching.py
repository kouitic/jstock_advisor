"""実際の保有株数・保有期間に基づく株主優待の評価(2026-07仕様追加)。

`yield_calc.compute_annual_benefit_value`は「最低取得株数を新規購入した場合」の
評価額(買い候補スクリーニング向け)を算出するのに対し、本モジュールは
「今まさに保有している銘柄が、実際の保有株数・保有期間でどの優待段階に該当するか」
を算出する(保有銘柄向け)。

保有期間は`Holding.first_purchase_date`(holdingsテーブルの登録日)を起点に算出する。
月末日はカレンダー上の実際の月末(2月末・30日月・31日月)を用いる。
"""

from __future__ import annotations

import calendar
import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.common import BenefitUtilityCoefficients
from jstock_advisor.domain.valuation.yield_calc import COEFFICIENT_FIELD_BY_CATEGORY
from jstock_advisor.interfaces.types import BenefitDetail, ShareholderBenefit


def compute_holding_duration_months(first_purchase_date: dt.date, as_of: dt.date) -> int:
    """first_purchase_dateからas_ofまでの継続保有期間を、完了した月数で算出する。

    暦月単位で、first_purchase_dateの「日」を迎えるまでは1ヶ月とカウントしない
    (例: 1/31購入→3/1時点はまだ1ヶ月; 3/31時点で2ヶ月)。as_ofがfirst_purchase_date
    より前の場合は0を返す。
    """
    if as_of < first_purchase_date:
        return 0
    months = (as_of.year - first_purchase_date.year) * 12 + (
        as_of.month - first_purchase_date.month
    )
    if as_of.day < first_purchase_date.day:
        months -= 1
    return max(months, 0)


def select_effective_benefit_details(
    benefits: list[BenefitDetail], shares_held: int, holding_duration_months: int
) -> list[BenefitDetail]:
    """実際の保有株数・保有期間に対して有効な優待明細を選ぶ。

    tier_groupが設定された明細同士は、保有株数・保有期間の両方の条件を満たす
    ものの中から最も条件の良い(min_shares_for_tier, long_term_holding_condition_months
    がともに大きい)1件のみを採用する(段階制優待の重複加算防止)。
    tier_groupが未設定の明細は、条件を満たす限り従来通りすべて個別に採用する
    (複数の優待が同時に併存する銘柄向け)。
    """
    grouped: dict[str, list[BenefitDetail]] = {}
    ungrouped: list[BenefitDetail] = []
    for detail in benefits:
        if detail.tier_group:
            grouped.setdefault(detail.tier_group, []).append(detail)
        else:
            ungrouped.append(detail)

    effective: list[BenefitDetail] = []
    for detail in ungrouped:
        if _qualifies(detail, shares_held, holding_duration_months):
            effective.append(detail)

    for group_details in grouped.values():
        qualifying = [
            d for d in group_details if _qualifies(d, shares_held, holding_duration_months)
        ]
        if not qualifying:
            continue
        best = max(
            qualifying,
            key=lambda d: (d.min_shares_for_tier, d.long_term_holding_condition_months or 0),
        )
        effective.append(best)

    return effective


def _qualifies(detail: BenefitDetail, shares_held: int, holding_duration_months: int) -> bool:
    if detail.min_shares_for_tier > shares_held:
        return False
    return not (
        detail.long_term_holding_condition_months is not None
        and detail.long_term_holding_condition_months > holding_duration_months
    )


def compute_annual_benefit_value_for_holding(
    benefit: ShareholderBenefit | None,
    shares_held: int,
    holding_duration_months: int,
    utility_coefficients: BenefitUtilityCoefficients,
) -> Decimal | None:
    """実際に保有している株数・保有期間に基づく年間株主優待評価額。"""
    if benefit is None or not benefit.benefits:
        return None

    effective = select_effective_benefit_details(
        benefit.benefits, shares_held, holding_duration_months
    )
    total = Decimal("0")
    for detail in effective:
        if detail.estimated_value is None:
            continue
        field_name = COEFFICIENT_FIELD_BY_CATEGORY[detail.category]
        coefficient = getattr(utility_coefficients, field_name)
        total += detail.estimated_value * Decimal(str(coefficient))

    return total * benefit.frequency_per_year


def compute_next_record_date(
    recurrence_months: list[int], reference_date: dt.date
) -> dt.date | None:
    """毎年の権利確定月(例: [3, 9])から、reference_date以降で直近の権利確定日
    (各月の実際の月末日)を算出する。recurrence_monthsが空の場合はNoneを返す。
    """
    if not recurrence_months:
        return None

    candidates: list[dt.date] = []
    for month in recurrence_months:
        for year in (reference_date.year, reference_date.year + 1):
            last_day = calendar.monthrange(year, month)[1]
            candidate = dt.date(year, month, last_day)
            if candidate >= reference_date:
                candidates.append(candidate)
                break
    return min(candidates) if candidates else None
