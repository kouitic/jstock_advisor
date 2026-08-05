"""リスク控除スコア(0-100点)の算出(実装プラン4節)。

既存sell_signal.pyのルール抽出部(build_sell_rule_inputs_from_data)の出力
(SellRuleTriggerInputs)を入力として再利用する(判定関数evaluate_sell_signal
自体は使わない)。ハードゲート該当シグナル(config側でhard_gate_excluded=true)
は対象から除外する(7節・4節: ハードゲートとの三重評価防止)。
"""

from __future__ import annotations

from dataclasses import dataclass

from jstock_advisor.config.models import HoldingDecisionRiskRulesConfig
from jstock_advisor.domain.entities.enums import EvidenceCoverageStatus, TriggerStatus
from jstock_advisor.domain.entities.holding_decision import (
    RiskDeductionCategoryDetail,
    RiskDeductionScore,
)
from jstock_advisor.domain.signals.sell_signal import SellRuleTriggerInputs

_SEVERITY_FACTOR = {"critical": 1.0, "major": 0.7, "minor": 0.4}
_CONTINUOUS_RULE_PREFIX = "continuous_"
_STRUCTURAL_RULE_NAMES = frozenset(
    {"investment_premise_broken", "long_term_holding_condition_unfavorable_change"}
)

_CATEGORY_NAMES = (
    "business_cashflow_deterioration",
    "shareholder_return_deterioration",
    "financial_crisis",
    "governance_and_listing_risk",
    "structural_change",
)


@dataclass(frozen=True)
class RiskDeductionInputs:
    sell_rule_inputs: SellRuleTriggerInputs


def _persistence_factor(rule_name: str, risk_config: HoldingDecisionRiskRulesConfig) -> float:
    if rule_name in _STRUCTURAL_RULE_NAMES:
        return risk_config.factors.persistence_structural
    if rule_name.startswith(_CONTINUOUS_RULE_PREFIX):
        return risk_config.factors.persistence_two_periods
    return risk_config.factors.persistence_single_occurrence


def _category_cap(category: str, risk_config: HoldingDecisionRiskRulesConfig) -> float:
    caps = risk_config.category_caps
    return {
        "business_cashflow_deterioration": caps.business_cashflow_deterioration,
        "shareholder_return_deterioration": caps.shareholder_return_deterioration,
        "financial_crisis": caps.financial_crisis,
        "governance_and_listing_risk": caps.governance_and_listing_risk,
        "structural_change": caps.structural_change,
    }[category]


def score_risk_deduction(
    inputs: RiskDeductionInputs,
    risk_config: HoldingDecisionRiskRulesConfig,
) -> RiskDeductionScore:
    category_totals: dict[str, float] = dict.fromkeys(_CATEGORY_NAMES, 0.0)
    category_signals: dict[str, list[str]] = {name: [] for name in _CATEGORY_NAMES}

    for rule_name, evaluation in inputs.sell_rule_inputs.evaluations.items():
        if evaluation.status != TriggerStatus.TRIGGERED:
            continue
        signal_config = risk_config.signals.get(rule_name)
        if signal_config is None or signal_config.hard_gate_excluded:
            # 未定義シグナル、またはハードゲート該当イベント(4節: リスク控除の
            # 対象から除外し、ハードゲート側のみで評価する)。
            continue

        severity_factor = _SEVERITY_FACTOR.get(evaluation.severity or "minor", 0.4)
        persistence_factor = _persistence_factor(rule_name, risk_config)
        confidence_factor = (
            risk_config.factors.confidence_primary_source_confirmed
            if evaluation.primary_source_confirmed
            else risk_config.factors.confidence_secondary_source_only
        )
        points = signal_config.base_points * severity_factor * persistence_factor * confidence_factor
        category_totals[signal_config.category] += points
        category_signals[signal_config.category].append(rule_name)

    category_details: list[RiskDeductionCategoryDetail] = []
    total = 0.0
    for category in _CATEGORY_NAMES:
        cap = _category_cap(category, risk_config)
        capped_points = min(category_totals[category], cap)
        category_details.append(
            RiskDeductionCategoryDetail(
                category=category,
                cap=cap,
                points=capped_points,
                status=EvidenceCoverageStatus.EVALUATED,
                signal_reason_codes=tuple(category_signals[category]),
            )
        )
        total += capped_points

    total = min(total, 100.0)

    # リスク控除カテゴリが丸ごとNOT_APPLICABLEになる状況は現状のデータソースでは
    # ほとんど発生しないため、全カテゴリ常にavailable(coverage_ratio=1.0)として扱う
    # (1.5節参照。企業品質の金融業除外ほど頻繁ではないという設計判断)。
    return RiskDeductionScore(score=total, coverage_ratio=1.0, categories=tuple(category_details))
