"""投資前提悪化による売却判定(2026-07仕様: 判定エンジンの再設計)。

株価の下落そのものは判定材料に含めない。個別ルールを三値(TRIGGERED/
NOT_TRIGGERED/NOT_EVALUATED)で評価し、同一の財務変化に由来するルールは
独立根拠グループ(EvidenceGroup)で束ねたうえで、独立した根拠の件数と
「即時性のあるcritical」該当有無に基づいてSELL/URGENT_REVIEW候補を判定する。

旧仕様(major該当1件でSELL、critical該当1件でURGENT_REVIEW)は廃止した。
単一のルールだけで強い判定に到達することはない(§4)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from jstock_advisor.config.models import SellRulesConfig
from jstock_advisor.domain.classification.financial_industry import classify_industry
from jstock_advisor.domain.entities.common import PriceWithRationale
from jstock_advisor.domain.entities.enums import (
    DisclosureRiskConfirmationLevel,
    EvidenceGroup,
    IndustryClassification,
    PriceFieldBasis,
    RecommendationType,
    TriggerStatus,
)
from jstock_advisor.domain.financial_decomposition import is_fundamentally_driven
from jstock_advisor.domain.financial_series import FinancialPeriodValue
from jstock_advisor.interfaces.types import (
    CashflowDecomposition,
    DividendInfo,
    FinancialSummary,
    ShareholderBenefit,
)

_RULE_LABELS: dict[str, str] = {
    "dividend_cut": "減配(推測)",
    "dividend_omission": "無配転落(推測)",
    "unfavorable_dividend_policy_change": "配当方針の不利な変更",
    "large_earnings_guidance_downgrade": "業績予想の大幅下方修正",
    "continuous_operating_income_decline": "営業利益の継続悪化",
    "continuous_operating_cashflow_decline": "営業キャッシュフローの継続悪化",
    "interest_bearing_debt_surge": "有利子負債の急増",
    "financial_health_severe_deterioration": "財務健全性の重大な悪化(一般事業会社基準)",
    "balance_sheet_insolvency": "債務超過",
    "regulatory_capital_breach": "規制資本割れ(銀行専用指標、未実装)",
    "shareholder_benefit_abolished": "株主優待の廃止",
    "shareholder_benefit_major_downgrade": "株主優待の大幅改悪",
    "long_term_holding_condition_unfavorable_change": "長期保有条件の不利な変更",
    "major_scandal": "重大な不祥事",
    "accounting_problem": "会計問題",
    "listing_maintenance_risk": "上場維持リスク・継続企業前提の重要事象",
    "investment_premise_broken": "投資開始時の前提が崩れた",
}

_RULE_EVIDENCE_GROUP: dict[str, EvidenceGroup] = {
    "dividend_cut": EvidenceGroup.DIVIDEND,
    "dividend_omission": EvidenceGroup.DIVIDEND,
    "unfavorable_dividend_policy_change": EvidenceGroup.DIVIDEND,
    "large_earnings_guidance_downgrade": EvidenceGroup.EARNINGS,
    "continuous_operating_income_decline": EvidenceGroup.EARNINGS,
    "continuous_operating_cashflow_decline": EvidenceGroup.CASHFLOW,
    "interest_bearing_debt_surge": EvidenceGroup.BALANCE_SHEET,
    "financial_health_severe_deterioration": EvidenceGroup.BALANCE_SHEET,
    "balance_sheet_insolvency": EvidenceGroup.BALANCE_SHEET,
    "regulatory_capital_breach": EvidenceGroup.REGULATORY_CAPITAL,
    "shareholder_benefit_abolished": EvidenceGroup.SHAREHOLDER_BENEFIT,
    "shareholder_benefit_major_downgrade": EvidenceGroup.SHAREHOLDER_BENEFIT,
    "long_term_holding_condition_unfavorable_change": EvidenceGroup.SHAREHOLDER_BENEFIT,
    "major_scandal": EvidenceGroup.GOVERNANCE,
    "accounting_problem": EvidenceGroup.GOVERNANCE,
    "listing_maintenance_risk": EvidenceGroup.LISTING,
    "investment_premise_broken": EvidenceGroup.INVESTMENT_PREMISE,
}

# 「即時性のあるcritical」(要求仕様§4): 単に閾値を下回っただけの指標はここに含めない。
_IMMEDIATE_CRITICAL_RULES = frozenset(
    {
        "balance_sheet_insolvency",
        "regulatory_capital_breach",
        "listing_maintenance_risk",
        "major_scandal",
    }
)

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


# is_immediate_criticalの対象とするルール(major_scandal/listing_maintenance_risk)は、
# リスクキーワード一致だけでなく、決算訂正・監査意見への影響等の重大事象確認語が
# 別途検出された場合のみMATERIAL_EVENT_CONFIRMEDとする(2026-07仕様レビュー対応)。
_TWO_STAGE_CONFIRMATION_RULES = frozenset({"major_scandal", "listing_maintenance_risk"})


def classify_disclosure_risk_keywords_with_confirmation(
    found_keywords: list[str], material_event_keywords_found: list[str]
) -> dict[str, DisclosureRiskConfirmationLevel | None]:
    """開示リスクキーワードの検出結果を、重大性の二段階(RISK_KEYWORD_DETECTED/
    MATERIAL_EVENT_CONFIRMED)で分類する。該当なしはNone。
    """
    flags = classify_disclosure_risk_keywords(found_keywords)
    has_material_event = bool(material_event_keywords_found)
    result: dict[str, DisclosureRiskConfirmationLevel | None] = {}
    for rule_name, triggered in flags.items():
        if not triggered:
            result[rule_name] = None
        elif rule_name in _TWO_STAGE_CONFIRMATION_RULES and has_material_event:
            result[rule_name] = DisclosureRiskConfirmationLevel.MATERIAL_EVENT_CONFIRMED
        else:
            result[rule_name] = DisclosureRiskConfirmationLevel.RISK_KEYWORD_DETECTED
    return result


def detect_continuous_decline(values: list[Decimal], consecutive_quarters: int) -> bool:
    """直近consecutive_quarters期にわたり前期比で悪化し続けているかを判定する。"""
    if consecutive_quarters < 1 or len(values) < consecutive_quarters + 1:
        return False
    recent = values[-(consecutive_quarters + 1) :]
    return all(recent[i] < recent[i - 1] for i in range(1, len(recent)))


def detect_continuous_decline_period_aware(
    periods: list[FinancialPeriodValue], consecutive_quarters: int
) -> bool | None:
    """period_typeを明示したFinancialPeriodValue系列で継続悪化を判定する
    (2026-07仕様レビュー対応)。

    QUARTERとANNUALの比較・単独四半期と累計値の比較・比較期間種別不明の値による
    判定を禁止する。比較対象窓の中でperiod_typeが揃っていない場合や、件数が
    不足している場合はNone(判定不能)を返す(データ不足を理由に安全側の判定を
    弱めない、という既存方針とは別に、そもそも比較不能なものを比較しない)。
    """
    if consecutive_quarters < 1 or len(periods) < consecutive_quarters + 1:
        return None
    recent = periods[-(consecutive_quarters + 1) :]
    period_types = {p.period_type for p in recent}
    if len(period_types) != 1:
        return None
    if any(p.is_cumulative for p in recent):
        return None
    return all(recent[i].value < recent[i - 1].value for i in range(1, len(recent)))


@dataclass(frozen=True)
class SellRuleEvaluation:
    """売却ルール1件の評価結果(要求仕様§3)。

    データ不足による未評価(NOT_EVALUATED)は、Falseと同じ意味には扱わない。
    """

    rule_name: str
    status: TriggerStatus
    evidence_group: EvidenceGroup
    severity: str | None = None  # "critical" / "major" / "minor"、NOT_EVALUATEDならNone
    is_immediate_critical: bool = False
    metric_name: str | None = None
    current_value: str | None = None
    previous_value: str | None = None
    threshold: str | None = None
    comparison_period: str | None = None
    primary_source_confirmed: bool = False
    source: str | None = None
    explanation: str = ""

    @property
    def label(self) -> str:
        return _RULE_LABELS.get(self.rule_name, self.rule_name)


def _evaluate_financial_health_rules(
    financial: FinancialSummary, config: SellRulesConfig
) -> list[SellRuleEvaluation]:
    """balance_sheet_insolvency(全業種共通)とfinancial_health_severe_deterioration
    (一般事業会社限定)を評価する(要求仕様§2、レビュー対応で業種三値化・
    債務超過のsuspected/confirmed分離)。

    業種分類は三値(GENERAL_CORPORATE/FINANCIAL/UNKNOWN)。UNKNOWN(sector欠損等で
    業種を確認できない)をGENERAL_CORPORATEとして扱わない(一般事業会社向け
    自己資本比率ルールは、業種がGENERAL_CORPORATEと明確に判定できた場合にのみ適用)。
    金融業には同ルールを適用しない。銀行専用の健全性指標(CET1比率等)は現時点で
    取得できるProviderが無いため、regulatory_capital_breachは常にNOT_EVALUATEDとする。

    債務超過(balance_sheet_insolvency)は、yfinance等の二次情報による自己資本比率
    マイナスの検出のみではSUSPECTEDとし、SELL/URGENT_REVIEWの直接的な根拠(major/
    critical件数・独立根拠グループ数)には算入しない。決算短信・有価証券報告書等の
    一次情報で純資産合計が負であることを確認できた場合のみTRIGGERED(即時critical)
    とするが、現時点でそうした一次情報を取得するProviderは存在しないため、実運用上は
    常にSUSPECTED/NOT_TRIGGERED/NOT_EVALUATEDのいずれかとなる。
    """
    classification = classify_industry(financial.sector, financial.industry)
    industry = classification.classification
    results: list[SellRuleEvaluation] = []
    insolvency_severity = config.rules["balance_sheet_insolvency"].severity

    if financial.equity_ratio_pct is None:
        results.append(
            SellRuleEvaluation(
                rule_name="balance_sheet_insolvency",
                status=TriggerStatus.NOT_EVALUATED,
                evidence_group=EvidenceGroup.BALANCE_SHEET,
                is_immediate_critical=True,
                metric_name="equity_ratio_pct",
                source="yfinance",
                explanation="自己資本比率を取得できないため判定不能",
            )
        )
    else:
        insolvent_suspected = financial.equity_ratio_pct < 0
        results.append(
            SellRuleEvaluation(
                rule_name="balance_sheet_insolvency",
                status=(
                    TriggerStatus.SUSPECTED if insolvent_suspected else TriggerStatus.NOT_TRIGGERED
                ),
                evidence_group=EvidenceGroup.BALANCE_SHEET,
                severity=insolvency_severity if insolvent_suspected else None,
                is_immediate_critical=True,
                metric_name="equity_ratio_pct",
                current_value=f"{financial.equity_ratio_pct:.1f}%",
                threshold="0%",
                source="yfinance",
                primary_source_confirmed=False,
                explanation=(
                    "自己資本比率がマイナスであり債務超過の疑いがあるが、yfinance由来の"
                    "二次情報のみであり、一次情報での確認が取れるまでSELL/URGENT_REVIEW"
                    "の根拠にはしない(insolvency_suspected)"
                    if insolvent_suspected
                    else "自己資本比率はマイナスではない(債務超過ではない)"
                ),
            )
        )

    rule_cfg = config.rules["financial_health_severe_deterioration"]
    if industry != IndustryClassification.GENERAL_CORPORATE:
        if industry == IndustryClassification.FINANCIAL:
            category_label = (
                classification.financial_category.value
                if classification.financial_category is not None
                else "金融業"
            )
            reason = (
                f"業種({category_label})が金融業のため、一般事業会社向け自己資本比率"
                "ルールは適用しない。銀行・保険・証券専用の健全性指標は未実装のため、"
                "この観点での財務健全性は判定不能"
            )
        else:
            reason = (
                "sector/industryが取得できず業種を確認できないため、一般事業会社向け"
                "自己資本比率ルールを適用しない(業種不明を一般事業会社として扱わない)"
            )
        results.append(
            SellRuleEvaluation(
                rule_name="financial_health_severe_deterioration",
                status=TriggerStatus.NOT_EVALUATED,
                evidence_group=EvidenceGroup.BALANCE_SHEET,
                metric_name="equity_ratio_pct",
                source="yfinance",
                explanation=reason,
            )
        )
        if industry == IndustryClassification.FINANCIAL:
            results.append(
                SellRuleEvaluation(
                    rule_name="regulatory_capital_breach",
                    status=TriggerStatus.NOT_EVALUATED,
                    evidence_group=EvidenceGroup.REGULATORY_CAPITAL,
                    is_immediate_critical=True,
                    metric_name="cet1_ratio_pct/total_capital_ratio_pct",
                    source=None,
                    explanation="銀行専用の規制資本指標を取得できるデータソースが無いため判定不能",
                )
            )
    elif financial.equity_ratio_pct is None:
        results.append(
            SellRuleEvaluation(
                rule_name="financial_health_severe_deterioration",
                status=TriggerStatus.NOT_EVALUATED,
                evidence_group=EvidenceGroup.BALANCE_SHEET,
                metric_name="equity_ratio_pct",
                source="yfinance",
                explanation="自己資本比率を取得できないため判定不能",
            )
        )
    else:
        threshold = rule_cfg.equity_ratio_critical_pct or 15.0
        triggered = financial.equity_ratio_pct < threshold
        results.append(
            SellRuleEvaluation(
                rule_name="financial_health_severe_deterioration",
                status=TriggerStatus.TRIGGERED if triggered else TriggerStatus.NOT_TRIGGERED,
                evidence_group=EvidenceGroup.BALANCE_SHEET,
                severity=rule_cfg.severity if triggered else None,
                is_immediate_critical=False,
                metric_name="equity_ratio_pct",
                current_value=f"{financial.equity_ratio_pct:.1f}%",
                threshold=f"{threshold:.1f}%",
                source="yfinance",
                explanation=(
                    f"自己資本比率({financial.equity_ratio_pct:.1f}%)が閾値"
                    f"({threshold:.1f}%)を下回っている"
                    if triggered
                    else "自己資本比率は閾値を下回っていない"
                ),
            )
        )

    return results


def _evaluate_cashflow_decline_rule(
    quarterly_operating_cashflow_periods: list[FinancialPeriodValue],
    cashflow_decomposition: CashflowDecomposition | None,
    consecutive_quarters: int,
    severity: str,
) -> SellRuleEvaluation:
    """営業CF継続悪化ルール(要求仕様§14、レビュー対応で財務期間の構造化)。

    比較対象窓の中でperiod_type(QUARTER/YTD/TTM/ANNUAL)が揃っていない場合は
    そもそも継続悪化を判定できないためNOT_EVALUATEDとする。要因分解が無く
    「本業要因が主因かどうか」を確認できない場合も、悪化そのものは観測されていても
    majorとして扱わずNOT_EVALUATEDとする(データ不足を理由に強い判定を出さない)。
    要因分解の結果、明確に運転資本・一過性要因が主因だと分かっている場合は
    NOT_TRIGGERED(発火させない)。
    """
    declined = detect_continuous_decline_period_aware(
        quarterly_operating_cashflow_periods, consecutive_quarters
    )
    if declined is None:
        return SellRuleEvaluation(
            rule_name="continuous_operating_cashflow_decline",
            status=TriggerStatus.NOT_EVALUATED,
            evidence_group=EvidenceGroup.CASHFLOW,
            metric_name="operating_cashflow",
            source="yfinance",
            explanation=(
                "比較対象期間のperiod_typeが揃っていない、または期間データが不足しており、"
                "継続悪化を判定できない"
            ),
        )
    if not declined:
        return SellRuleEvaluation(
            rule_name="continuous_operating_cashflow_decline",
            status=TriggerStatus.NOT_TRIGGERED,
            evidence_group=EvidenceGroup.CASHFLOW,
            metric_name="operating_cashflow",
            source="yfinance",
            explanation="営業キャッシュフローの継続悪化は検出されなかった",
        )

    fundamentally_driven = is_fundamentally_driven(cashflow_decomposition)
    if fundamentally_driven is False:
        return SellRuleEvaluation(
            rule_name="continuous_operating_cashflow_decline",
            status=TriggerStatus.NOT_TRIGGERED,
            evidence_group=EvidenceGroup.CASHFLOW,
            metric_name="operating_cashflow",
            source="yfinance",
            explanation="営業CFの悪化は検出されたが、要因分解の結果、運転資本・一過性要因が主因",
        )
    if fundamentally_driven is None:
        return SellRuleEvaluation(
            rule_name="continuous_operating_cashflow_decline",
            status=TriggerStatus.NOT_EVALUATED,
            evidence_group=EvidenceGroup.CASHFLOW,
            metric_name="operating_cashflow",
            source="yfinance",
            explanation=(
                "営業CFの継続悪化は検出されたが、要因分解データが無く本業要因が主因かどうか"
                "確認できないため、強い判定の根拠にはしない"
            ),
        )
    return SellRuleEvaluation(
        rule_name="continuous_operating_cashflow_decline",
        status=TriggerStatus.TRIGGERED,
        evidence_group=EvidenceGroup.CASHFLOW,
        severity=severity,
        metric_name="operating_cashflow",
        source="yfinance",
        explanation="営業CFが継続悪化しており、要因分解の結果、本業要因が主因と確認できた",
    )


@dataclass(frozen=True)
class SellRuleTriggerInputs:
    """各ルールの三値評価結果の集合。

    データ不足・未実装(NOT_EVALUATED)はFalse(NOT_TRIGGERED)と区別する。
    構造化データから機械的に判定できないルール(配当方針の不利な変更、業績予想の
    大幅下方修正、有利子負債の急増、長期保有条件の不利な変更、投資前提が崩れたか)は、
    呼び出し側(サービス層)が実際に開示情報等を確認して`SellRuleOverride`を渡さない限り
    NOT_EVALUATEDとする(未確認をFalse扱いにしない)。
    """

    evaluations: dict[str, SellRuleEvaluation] = field(default_factory=dict)

    def triggered_rules(self) -> list[SellRuleEvaluation]:
        return [e for e in self.evaluations.values() if e.status == TriggerStatus.TRIGGERED]

    def as_dict(self) -> dict[str, bool]:
        """後方互換用(監査ログ等での簡易表示)。TRIGGERED=Trueのみを返す。"""
        return {name: e.status == TriggerStatus.TRIGGERED for name, e in self.evaluations.items()}


@dataclass(frozen=True)
class SellRuleOverride:
    """構造化データから機械的に判定できないルールについて、サービス層が実際に
    確認した結果を渡すための入力(要求仕様§3: 未確認はNOT_EVALUATEDのままにする)。
    """

    status: TriggerStatus
    explanation: str = ""
    primary_source_confirmed: bool = False
    source: str | None = None


def build_sell_rule_inputs_from_data(
    dividend: DividendInfo | None,
    financial: FinancialSummary,
    benefit: ShareholderBenefit | None,
    quarterly_operating_income_periods: list[FinancialPeriodValue],
    quarterly_operating_cashflow_periods: list[FinancialPeriodValue],
    disclosure_risk_keywords_found: list[str],
    config: SellRulesConfig,
    cashflow_decomposition: CashflowDecomposition | None = None,
    material_event_keywords_found: list[str] | None = None,
    interest_bearing_debt_surge: SellRuleOverride | None = None,
    unfavorable_dividend_policy_change: SellRuleOverride | None = None,
    large_earnings_guidance_downgrade: SellRuleOverride | None = None,
    long_term_holding_condition_unfavorable_change: SellRuleOverride | None = None,
    investment_premise_broken: SellRuleOverride | None = None,
) -> SellRuleTriggerInputs:
    """構造化データから機械的に判定可能なルールを自動評価し、それ以外は
    SellRuleOverrideが渡された場合のみ反映する(渡されない場合はNOT_EVALUATED)。
    """
    keyword_confirmation = classify_disclosure_risk_keywords_with_confirmation(
        disclosure_risk_keywords_found, material_event_keywords_found or []
    )
    industry = classify_industry(financial.sector, financial.industry).classification

    income_quarters = config.rules["continuous_operating_income_decline"].consecutive_quarters or 2
    cashflow_quarters = (
        config.rules["continuous_operating_cashflow_decline"].consecutive_quarters or 2
    )

    evaluations: dict[str, SellRuleEvaluation] = {}

    for e in _evaluate_financial_health_rules(financial, config):
        evaluations[e.rule_name] = e

    dividend_cut_triggered = bool(dividend and dividend.official_dividend_cut_announced)
    evaluations["dividend_cut"] = SellRuleEvaluation(
        rule_name="dividend_cut",
        status=TriggerStatus.TRIGGERED if dividend_cut_triggered else TriggerStatus.NOT_TRIGGERED,
        evidence_group=EvidenceGroup.DIVIDEND,
        severity=config.rules["dividend_cut"].severity if dividend_cut_triggered else None,
        metric_name="official_dividend_cut_announced",
        source="EDINET/TDnet" if dividend_cut_triggered else "yfinance",
        primary_source_confirmed=dividend_cut_triggered,
        explanation=(
            "一次情報で確認された正式な減配発表がある"
            if dividend_cut_triggered
            else (
                "一次情報で確認された正式な減配発表は無い"
                "(yfinance由来の年間配当合計比較のみでは減配と断定しない)"
            )
        ),
    )

    official_omission = bool(dividend and dividend.official_dividend_omission_announced)
    inferred_omission = bool(dividend and dividend.inferred_dividend_omission)
    omission_status = (
        TriggerStatus.TRIGGERED
        if official_omission
        else (TriggerStatus.SUSPECTED if inferred_omission else TriggerStatus.NOT_TRIGGERED)
    )
    evaluations["dividend_omission"] = SellRuleEvaluation(
        rule_name="dividend_omission",
        status=omission_status,
        evidence_group=EvidenceGroup.DIVIDEND,
        severity=config.rules["dividend_omission"].severity if official_omission else None,
        is_immediate_critical=False,
        metric_name="official_dividend_omission_announced",
        source="EDINET/TDnet" if official_omission else "yfinance",
        primary_source_confirmed=official_omission,
        explanation=(
            "一次情報で確認された正式な無配転落発表がある"
            if official_omission
            else (
                "yfinanceの予想配当率が0になっているが、一次情報での公式発表は未確認"
                "(SUSPECTED、SELL根拠の独立根拠数には含めない)"
                if inferred_omission
                else "無配転落は検出されていない"
            )
        ),
    )

    income_declined = detect_continuous_decline_period_aware(
        quarterly_operating_income_periods, income_quarters
    )
    evaluations["continuous_operating_income_decline"] = SellRuleEvaluation(
        rule_name="continuous_operating_income_decline",
        status=(
            TriggerStatus.NOT_EVALUATED
            if income_declined is None
            else (TriggerStatus.TRIGGERED if income_declined else TriggerStatus.NOT_TRIGGERED)
        ),
        evidence_group=EvidenceGroup.EARNINGS,
        severity=(
            config.rules["continuous_operating_income_decline"].severity
            if income_declined
            else None
        ),
        source="yfinance",
        explanation=(
            "比較対象期間のperiod_typeが揃っていない、または期間データが不足しており、"
            "継続悪化を判定できない"
            if income_declined is None
            else (
                f"営業利益が{income_quarters}期連続で悪化している"
                if income_declined
                else "営業利益の継続悪化は検出されなかった"
            )
        ),
    )

    evaluations["continuous_operating_cashflow_decline"] = _evaluate_cashflow_decline_rule(
        quarterly_operating_cashflow_periods,
        cashflow_decomposition,
        cashflow_quarters,
        config.rules["continuous_operating_cashflow_decline"].severity,
    )

    benefit_abolished = bool(benefit and benefit.is_abolished)
    evaluations["shareholder_benefit_abolished"] = SellRuleEvaluation(
        rule_name="shareholder_benefit_abolished",
        status=TriggerStatus.TRIGGERED if benefit_abolished else TriggerStatus.NOT_TRIGGERED,
        evidence_group=EvidenceGroup.SHAREHOLDER_BENEFIT,
        severity=(
            config.rules["shareholder_benefit_abolished"].severity if benefit_abolished else None
        ),
        source="manual_registry",
        primary_source_confirmed=True,
        explanation="株主優待の廃止が確認されている"
        if benefit_abolished
        else "株主優待は継続している",
    )

    benefit_downgraded = bool(benefit and benefit.is_major_downgrade)
    evaluations["shareholder_benefit_major_downgrade"] = SellRuleEvaluation(
        rule_name="shareholder_benefit_major_downgrade",
        status=TriggerStatus.TRIGGERED if benefit_downgraded else TriggerStatus.NOT_TRIGGERED,
        evidence_group=EvidenceGroup.SHAREHOLDER_BENEFIT,
        severity=(
            config.rules["shareholder_benefit_major_downgrade"].severity
            if benefit_downgraded
            else None
        ),
        source="manual_registry",
        primary_source_confirmed=True,
        explanation="株主優待の大幅改悪が確認されている"
        if benefit_downgraded
        else "株主優待の大幅改悪は無い",
    )

    for rule_name in ("major_scandal", "accounting_problem", "listing_maintenance_risk"):
        confirmation = keyword_confirmation[rule_name]
        triggered = confirmation is not None
        material_confirmed = (
            confirmation == DisclosureRiskConfirmationLevel.MATERIAL_EVENT_CONFIRMED
        )
        evaluations[rule_name] = SellRuleEvaluation(
            rule_name=rule_name,
            status=TriggerStatus.TRIGGERED if triggered else TriggerStatus.NOT_TRIGGERED,
            evidence_group=_RULE_EVIDENCE_GROUP[rule_name],
            severity=config.rules[rule_name].severity if triggered else None,
            is_immediate_critical=(rule_name in _IMMEDIATE_CRITICAL_RULES) and material_confirmed,
            source="EDINET/TDnet開示",
            primary_source_confirmed=True,
            explanation=(
                (
                    f"開示情報に重大事象の確認語を含めて検出された({_RULE_LABELS[rule_name]}、"
                    "MATERIAL_EVENT_CONFIRMED)"
                    if material_confirmed
                    else f"開示情報にリスクキーワードのみ検出された({_RULE_LABELS[rule_name]}、"
                    "RISK_KEYWORD_DETECTED。重大事象は未確認のためREVIEW止まりとする)"
                )
                if triggered
                else "該当する開示情報は検出されなかった"
            ),
        )

    override_rules: dict[str, SellRuleOverride | None] = {
        "interest_bearing_debt_surge": interest_bearing_debt_surge,
        "unfavorable_dividend_policy_change": unfavorable_dividend_policy_change,
        "large_earnings_guidance_downgrade": large_earnings_guidance_downgrade,
        "long_term_holding_condition_unfavorable_change": (
            long_term_holding_condition_unfavorable_change
        ),
        "investment_premise_broken": investment_premise_broken,
    }
    for rule_name, override in override_rules.items():
        if (
            rule_name == "interest_bearing_debt_surge"
            and industry != IndustryClassification.GENERAL_CORPORATE
        ):
            evaluations[rule_name] = SellRuleEvaluation(
                rule_name=rule_name,
                status=TriggerStatus.NOT_EVALUATED,
                evidence_group=_RULE_EVIDENCE_GROUP[rule_name],
                explanation=(
                    "業種がGENERAL_CORPORATEと明確に判定できないため、一般事業会社向けの"
                    "有利子負債比率ルールを適用しない"
                ),
            )
            continue
        if override is None:
            evaluations[rule_name] = SellRuleEvaluation(
                rule_name=rule_name,
                status=TriggerStatus.NOT_EVALUATED,
                evidence_group=_RULE_EVIDENCE_GROUP[rule_name],
                explanation="この観点は今回未確認(開示情報等の個別確認が必要)",
            )
        else:
            evaluations[rule_name] = SellRuleEvaluation(
                rule_name=rule_name,
                status=override.status,
                evidence_group=_RULE_EVIDENCE_GROUP[rule_name],
                severity=(
                    config.rules[rule_name].severity
                    if override.status == TriggerStatus.TRIGGERED
                    else None
                ),
                source=override.source,
                primary_source_confirmed=override.primary_source_confirmed,
                explanation=override.explanation,
            )

    for name, evaluation in list(evaluations.items()):
        rule_cfg = config.rules.get(name)
        if (
            rule_cfg is not None
            and not rule_cfg.enabled
            and evaluation.status != TriggerStatus.NOT_EVALUATED
        ):
            evaluations[name] = SellRuleEvaluation(
                rule_name=name,
                status=TriggerStatus.NOT_EVALUATED,
                evidence_group=evaluation.evidence_group,
                explanation="ルールがconfig上で無効化されている(enabled: false)",
            )

    return SellRuleTriggerInputs(evaluations=evaluations)


@dataclass(frozen=True)
class SellSignalResult:
    recommendation_type: RecommendationType
    triggered_rules: list[str]
    reasons: list[str]
    hold_reasons: list[str]
    evidence_details: list[SellRuleEvaluation]
    independent_evidence_group_count: int
    all_evidence_yfinance_only: bool
    immediate_execution_price: PriceWithRationale | None
    stop_review_price: PriceWithRationale | None


def _independent_evidence_groups(triggered: list[SellRuleEvaluation]) -> set[EvidenceGroup]:
    return {e.evidence_group for e in triggered}


def evaluate_sell_signal(
    inputs: SellRuleTriggerInputs,
    current_price: Decimal,
    config: SellRulesConfig,
) -> SellSignalResult:
    """独立根拠グループ数と即時性のあるcritical該当有無に基づき判定する(要求仕様§4)。

    - major/critical該当が0件、またはNOT_EVALUATEDのみ -> HOLD
    - major(非独立グループで数えて)1件のみ、または非即時のcritical1件のみ -> REVIEW
    - 独立major2件以上、またはcritical1件+独立major1件以上 -> SELL候補
    - 即時性のあるcritical1件のみ、または即時性critical1件+他の独立根拠 -> URGENT_REVIEW候補
    単一のルールだけで強い判定(SELL/URGENT_REVIEW)に到達することはない。
    """
    triggered = inputs.triggered_rules()
    reasons = [f"{e.label}({e.severity})" for e in triggered]
    triggered_names = [e.rule_name for e in triggered]

    major = [e for e in triggered if e.severity == "major"]
    critical = [e for e in triggered if e.severity == "critical"]
    immediate_critical = [e for e in critical if e.is_immediate_critical]
    # 即時性のあるcriticalは、一次情報で確認されたものに限りURGENT_REVIEWの根拠と
    # する(レビュー対応: 一次情報未確認の即時criticalではURGENTを許可しない)。
    immediate_critical_confirmed = [e for e in immediate_critical if e.primary_source_confirmed]
    # immediate_critical_confirmedが1件でもあれば下のelif以降には進まないため、
    # ここでのcriticalは実質的にnon-immediate(または未確認即時)criticalのみを指す。

    independent_groups = _independent_evidence_groups(triggered)
    independent_major_groups = _independent_evidence_groups(major)
    independent_critical_groups = _independent_evidence_groups(critical)
    all_yfinance_only = bool(triggered) and all(not e.primary_source_confirmed for e in triggered)

    if immediate_critical_confirmed:
        # 一次情報確認済みの即時性critical該当が1件でもあれば、それ単独でも
        # URGENT_REVIEW候補(他に独立根拠があっても結論は変わらない、要求仕様§4)。
        recommendation_type = RecommendationType.URGENT_REVIEW
    elif (
        len(independent_major_groups) >= 2
        or len(independent_critical_groups) >= 2
        or (critical and (independent_major_groups - independent_critical_groups))
    ):
        # 独立したmajor2件以上、独立したcritical(非即時)2件以上、
        # またはcritical1件+(criticalとは別の)独立したmajor1件以上 -> SELL候補
        recommendation_type = RecommendationType.SELL
    elif major or critical:
        # 単一のルール(または同一グループに重複するルール)のみでは、
        # major/criticalいずれもSELL/URGENT_REVIEWまで進めずREVIEWに留める。
        recommendation_type = RecommendationType.REVIEW
    else:
        recommendation_type = RecommendationType.HOLD

    if recommendation_type == RecommendationType.HOLD:
        return SellSignalResult(
            recommendation_type=RecommendationType.HOLD,
            triggered_rules=triggered_names,
            reasons=reasons,
            hold_reasons=["投資前提の悪化を示すルールに該当しない(株価の下落のみでは判定しない)"],
            evidence_details=list(inputs.evaluations.values()),
            independent_evidence_group_count=len(independent_groups),
            all_evidence_yfinance_only=all_yfinance_only,
            immediate_execution_price=None,
            stop_review_price=None,
        )

    immediate_execution_price = None
    if recommendation_type == RecommendationType.URGENT_REVIEW:
        immediate_execution_price = PriceWithRationale(
            price=current_price,
            rationale="即時性のある重大な悪化事象が検出されたため、速やかな検討が必要",
            basis=PriceFieldBasis.IMMEDIATE_EXECUTION_REFERENCE,
        )

    return SellSignalResult(
        recommendation_type=recommendation_type,
        triggered_rules=triggered_names,
        reasons=reasons,
        hold_reasons=[],
        evidence_details=list(inputs.evaluations.values()),
        independent_evidence_group_count=len(independent_groups),
        all_evidence_yfinance_only=all_yfinance_only,
        immediate_execution_price=immediate_execution_price,
        # 将来の再評価条件として提示できる具体的な価格水準を算出するロジックは
        # 未実装のため、現時点では常にNone(算出不能を現在値へフォールバックしない)。
        stop_review_price=None,
    )
