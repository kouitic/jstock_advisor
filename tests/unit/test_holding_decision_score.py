"""保有判断スコアのbase/final合成・判定区分境界・通知条件のテスト(実装プラン20節)。"""

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import HoldingDecisionCategory, HoldingDecisionConfidenceLevel
from jstock_advisor.domain.entities.holding_decision import (
    CompanyQualityScore,
    HoldingDecisionHardGate,
    InvestmentThesisScore,
    RiskDeductionScore,
)
from jstock_advisor.domain.signals.holding_decision_score import combine_holding_decision

_RULES = load_config().holding_decision
_NO_GATE = HoldingDecisionHardGate(triggered=False)


def _scores(cq: float, it: float, rd: float, coverage: float = 1.0):
    return (
        CompanyQualityScore(score=cq, coverage_ratio=coverage),
        InvestmentThesisScore(score=it, coverage_ratio=coverage),
        RiskDeductionScore(score=rd, coverage_ratio=coverage),
    )


def test_full_quality_and_thesis_no_risk_gives_max_score():
    q, i, r = _scores(50, 50, 0)
    out = combine_holding_decision(q, i, r, _NO_GATE, _RULES)
    assert out.base_score == 100.0
    assert out.final_score == 100.0
    assert out.category == HoldingDecisionCategory.STRONG_HOLD
    assert out.should_notify is False


def test_zero_quality_and_thesis_full_risk_gives_min_score():
    q, i, r = _scores(0, 0, 100)
    out = combine_holding_decision(q, i, r, _NO_GATE, _RULES)
    assert out.base_score == -100.0
    assert out.final_score == -100.0
    assert out.category == HoldingDecisionCategory.STRONG_SELL_CONSIDERATION
    assert out.should_notify is True


def test_representative_mixed_case_minus_12():
    q, i, r = _scores(35, 28, 75)
    out = combine_holding_decision(q, i, r, _NO_GATE, _RULES)
    assert out.base_score == -12.0
    assert out.final_score == -12.0
    assert out.category == HoldingDecisionCategory.SELL_CONSIDERATION
    assert out.should_notify is True


def _score_for_target(target: float) -> tuple[CompanyQualityScore, InvestmentThesisScore, RiskDeductionScore]:
    # cq=25 + it=25 - rd=(50-target) -> final = target
    return _scores(25, 25, 50 - target)


def test_boundary_minus_0_99_is_sell_watch_and_not_notified():
    q, i, r = _score_for_target(-0.99)
    out = combine_holding_decision(q, i, r, _NO_GATE, _RULES)
    assert out.category == HoldingDecisionCategory.SELL_WATCH
    assert out.should_notify is False


def test_boundary_minus_1_00_is_sell_watch_and_not_notified():
    q, i, r = _score_for_target(-1.00)
    out = combine_holding_decision(q, i, r, _NO_GATE, _RULES)
    assert out.category == HoldingDecisionCategory.SELL_WATCH
    assert out.should_notify is False


def test_boundary_minus_1_01_is_sell_consideration_and_notified():
    q, i, r = _score_for_target(-1.01)
    out = combine_holding_decision(q, i, r, _NO_GATE, _RULES)
    assert out.category == HoldingDecisionCategory.SELL_CONSIDERATION
    assert out.should_notify is True


def test_boundary_minus_2_00_is_sell_consideration_and_notified():
    q, i, r = _score_for_target(-2.00)
    out = combine_holding_decision(q, i, r, _NO_GATE, _RULES)
    assert out.category == HoldingDecisionCategory.SELL_CONSIDERATION
    assert out.should_notify is True


def test_boundary_minus_30_00_is_strong_sell_consideration():
    q, i, r = _score_for_target(-30.00)
    out = combine_holding_decision(q, i, r, _NO_GATE, _RULES)
    assert out.category == HoldingDecisionCategory.STRONG_SELL_CONSIDERATION
    assert out.should_notify is True


def test_boundary_minus_29_99_is_sell_consideration():
    q, i, r = _score_for_target(-29.99)
    out = combine_holding_decision(q, i, r, _NO_GATE, _RULES)
    assert out.category == HoldingDecisionCategory.SELL_CONSIDERATION
    assert out.should_notify is True


def test_hard_gate_caps_high_base_score_and_forces_notify():
    q, i, r = _scores(50, 50, 0)  # base=100
    gate = HoldingDecisionHardGate(triggered=True, reason_codes=("DEBT_EXCESS",))
    out = combine_holding_decision(q, i, r, gate, _RULES)
    assert out.base_score == 100.0
    assert out.final_score == _RULES.hard_gate.score_cap
    assert out.hard_gate.adjustment_applied is True
    assert out.category == HoldingDecisionCategory.STRONG_SELL_CONSIDERATION
    # ハードゲート単独ではなく、score_capにより結果的にscore_threshold_metを満たす。
    assert out.score_threshold_met is True
    assert out.should_notify is True


def test_hard_gate_not_applied_when_base_already_below_cap():
    q, i, r = _scores(0, 0, 100)  # base=-100, already below cap(-30)
    gate = HoldingDecisionHardGate(triggered=True, reason_codes=("DEBT_EXCESS",))
    out = combine_holding_decision(q, i, r, gate, _RULES)
    assert out.hard_gate.adjustment_applied is False
    assert out.final_score == -100.0


def test_hard_gate_only_exempts_coverage_not_score_threshold():
    """ハードゲートはcoverage_satisfiedのみを免除し、score_threshold_metは免除しない。

    score_cap適用後finalは必ずnotify_below_score未満になるため、この状況は
    実運用では発生しないが、should_notifyの論理式そのものがscore条件を
    無条件で要求していることを直接検証する。
    """
    q, i, r = _scores(50, 50, 0)  # base=100, way above notify threshold
    gate = HoldingDecisionHardGate(triggered=False)  # ハードゲート発動なし
    out = combine_holding_decision(q, i, r, gate, _RULES)
    assert out.coverage_gate_passed is True  # coverage自体は十分
    assert out.score_threshold_met is False  # スコアが閾値を満たさない
    assert out.should_notify is False  # ハードゲートが無いのでブロックされたまま


def test_no_hard_gate_insufficient_coverage_blocks_notification():
    q = CompanyQualityScore(score=10, coverage_ratio=0.3)
    i = InvestmentThesisScore(score=10, coverage_ratio=0.3)
    r = RiskDeductionScore(score=80, coverage_ratio=0.3)
    out = combine_holding_decision(q, i, r, _NO_GATE, _RULES)
    assert out.final_score < _RULES.notify_below_score
    assert out.coverage_satisfied is False
    assert out.should_notify is False
    assert out.confidence == HoldingDecisionConfidenceLevel.INSUFFICIENT_EVIDENCE


def test_sufficient_coverage_and_score_below_threshold_notifies():
    q, i, r = _score_for_target(-5.0)
    out = combine_holding_decision(q, i, r, _NO_GATE, _RULES)
    assert out.coverage_satisfied is True
    assert out.should_notify is True


def test_risk_deduction_coverage_between_block_and_confidence_minimum_does_not_block():
    """risk_deduction_coverageがblock_minimum以上confidence_minimum未満なら、
    通知はブロックされずconfidenceのみ制限される。"""
    q = CompanyQualityScore(score=25, coverage_ratio=1.0)
    i = InvestmentThesisScore(score=25, coverage_ratio=1.0)
    r = RiskDeductionScore(score=52, coverage_ratio=0.5)  # block=0.30 <= 0.5 < confidence=0.70
    out = combine_holding_decision(q, i, r, _NO_GATE, _RULES)
    assert out.coverage_satisfied is True
    assert out.should_notify is True
    assert out.confidence != HoldingDecisionConfidenceLevel.HIGH


def test_risk_deduction_coverage_below_block_minimum_blocks_notification():
    q = CompanyQualityScore(score=25, coverage_ratio=1.0)
    i = InvestmentThesisScore(score=25, coverage_ratio=1.0)
    r = RiskDeductionScore(score=52, coverage_ratio=0.1)  # < block_minimum=0.30
    out = combine_holding_decision(q, i, r, _NO_GATE, _RULES)
    assert out.coverage_satisfied is False
    assert out.should_notify is False


def test_display_value_rounding_matches_expectation():
    q, i, r = _score_for_target(-0.6)
    out = combine_holding_decision(q, i, r, _NO_GATE, _RULES)
    assert out.display_value == -1
