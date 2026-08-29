"""総合利回り(配当利回り+株主優待利回り)の算出(要求仕様7節)。

総合利回り = 予想年間配当金 ÷ 現在株価 + 年間株主優待評価額 ÷ 優待取得に必要な投資金額

Issue #55 Phase B-1(欠測semanticsの分離):
「値が0である」ことと「値が不明である」ことを区別する。以前は
`(dividend_yield_pct or 0.0) + (benefit_yield_pct or 0.0)` により両者が同一の
`0.0` へ潰れていたため、下流(保有判断のcoverage gate・利確の利回り条件)が
「データ取得できなかった」を「利回りが0%だった」として断定していた。

優待については3状態を明確に区別する(混同してはならない):
  - 制度なし(NO_PROGRAM)     : 寄与0として確定。市場の大多数がこれに該当する
  - 評価可能(VALUED)         : 年間評価額が算出できた(0円確定を含む)
  - 評価不能(UNVALUABLE)     : 制度はあるが適用対象ティアの価値が不明 = unknown

配当の「信頼できる0」の扱い: `DividendInfo` 上に0が明示されている場合のみ0%として
扱い、値が無い場合は推測せずunknownとする。provider側で「真の無配 / 未公表 /
取得失敗」を区別する仕組みはIssue #59の責務であり、ここでは作らない。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

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


class BenefitProgramState(StrEnum):
    """株主優待の評価状態(Issue #55 Phase B-1)。

    「制度が無い」と「制度はあるが値付けできない」を同一視しないための3状態。
    前者は寄与0として確定できるが、後者はunknownであり総合利回りを断定できない。
    """

    NO_PROGRAM = "NO_PROGRAM"
    VALUED = "VALUED"
    UNVALUABLE = "UNVALUABLE"


@dataclass(frozen=True)
class BenefitValuation:
    """優待の年間評価額と、その評価状態。

    annual_value は state が VALUED のときのみ非None(0円確定を含む)。
    """

    state: BenefitProgramState
    annual_value: Decimal | None


def compute_annual_benefit_valuation(
    benefit: ShareholderBenefit | None,
    utility_coefficients: BenefitUtilityCoefficients,
    include_long_term_conditional: bool = False,
) -> BenefitValuation:
    """最低取得株数(min_shares_required)を保有した場合の年間株主優待評価額と評価状態。

    利用可能性を考慮した評価係数(要求仕様7節)を適用する。long_term_holding_condition_months
    が設定されている優待は、新規購入時点ではまだ条件を満たしていないとみなし、
    include_long_term_conditional=False(既定)の場合は含めない。

    Issue #55 Phase B-1: 「寄与しないことが確定している」ティアと「値付けできない」
    ティアを区別する。前者(最低取得株数では対象外・長期保有条件が未充足)は
    その保有条件では受け取れないことが確定しているため合計へ寄与しないだけだが、
    後者(適用対象なのに estimated_value が無い)は年間価値を算出できないため、
    合計を0円と断定してはならない。従来は後者も読み飛ばして `Decimal("0")` を返しており、
    「優待はあるが評価額不明」が「優待価値0円確定」と同一視されていた。

    適用対象ティアがすべて値付け済みであれば、合計が0でもVALUED(=0円確定)とする。
    """
    if benefit is None or not benefit.benefits:
        # 制度そのものが無い(未登録を含む)。市場の大多数がこれに該当し、
        # 「欠測」ではなく「寄与0」として扱う(screening_data_provider.pyの既存契約と同じ)。
        return BenefitValuation(state=BenefitProgramState.NO_PROGRAM, annual_value=None)

    total = Decimal("0")
    has_unvaluable_applicable_tier = False
    for detail in benefit.benefits:
        if detail.min_shares_for_tier > benefit.min_shares_required:
            continue  # 最低取得株数では対象外のより上位ティア(受け取れないことが確定)
        if detail.long_term_holding_condition_months and not include_long_term_conditional:
            continue  # 新規購入時点では未充足(この時点では受け取れないことが確定)
        if detail.estimated_value is None:
            # 適用対象なのに価値が不明。合計を確定できない。
            has_unvaluable_applicable_tier = True
            continue
        field_name = COEFFICIENT_FIELD_BY_CATEGORY[detail.category]
        coefficient = getattr(utility_coefficients, field_name)
        total += detail.estimated_value * Decimal(str(coefficient))

    if has_unvaluable_applicable_tier:
        return BenefitValuation(state=BenefitProgramState.UNVALUABLE, annual_value=None)
    return BenefitValuation(
        state=BenefitProgramState.VALUED, annual_value=total * benefit.frequency_per_year
    )


def compute_annual_benefit_value(
    benefit: ShareholderBenefit | None,
    utility_coefficients: BenefitUtilityCoefficients,
    include_long_term_conditional: bool = False,
) -> Decimal | None:
    """`compute_annual_benefit_valuation()`の年間評価額のみを返す薄いラッパー。

    Issue #55 Phase B-1以降、「制度あり・評価不能」は `Decimal("0")` ではなく
    `None` を返す。screening経路(`screening_data_provider.py`)は
    「優待はあるが利回りが算出できない場合のみ欠損として扱う」という契約を
    既にコメントで宣言しており、本変更でその宣言どおりに動作するようになる。
    """
    return compute_annual_benefit_valuation(
        benefit, utility_coefficients, include_long_term_conditional
    ).annual_value


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
    dividend_yield_pct: float | None,
    benefit_yield_pct: float | None,
    *,
    benefit_state: BenefitProgramState,
) -> float | None:
    """総合利回り。判定に使える値が確定しない場合は None(=unknown)を返す。

    Issue #55 Phase B-1で確定した契約(`None` と `0` を truthiness で判定しないこと):

    | 配当 | 優待 | 総合利回り | 状態 |
    |---|---|---|---|
    | A 既知 | 制度なし | 配当のみ | EVALUATED |
    | B 既知 | 評価可能 | 配当+優待 | EVALUATED |
    | C 既知 | 評価不能 | None | NOT_EVALUATED |
    | D 不明 | 制度なし | None | NOT_EVALUATED |
    | E 不明 | 評価可能 | None | NOT_EVALUATED |
    | F 不明 | 評価不能 | None | NOT_EVALUATED |
    | G 0(明示) | 制度なし | 0 | EVALUATED |
    | H 0(明示) | 評価可能 | 優待のみ | EVALUATED |

    G/H の「信頼できる0」は、`DividendInfo` 上に0が明示されている場合
    (= `compute_dividend_yield_pct` が `0.0` を返した場合)を指す。値が無い場合
    (`None`)から0を推測することはしない。provider側で「真の無配 / 未公表 /
    取得失敗」を区別する仕組みはIssue #59の責務であり、ここでは扱わない。

    総合利回りは加算量であるため、片方が不明なら「総合利回りが閾値未満」という
    下限方向の断定ができない。したがって C/E は既知の側の値だけで確定させない。
    """
    if dividend_yield_pct is None:
        return None  # D / E / F
    if benefit_state is BenefitProgramState.UNVALUABLE:
        return None  # C
    if benefit_state is BenefitProgramState.NO_PROGRAM:
        return dividend_yield_pct  # A / G(優待の寄与は0で確定)
    if benefit_yield_pct is None:
        # VALUED だが利回りへ変換できない(最低取得株数や株価が不正)。
        # 値付けはできたが利回りとしては確定しないため unknown 扱いとする。
        return None
    return dividend_yield_pct + benefit_yield_pct  # B / H
