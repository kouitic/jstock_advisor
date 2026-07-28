"""投資前提悪化による売却判定(要求仕様13節)。

株価の下落そのものは判定材料に含めない。個別ルールの検出結果を集計し、
sell_rules.yamlのseverityと判定閾値に基づいてSELL/URGENT_REVIEWを判定する。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.config.models import SellRulesConfig
from jstock_advisor.domain.entities.common import PriceWithRationale
from jstock_advisor.domain.entities.enums import PriceFieldBasis, RecommendationType
from jstock_advisor.domain.financial_decomposition import is_fundamentally_driven
from jstock_advisor.interfaces.types import (
    CashflowDecomposition,
    DividendInfo,
    FinancialSummary,
    ShareholderBenefit,
)

_RULE_LABELS: dict[str, str] = {
    "dividend_cut": "減配",
    "dividend_omission": "無配転落",
    "unfavorable_dividend_policy_change": "配当方針の不利な変更",
    "large_earnings_guidance_downgrade": "業績予想の大幅下方修正",
    "continuous_operating_income_decline": "営業利益の継続悪化",
    "continuous_operating_cashflow_decline": "営業キャッシュフローの継続悪化",
    "interest_bearing_debt_surge": "有利子負債の急増",
    "financial_health_severe_deterioration": "財務健全性の重大な悪化",
    "shareholder_benefit_abolished": "株主優待の廃止",
    "shareholder_benefit_major_downgrade": "株主優待の大幅改悪",
    "long_term_holding_condition_unfavorable_change": "長期保有条件の不利な変更",
    "major_scandal": "重大な不祥事",
    "accounting_problem": "会計問題",
    "listing_maintenance_risk": "上場維持リスク",
    "investment_premise_broken": "投資開始時の前提が崩れた",
}

_KEYWORD_RULE_MAP: dict[str, str] = {
    "特別調査委員会": "major_scandal",
    "第三者委員会": "major_scandal",
    "内部統制上の重要な不備": "accounting_problem",
    "不適切な会計処理": "accounting_problem",
    "上場廃止基準": "listing_maintenance_risk",
    "監理銘柄": "listing_maintenance_risk",
    "整理銘柄": "listing_maintenance_risk",
    "継続企業の前提に関する重要事象": "listing_maintenance_risk",
}


def classify_disclosure_risk_keywords(found_keywords: list[str]) -> dict[str, bool]:
    result = {
        "major_scandal": False,
        "accounting_problem": False,
        "listing_maintenance_risk": False,
    }
    for keyword in found_keywords:
        rule = _KEYWORD_RULE_MAP.get(keyword)
        if rule:
            result[rule] = True
    return result


def detect_continuous_decline(values: list[Decimal], consecutive_quarters: int) -> bool:
    """直近consecutive_quarters期にわたり前期比で悪化し続けているかを判定する。"""
    if consecutive_quarters < 1 or len(values) < consecutive_quarters + 1:
        return False
    recent = values[-(consecutive_quarters + 1) :]
    return all(recent[i] < recent[i - 1] for i in range(1, len(recent)))


def detect_financial_health_severe_deterioration(
    financial: FinancialSummary, equity_ratio_critical_pct: float
) -> bool:
    if financial.equity_ratio_pct is None:
        return False
    return financial.equity_ratio_pct < equity_ratio_critical_pct


@dataclass(frozen=True)
class SellRuleTriggerInputs:
    """各ルールの該当有無。構造化データから機械的に判定できないもの
    (配当方針の不利な変更、業績予想の大幅下方修正、有利子負債の急増、
    長期保有条件の不利な変更、投資前提が崩れたか)は呼び出し側(サービス層)が
    開示情報等をもとに判定して渡す。未評価・データ不足の場合はFalseとする
    (株価下落のみを理由に自動でTrueにはしない)。
    """

    dividend_cut: bool = False
    dividend_omission: bool = False
    unfavorable_dividend_policy_change: bool = False
    large_earnings_guidance_downgrade: bool = False
    continuous_operating_income_decline: bool = False
    continuous_operating_cashflow_decline: bool = False
    interest_bearing_debt_surge: bool = False
    financial_health_severe_deterioration: bool = False
    shareholder_benefit_abolished: bool = False
    shareholder_benefit_major_downgrade: bool = False
    long_term_holding_condition_unfavorable_change: bool = False
    major_scandal: bool = False
    accounting_problem: bool = False
    listing_maintenance_risk: bool = False
    investment_premise_broken: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "dividend_cut": self.dividend_cut,
            "dividend_omission": self.dividend_omission,
            "unfavorable_dividend_policy_change": self.unfavorable_dividend_policy_change,
            "large_earnings_guidance_downgrade": self.large_earnings_guidance_downgrade,
            "continuous_operating_income_decline": self.continuous_operating_income_decline,
            "continuous_operating_cashflow_decline": self.continuous_operating_cashflow_decline,
            "interest_bearing_debt_surge": self.interest_bearing_debt_surge,
            "financial_health_severe_deterioration": self.financial_health_severe_deterioration,
            "shareholder_benefit_abolished": self.shareholder_benefit_abolished,
            "shareholder_benefit_major_downgrade": self.shareholder_benefit_major_downgrade,
            "long_term_holding_condition_unfavorable_change": (
                self.long_term_holding_condition_unfavorable_change
            ),
            "major_scandal": self.major_scandal,
            "accounting_problem": self.accounting_problem,
            "listing_maintenance_risk": self.listing_maintenance_risk,
            "investment_premise_broken": self.investment_premise_broken,
        }


def build_sell_rule_inputs_from_data(
    dividend: DividendInfo | None,
    financial: FinancialSummary,
    benefit: ShareholderBenefit | None,
    quarterly_operating_incomes: list[Decimal],
    quarterly_operating_cashflows: list[Decimal],
    disclosure_risk_keywords_found: list[str],
    config: SellRulesConfig,
    interest_bearing_debt_surge: bool = False,
    unfavorable_dividend_policy_change: bool = False,
    large_earnings_guidance_downgrade: bool = False,
    long_term_holding_condition_unfavorable_change: bool = False,
    investment_premise_broken: bool = False,
    cashflow_decomposition: CashflowDecomposition | None = None,
) -> SellRuleTriggerInputs:
    """構造化データから機械的に判定可能なルールを自動評価し、それ以外は引数の値をそのまま使う。"""
    keyword_flags = classify_disclosure_risk_keywords(disclosure_risk_keywords_found)

    income_quarters = config.rules["continuous_operating_income_decline"].consecutive_quarters or 2
    cashflow_quarters = (
        config.rules["continuous_operating_cashflow_decline"].consecutive_quarters or 2
    )
    equity_critical_pct = (
        config.rules["financial_health_severe_deterioration"].equity_ratio_critical_pct or 15.0
    )

    cashflow_decline_detected = detect_continuous_decline(
        quarterly_operating_cashflows, cashflow_quarters
    )
    # 営業CFの継続悪化が検出されても、要因分解の結果が「運転資本・一過性要因が
    # 主因」(False)と明確に示している場合は、投資前提悪化ルールとして発火させない。
    # 分解データが無い(None)場合は、データ不足を理由に元のシグナルを弱めない
    # (要求仕様4節: 「運転資本や一過性支払いが主因の場合は…断定しない」の裏返しとして、
    # 主因不明な場合まで安全側を弱める必要は無い)。
    fundamentally_driven = is_fundamentally_driven(cashflow_decomposition)
    continuous_operating_cashflow_decline = cashflow_decline_detected and (
        fundamentally_driven is not False
    )

    return SellRuleTriggerInputs(
        dividend_cut=bool(dividend and dividend.is_dividend_cut_announced),
        dividend_omission=bool(dividend and dividend.is_dividend_omission_announced),
        unfavorable_dividend_policy_change=unfavorable_dividend_policy_change,
        large_earnings_guidance_downgrade=large_earnings_guidance_downgrade,
        continuous_operating_income_decline=detect_continuous_decline(
            quarterly_operating_incomes, income_quarters
        ),
        continuous_operating_cashflow_decline=continuous_operating_cashflow_decline,
        interest_bearing_debt_surge=interest_bearing_debt_surge,
        financial_health_severe_deterioration=detect_financial_health_severe_deterioration(
            financial, equity_critical_pct
        ),
        shareholder_benefit_abolished=bool(benefit and benefit.is_abolished),
        shareholder_benefit_major_downgrade=bool(benefit and benefit.is_major_downgrade),
        long_term_holding_condition_unfavorable_change=long_term_holding_condition_unfavorable_change,
        major_scandal=keyword_flags["major_scandal"],
        accounting_problem=keyword_flags["accounting_problem"],
        listing_maintenance_risk=keyword_flags["listing_maintenance_risk"],
        investment_premise_broken=investment_premise_broken,
    )


@dataclass(frozen=True)
class SellSignalResult:
    recommendation_type: RecommendationType
    triggered_rules: list[str]
    reasons: list[str]
    hold_reasons: list[str]
    stop_review_price: PriceWithRationale | None


def evaluate_sell_signal(
    inputs: SellRuleTriggerInputs,
    current_price: Decimal,
    config: SellRulesConfig,
) -> SellSignalResult:
    triggered = [name for name, value in inputs.as_dict().items() if value]

    major_count = 0
    critical_count = 0
    reasons: list[str] = []
    for name in triggered:
        rule = config.rules.get(name)
        if rule is None or not rule.enabled:
            continue
        label = _RULE_LABELS.get(name, name)
        reasons.append(f"{label}({rule.severity})")
        if rule.severity == "critical":
            critical_count += 1
        elif rule.severity == "major":
            major_count += 1

    j = config.judgment
    if (
        critical_count >= j.critical_to_urgent_review_min_count
        or major_count >= j.major_to_urgent_review_min_count
    ):
        recommendation_type = RecommendationType.URGENT_REVIEW
    elif major_count >= j.major_to_sell_min_count or critical_count >= 1:
        recommendation_type = RecommendationType.SELL
    else:
        recommendation_type = RecommendationType.HOLD

    if recommendation_type == RecommendationType.HOLD:
        return SellSignalResult(
            recommendation_type=RecommendationType.HOLD,
            triggered_rules=triggered,
            reasons=reasons,
            hold_reasons=["投資前提の悪化を示すルールに該当しない(株価の下落のみでは判定しない)"],
            stop_review_price=None,
        )

    target = PriceWithRationale(
        price=current_price,
        rationale="投資前提が悪化したため、現在株価付近での売却検討を目安とする",
        basis=PriceFieldBasis.IMMEDIATE_EXECUTION_REFERENCE,
    )
    return SellSignalResult(
        recommendation_type=recommendation_type,
        triggered_rules=triggered,
        reasons=reasons,
        hold_reasons=[],
        stop_review_price=target,
    )
