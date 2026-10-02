"""利確判定のaction gateが、実際に候補を抑制したかの因果追跡(Issue #720)。

`evaluate_profit_taking()`のgate(業種モデル・決算までの営業日数・一部売却の
実行可能性・強い反対材料)は、同じ入力が複数の判定経路へ同時に効くため、
「gateが閉じていた」ことと「そのgateのせいで最終判定が変わった」ことは別である
(他の経路が同水準以上を既にカバーしていれば、gateが閉じていても判定は変わらない)。

本moduleは、**遮断側にある入力を1つだけ通過側へ置換して同じ純粋関数で再評価する**
反実仮想により、その差を観測として残す。判定式・gate式・閾値は一切変更せず、
`evaluate_profit_taking()`を呼ぶだけである(本moduleはその関数のgate式を再実装
しない。通過側へ置換するだけで、どの経路にどう効くかは関数自身が決める)。

保持先は監査記録(AuditLog output_values)であり、Recommendationへは載せない
(純粋HOLDではRecommendationが生成されないため、「gateのせいでHOLDへ落ちた」を
残せない)。値はbool・enum名・codeのみで、金額・株数・銘柄名は運ばない。

effect:
  ACTION_CHANGED  置換するとfinal_actionが変わる(actually_suppressed=True)
  ORIGIN_ONLY     final_actionは同じだが、判定を押し上げた根拠(origin)が変わる
                  (他の経路が既に同水準以上のため抑制していない。
                  superseded_by=実際に採用されたorigin)
  JOINT_ONLY      単独では変わらないが、遮断中の全入力を同時に通すと変わる
                  (actually_suppressed=True)
  NONE            変わらない
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jstock_advisor.domain.signals.profit_taking import (
    ProfitTakingConditionInputs,
    ProfitTakingResult,
)

EFFECT_ACTION_CHANGED = "ACTION_CHANGED"
EFFECT_ORIGIN_ONLY = "ORIGIN_ONLY"
EFFECT_JOINT_ONLY = "JOINT_ONLY"
EFFECT_NONE = "NONE"

GATE_RESULT_BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class _BlockedInput:
    """遮断側にある入力1つ分と、それを通過側へ置換した入力。"""

    input_name: str
    gate_name: str
    families: tuple[int, ...]
    # 遮断側の値を通過側へ置換する更新(ProfitTakingConditionInputsのfield名 -> 通過側の値)
    updates: dict[str, Any]


def _blocked_inputs(
    inputs: ProfitTakingConditionInputs, min_earnings_business_days: int
) -> list[_BlockedInput]:
    """遮断側にある入力だけを列挙する(通過中の入力は置換不要)。

    family: 1=適正価格ベースの強い判定(_extra_action_gates_met)、
    2=上限価格(ceiling)の利用可否(_fair_value_action_usable)、
    3=PARTIAL候補を直接ゲートする経路(partial_sale_executable)。
    同じ入力が複数familyへ効く(例: 決算直前は1と2を同時に閉じる)ため、
    gate名ではなく入力を単位に記録する。
    """
    blocked: list[_BlockedInput] = []
    if not inputs.industry_model_applied:
        blocked.append(
            _BlockedInput(
                "industry_model_applied",
                "INDUSTRY_MODEL_NOT_APPLIED",
                (1, 2),
                {"industry_model_applied": True},
            )
        )
    days = inputs.days_to_next_earnings_business_days
    if days is None:
        blocked.append(
            _BlockedInput(
                "days_to_next_earnings_business_days",
                "EARNINGS_DAYS_UNKNOWN",
                (1,),
                {"days_to_next_earnings_business_days": min_earnings_business_days},
            )
        )
    elif days < min_earnings_business_days:
        blocked.append(
            _BlockedInput(
                "days_to_next_earnings_business_days",
                "EARNINGS_TOO_CLOSE",
                (1, 2),
                {"days_to_next_earnings_business_days": min_earnings_business_days},
            )
        )
    if not inputs.partial_sale_executable:
        blocked.append(
            _BlockedInput(
                "partial_sale_executable",
                "PARTIAL_SALE_NOT_EXECUTABLE",
                (1, 3),
                {"partial_sale_executable": True},
            )
        )
    if inputs.has_strong_counter_material:
        blocked.append(
            _BlockedInput(
                "has_strong_counter_material",
                "STRONG_COUNTER_MATERIAL_PRESENT",
                (1,),
                {"has_strong_counter_material": False},
            )
        )
    return blocked


def build_profit_taking_gate_trace(
    condition_inputs: ProfitTakingConditionInputs,
    evaluate: Callable[[ProfitTakingConditionInputs], ProfitTakingResult],
    baseline: ProfitTakingResult,
    min_earnings_business_days: int,
) -> list[dict[str, object]]:
    """遮断側の入力ごとに、反実仮想の再評価結果を構造化して返す(Issue #720)。

    `evaluate`は`evaluate_profit_taking()`をcondition_inputs以外を固定して
    呼ぶcallable、`baseline`は実際の入力での結果(呼び出し側が既に得ている)。
    例外は握りつぶさない(呼び出し側が`isolated_shadow_computation()`で隔離し、
    失敗を「算出できなかった」として残す)。
    """
    blocked = _blocked_inputs(condition_inputs, min_earnings_business_days)
    if not blocked:
        return []

    single_results = [evaluate(dataclasses.replace(condition_inputs, **b.updates)) for b in blocked]

    # 複数が同時に遮断中で、単独では変わらないものがある場合のみ、全入力を
    # 同時に通した結果を1回だけ計算する(JOINT_ONLYの判定用)。
    joint_changes_action = False
    needs_joint = len(blocked) >= 2 and any(
        r.final_action == baseline.final_action and r.origin == baseline.origin
        for r in single_results
    )
    if needs_joint:
        joint_updates: dict[str, Any] = {}
        for b in blocked:
            joint_updates.update(b.updates)
        joint_inputs = dataclasses.replace(condition_inputs, **joint_updates)
        joint_changes_action = evaluate(joint_inputs).final_action != baseline.final_action

    records: list[dict[str, object]] = []
    for b, single in zip(blocked, single_results, strict=True):
        if single.final_action != baseline.final_action:
            effect = EFFECT_ACTION_CHANGED
        elif single.origin != baseline.origin:
            effect = EFFECT_ORIGIN_ONLY
        elif joint_changes_action:
            effect = EFFECT_JOINT_ONLY
        else:
            effect = EFFECT_NONE
        records.append(
            {
                "input": b.input_name,
                "gate_name": b.gate_name,
                "gate_result": GATE_RESULT_BLOCKED,
                "families": list(b.families),
                "effect": effect,
                "actually_suppressed": effect in (EFFECT_ACTION_CHANGED, EFFECT_JOINT_ONLY),
                "baseline_action": baseline.final_action.value,
                "candidate_action": single.final_action.value,
                "superseded_by": baseline.origin if effect == EFFECT_ORIGIN_ONLY else None,
            }
        )
    return records
