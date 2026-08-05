"""ハードゲート判定のテスト(実装プラン7節)。一次情報確認済みの場合のみ発動する。"""

from jstock_advisor.domain.signals.holding_decision_hard_gate import (
    HardGateInputs,
    evaluate_hard_gate,
)


def test_no_conditions_does_not_trigger():
    gate = evaluate_hard_gate(HardGateInputs())
    assert gate.triggered is False
    assert gate.reason_codes == ()


def test_debt_excess_triggers_with_reason_code():
    gate = evaluate_hard_gate(HardGateInputs(debt_excess_confirmed=True))
    assert gate.triggered is True
    assert "DEBT_EXCESS" in gate.reason_codes


def test_accounting_fraud_triggers():
    gate = evaluate_hard_gate(HardGateInputs(accounting_fraud_confirmed=True))
    assert gate.triggered is True
    assert "ACCOUNTING_FRAUD" in gate.reason_codes


def test_listing_risk_triggers():
    gate = evaluate_hard_gate(HardGateInputs(delisting_or_kanri_confirmed=True))
    assert gate.triggered is True
    assert "DELISTING_OR_KANRI" in gate.reason_codes


def test_multiple_conditions_all_recorded():
    gate = evaluate_hard_gate(
        HardGateInputs(debt_excess_confirmed=True, going_concern_doubt_confirmed=True)
    )
    assert gate.triggered is True
    assert set(gate.reason_codes) == {"DEBT_EXCESS", "GOING_CONCERN_DOUBT"}


def test_investment_thesis_collapse_flag_maps_correctly():
    gate = evaluate_hard_gate(HardGateInputs(investment_thesis_collapse_confirmed=True))
    assert gate.triggered is True
    assert "INVESTMENT_THESIS_COLLAPSE" in gate.reason_codes
