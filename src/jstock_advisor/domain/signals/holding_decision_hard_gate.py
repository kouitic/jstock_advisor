"""ハードゲート判定(実装プラン7節)。

一次情報で確認できた場合のみ発動する(SUSPECTED止まりでは発動しない)。
呼び出し側が各フラグへ渡す時点で一次情報確認済みであることを保証する責務を持つ。
"""

from __future__ import annotations

from dataclasses import dataclass

from jstock_advisor.domain.entities.holding_decision import HoldingDecisionHardGate


@dataclass(frozen=True)
class HardGateInputs:
    debt_excess_confirmed: bool = False
    going_concern_doubt_confirmed: bool = False
    bankruptcy_filing_confirmed: bool = False
    delisting_or_kanri_confirmed: bool = False
    accounting_fraud_confirmed: bool = False
    dividend_omission_and_cashflow_crisis_confirmed: bool = False
    # 投資ストーリーの根幹の完全消失。HUMAN_APPROVEDのInvestmentThesisが存在する
    # 場合のみ機械判定してよい(呼び出し側で保証する)。
    investment_thesis_collapse_confirmed: bool = False


_REASON_LABELS: dict[str, str] = {
    "DEBT_EXCESS": "debt_excess_confirmed",
    "GOING_CONCERN_DOUBT": "going_concern_doubt_confirmed",
    "BANKRUPTCY_FILING": "bankruptcy_filing_confirmed",
    "DELISTING_OR_KANRI": "delisting_or_kanri_confirmed",
    "ACCOUNTING_FRAUD": "accounting_fraud_confirmed",
    "DIVIDEND_OMISSION_AND_CASHFLOW_CRISIS": "dividend_omission_and_cashflow_crisis_confirmed",
    "INVESTMENT_THESIS_COLLAPSE": "investment_thesis_collapse_confirmed",
}


def evaluate_hard_gate(inputs: HardGateInputs) -> HoldingDecisionHardGate:
    """発動有無・理由コードのみを判定する(score_capの適用はholding_decision_score.py)。"""
    reason_codes = [
        code for code, field_name in _REASON_LABELS.items() if getattr(inputs, field_name)
    ]
    return HoldingDecisionHardGate(triggered=bool(reason_codes), reason_codes=tuple(reason_codes))
