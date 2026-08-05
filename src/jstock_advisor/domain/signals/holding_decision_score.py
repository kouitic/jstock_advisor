"""保有判断スコアのbase/final合成・判定区分・coverage/confidence・通知判定
(実装プラン1節・1.5節・5節・6節・7節・8節)。
"""

from __future__ import annotations

from dataclasses import dataclass

from jstock_advisor.config.models import HoldingDecisionRulesConfig
from jstock_advisor.domain.entities.enums import HoldingDecisionCategory, HoldingDecisionConfidenceLevel
from jstock_advisor.domain.entities.holding_decision import (
    ComponentCoverage,
    CompanyQualityScore,
    HoldingDecisionHardGate,
    InvestmentThesisScore,
    RiskDeductionScore,
)


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class HoldingDecisionOutcome:
    base_score: float
    final_score: float
    display_value: int
    hard_gate: HoldingDecisionHardGate
    category: HoldingDecisionCategory
    coverage: ComponentCoverage
    confidence: HoldingDecisionConfidenceLevel
    score_threshold_met: bool
    coverage_satisfied: bool
    coverage_gate_passed: bool
    should_notify: bool


def _category_for_score(
    score: float, t: "HoldingDecisionRulesConfig"
) -> HoldingDecisionCategory:
    thresholds = t.judgment_category_thresholds
    if score >= thresholds.strong_hold_min:
        return HoldingDecisionCategory.STRONG_HOLD
    if score >= thresholds.hold_min:
        return HoldingDecisionCategory.HOLD
    if score >= thresholds.caution_min:
        return HoldingDecisionCategory.CAUTION
    if score >= thresholds.partial_sell_consideration_min:
        return HoldingDecisionCategory.PARTIAL_SELL_CONSIDERATION
    if score >= thresholds.sell_watch_min:
        return HoldingDecisionCategory.SELL_WATCH
    if score > thresholds.sell_consideration_min:
        return HoldingDecisionCategory.SELL_CONSIDERATION
    return HoldingDecisionCategory.STRONG_SELL_CONSIDERATION


def _confidence_for_overall_coverage(
    overall_coverage: float, rules: HoldingDecisionRulesConfig
) -> HoldingDecisionConfidenceLevel:
    thresholds = rules.confidence_thresholds
    if overall_coverage < thresholds.low_minimum:
        return HoldingDecisionConfidenceLevel.INSUFFICIENT_EVIDENCE
    if overall_coverage < thresholds.medium_minimum:
        return HoldingDecisionConfidenceLevel.LOW
    if overall_coverage < thresholds.high_minimum:
        return HoldingDecisionConfidenceLevel.MEDIUM
    return HoldingDecisionConfidenceLevel.HIGH


def combine_holding_decision(
    company_quality: CompanyQualityScore,
    investment_thesis: InvestmentThesisScore,
    risk_deduction: RiskDeductionScore,
    hard_gate: HoldingDecisionHardGate,
    rules: HoldingDecisionRulesConfig,
) -> HoldingDecisionOutcome:
    base_score = _clip(
        company_quality.score + investment_thesis.score - risk_deduction.score, -100.0, 100.0
    )

    if hard_gate.triggered:
        score_cap = rules.hard_gate.score_cap
        final_score = min(base_score, score_cap)
        hard_gate = hard_gate.model_copy(
            update={"score_cap": score_cap, "adjustment_applied": base_score > score_cap}
        )
    else:
        final_score = base_score

    display_value = round(final_score)
    category = _category_for_score(final_score, rules)

    overall_coverage = (
        company_quality.coverage_ratio * 50.0
        + investment_thesis.coverage_ratio * 50.0
        + risk_deduction.coverage_ratio * 100.0
    ) / 200.0
    coverage = ComponentCoverage(
        overall=overall_coverage,
        company_quality=company_quality.coverage_ratio,
        investment_thesis=investment_thesis.coverage_ratio,
        risk_deduction=risk_deduction.coverage_ratio,
    )

    confidence = _confidence_for_overall_coverage(overall_coverage, rules)
    # risk_deductionのcoverageがconfidence_minimum未満(block_minimum以上)の場合、
    # 通知はブロックしないがconfidenceをMEDIUM以下へ制限する(5節)。
    if (
        confidence == HoldingDecisionConfidenceLevel.HIGH
        and coverage.risk_deduction < rules.coverage_thresholds.risk_deduction_confidence_minimum
    ):
        confidence = HoldingDecisionConfidenceLevel.MEDIUM

    coverage_satisfied = (
        coverage.overall >= rules.coverage_thresholds.overall_minimum
        and coverage.company_quality >= rules.coverage_thresholds.company_quality_minimum
        and coverage.investment_thesis >= rules.coverage_thresholds.investment_thesis_minimum
        and coverage.risk_deduction >= rules.coverage_thresholds.risk_deduction_block_minimum
    )
    confirmed_hard_gate = hard_gate.triggered
    coverage_gate_passed = coverage_satisfied or confirmed_hard_gate

    score_threshold_met = final_score < rules.notify_below_score
    should_notify = score_threshold_met and coverage_gate_passed

    return HoldingDecisionOutcome(
        base_score=base_score,
        final_score=final_score,
        display_value=display_value,
        hard_gate=hard_gate,
        category=category,
        coverage=coverage,
        confidence=confidence,
        score_threshold_met=score_threshold_met,
        coverage_satisfied=coverage_satisfied,
        coverage_gate_passed=coverage_gate_passed,
        should_notify=should_notify,
    )
