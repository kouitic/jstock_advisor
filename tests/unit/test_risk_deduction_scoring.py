"""リスク控除スコアの係数適用・カテゴリ上限・ハードゲート除外のテスト(実装プラン4節)。"""

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import EvidenceGroup, TriggerStatus
from jstock_advisor.domain.signals.risk_deduction_scoring import (
    RiskDeductionInputs,
    score_risk_deduction,
)
from jstock_advisor.domain.signals.sell_signal import SellRuleEvaluation, SellRuleTriggerInputs

_CFG = load_config().holding_decision_risk


def _inputs(**evaluations: SellRuleEvaluation) -> RiskDeductionInputs:
    return RiskDeductionInputs(sell_rule_inputs=SellRuleTriggerInputs(evaluations=evaluations))


def test_no_triggers_gives_zero_score():
    result = score_risk_deduction(_inputs(), _CFG)
    assert result.score == 0.0


def test_hard_gate_signal_is_excluded_from_risk_deduction():
    """balance_sheet_insolvency(ハードゲート該当)はリスク控除では計上されない
    (4節: ハードゲートとの三重評価防止)。"""
    inputs = _inputs(
        balance_sheet_insolvency=SellRuleEvaluation(
            rule_name="balance_sheet_insolvency",
            status=TriggerStatus.TRIGGERED,
            evidence_group=EvidenceGroup.BALANCE_SHEET,
            severity="critical",
            primary_source_confirmed=True,
        )
    )
    result = score_risk_deduction(inputs, _CFG)
    assert result.score == 0.0
    for category in result.categories:
        assert "balance_sheet_insolvency" not in category.signal_reason_codes


def test_non_hard_gate_signal_contributes_points():
    inputs = _inputs(
        dividend_cut=SellRuleEvaluation(
            rule_name="dividend_cut",
            status=TriggerStatus.TRIGGERED,
            evidence_group=EvidenceGroup.DIVIDEND,
            severity="major",
            primary_source_confirmed=True,
        )
    )
    result = score_risk_deduction(inputs, _CFG)
    assert result.score > 0.0
    shareholder_return = next(
        c for c in result.categories if c.category == "shareholder_return_deterioration"
    )
    assert "dividend_cut" in shareholder_return.signal_reason_codes


def test_continuous_signal_uses_two_period_persistence_factor():
    inputs = _inputs(
        continuous_operating_income_decline=SellRuleEvaluation(
            rule_name="continuous_operating_income_decline",
            status=TriggerStatus.TRIGGERED,
            evidence_group=EvidenceGroup.EARNINGS,
            severity="major",
            primary_source_confirmed=True,
        )
    )
    result = score_risk_deduction(inputs, _CFG)
    signal_cfg = _CFG.signals["continuous_operating_income_decline"]
    expected = (
        signal_cfg.base_points
        * 0.7  # major severity factor
        * _CFG.factors.persistence_two_periods
        * _CFG.factors.confidence_primary_source_confirmed
    )
    assert abs(result.score - expected) < 0.001


def test_secondary_source_only_uses_lower_confidence_factor():
    inputs = _inputs(
        dividend_cut=SellRuleEvaluation(
            rule_name="dividend_cut",
            status=TriggerStatus.TRIGGERED,
            evidence_group=EvidenceGroup.DIVIDEND,
            severity="major",
            primary_source_confirmed=False,
        )
    )
    result = score_risk_deduction(inputs, _CFG)
    signal_cfg = _CFG.signals["dividend_cut"]
    expected = (
        signal_cfg.base_points * 0.7 * _CFG.factors.persistence_single_occurrence
        * _CFG.factors.confidence_secondary_source_only
    )
    assert abs(result.score - expected) < 0.001


def test_category_total_is_capped_at_category_maximum():
    # 同一カテゴリ内の複数シグナルをすべてTRIGGEREDにしてcapを超えさせる
    inputs = _inputs(
        dividend_cut=SellRuleEvaluation(
            rule_name="dividend_cut",
            status=TriggerStatus.TRIGGERED,
            evidence_group=EvidenceGroup.DIVIDEND,
            severity="critical",
            primary_source_confirmed=True,
        ),
        dividend_omission=SellRuleEvaluation(
            rule_name="dividend_omission",
            status=TriggerStatus.TRIGGERED,
            evidence_group=EvidenceGroup.DIVIDEND,
            severity="critical",
            primary_source_confirmed=True,
        ),
        shareholder_benefit_abolished=SellRuleEvaluation(
            rule_name="shareholder_benefit_abolished",
            status=TriggerStatus.TRIGGERED,
            evidence_group=EvidenceGroup.SHAREHOLDER_BENEFIT,
            severity="major",
            primary_source_confirmed=True,
        ),
    )
    result = score_risk_deduction(inputs, _CFG)
    category = next(
        c for c in result.categories if c.category == "shareholder_return_deterioration"
    )
    assert category.points <= category.cap


def test_overall_score_never_exceeds_100():
    result = score_risk_deduction(_inputs(), _CFG)
    assert result.score <= 100.0
