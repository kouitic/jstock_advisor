"""リスク控除スコア(0-100点)の算出(実装プラン4節)。

既存sell_signal.pyのルール抽出部(build_sell_rule_inputs_from_data)の出力
(SellRuleTriggerInputs)を入力として再利用する(判定関数evaluate_sell_signal
自体は使わない)。ハードゲート該当シグナル(config側でhard_gate_excluded=true)
は対象から除外する(7節・4節: ハードゲートとの三重評価防止)。

## coverage_ratio の算出(Issue #269)

企業品質(company_quality_scoring)・投資ストーリー維持と**同じ形**で算出する。

    available_weight = NOT_APPLICABLE **以外**のシグナルの base_points 合計
    evaluated_weight = **評価できた**シグナルの base_points 合計
    coverage_ratio   = evaluated_weight / available_weight

    ★ NOT_EVALUATED(データ不足で判定できなかった)は
      **分母に残り、分子から外れる** = 不足として計上する。

修正前は `coverage_ratio=1.0` を無条件に返しており、
「評価できなかった」という事実を「該当しない」と同じ扱いで捨てていた。
その結果、3軸を合成した overall coverage の下限が実効 0.5 相当となり、
★ **confidence が必ず MEDIUM 以上になって INSUFFICIENT/LOW が到達不能**だった。

★ 粒度は**シグナル単位**(重み = base_points)である。カテゴリ単位にすると
  「カテゴリ内に1本でも評価できれば満点」という粗い扱いになり、
  3本中1本欠けと5本中1本欠けを同じ 1.0 と数えてしまう(情報を捨てる方向)。

★ **governance_and_listing_risk は NOT_APPLICABLE** である。
  同カテゴリのシグナル(major_scandal / accounting_problem /
  listing_maintenance_risk)は**すべて hard_gate_excluded** であり、
  リスク控除の対象シグナルが**1本も無い**(= データ不足ではなく評価対象外)。
  分母へ入れると coverage が構造的に 0.80 で頭打ちになる。
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


#: 「評価できた」とみなす TriggerStatus(Issue #269)。
#: ★ SUSPECTED も **評価できた**側である。「一次情報が未確認の推測」であって
#:   「データが無くて判定できなかった」ではない(TriggerStatus の docstring 参照)。
#:   NOT_EVALUATED だけが「判定できなかった」を表す。
_EVALUATED_TRIGGER_STATUSES = frozenset(
    {TriggerStatus.TRIGGERED, TriggerStatus.NOT_TRIGGERED, TriggerStatus.SUSPECTED}
)


def _category_coverage(
    category: str,
    inputs: RiskDeductionInputs,
    risk_config: HoldingDecisionRiskRulesConfig,
) -> tuple[EvidenceCoverageStatus, float, float]:
    """カテゴリ1件分の (status, available_weight, evaluated_weight) を返す(Issue #269)。

    ★ 対象シグナルが1本も無いカテゴリは **NOT_APPLICABLE** であり、
      available へ加算しない(= 分母から外れる)。
      governance_and_listing_risk がこれに当たる(全シグナルが hard_gate_excluded)。

    ★ 評価結果が1本も得られなかったカテゴリは **NOT_EVALUATED** とする。
      available には残る(不足として計上される)。

    ★ 重みは base_points である(cap ではない)。cap はカテゴリ内の得点上限であり、
      「どれだけの根拠を見られたか」を表す量ではない。
    """
    available = 0.0
    evaluated = 0.0
    for rule_name, signal_config in risk_config.signals.items():
        if signal_config.category != category or signal_config.hard_gate_excluded:
            continue
        available += signal_config.base_points
        evaluation = inputs.sell_rule_inputs.evaluations.get(rule_name)
        # 評価そのものが存在しない場合も「判定できなかった」側へ倒す(fail-closed)。
        if evaluation is not None and evaluation.status in _EVALUATED_TRIGGER_STATUSES:
            evaluated += signal_config.base_points

    if available <= 0:
        return EvidenceCoverageStatus.NOT_APPLICABLE, 0.0, 0.0
    if evaluated <= 0:
        return EvidenceCoverageStatus.NOT_EVALUATED, available, 0.0
    return EvidenceCoverageStatus.EVALUATED, available, evaluated


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
        points = (
            signal_config.base_points * severity_factor * persistence_factor * confidence_factor
        )
        category_totals[signal_config.category] += points
        category_signals[signal_config.category].append(rule_name)

    category_details: list[RiskDeductionCategoryDetail] = []
    total = 0.0
    available_weight = 0.0
    evaluated_weight = 0.0
    for category in _CATEGORY_NAMES:
        cap = _category_cap(category, risk_config)
        capped_points = min(category_totals[category], cap)
        status, category_available, category_evaluated = _category_coverage(
            category, inputs, risk_config
        )
        available_weight += category_available
        evaluated_weight += category_evaluated
        category_details.append(
            RiskDeductionCategoryDetail(
                category=category,
                cap=cap,
                points=capped_points,
                status=status,
                signal_reason_codes=tuple(category_signals[category]),
            )
        )
        total += capped_points

    total = min(total, 100.0)

    # Issue #269: 「評価できなかった」を不足として計上する(修正前は 1.0 固定)。
    # available_weight が 0 になるのは「対象シグナルが1本も無い」場合であり、
    # 0除算を避けて 0.0 を返す(企業品質・投資ストーリー維持と同じ扱い)。
    coverage_ratio = (evaluated_weight / available_weight) if available_weight > 0 else 0.0
    return RiskDeductionScore(
        score=total, coverage_ratio=coverage_ratio, categories=tuple(category_details)
    )
