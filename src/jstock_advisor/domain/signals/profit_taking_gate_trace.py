"""利確判定のaction gateが、実際に候補を抑制したかの因果追跡(Issue #720)。

`evaluate_profit_taking()`のgate(業種モデル・決算までの営業日数・一部売却の
実行可能性・強い反対材料)は、同じ入力が複数の判定経路へ同時に効くため、
「gateが閉じていた」ことと「そのgateのせいで最終判定が変わった」ことは別である
(他の経路が既に同水準以上をカバーしていれば、gateが閉じていても判定は変わらない)。

本moduleは、**遮断側にある入力を1つだけ通過側へ置換して同じ純粋関数で再評価する**
反実仮想により、その差を観測として残す。判定式・gate式・閾値は一切変更せず、
`evaluate_profit_taking()`を呼ぶだけである(本moduleはその関数のgate式を再実装
しない。**どの入力がどの判定経路へ効くかの対応表も持たない**。通過側へ置換して
再評価し、どの経路にどう効くかは関数自身が決める)。

保持先は監査記録(AuditLog output_values)であり、Recommendationへは載せない
(純粋HOLDではRecommendationが生成されないため、「gateのせいでHOLDへ落ちた」を
残せない)。値はbool・enum名・codeのみで、金額・株数・銘柄名は運ばない。

## 記録する項目(USER決定が列挙した5項目のみ)

  gate_name           どのgateか(入力が遮断側にある理由を表す固定の名前)
  gate_result         遮断側にあったことを表す値(本moduleは遮断側の入力だけを記録する)
  candidate_action    そのgateを開けた場合に成立した候補(final_action)。
                      単独で開けても変わらず、遮断中の全入力を同時に開けたときだけ
                      変わる場合は、全入力を開けた場合の候補
  actually_suppressed そのgateが実際に最終判定を抑制していたか
  superseded_by       最終判定は同じだが他の経路が既にカバーしていた場合の、
                      実際に採用された根拠(origin)。それ以外はNone

下記の分類(effect)は`actually_suppressed`/`superseded_by`/`candidate_action`を
導くための**内部の分類であり、記録には出さない**(USER決定の列挙外のため)。

  ACTION_CHANGED  置換するとfinal_actionが変わる(actually_suppressed=True)
  ORIGIN_ONLY     final_actionは同じだが、判定を押し上げた根拠(origin)が変わる
                  (他の経路が既に同水準以上のため抑制していない)
  JOINT_ONLY      単独では変わらないが、遮断中の全入力を同時に通すと変わる
                  (actually_suppressed=True)
  NONE            変わらない

## 実入力での注意(記録を後から読む人へ)

`profit_taking_service.py`は`industry_model_applied`を**定数False**で渡している
(現状の配線。業種別の専用モデルは未実装)。そのため実入力では、この入力が
**すべての保有評価で常に遮断側**となり、`INDUSTRY_MODEL_NOT_APPLIED`の記録が
必ず1件入る。これは「業種モデルの遮断が至る所で起きている」という発見ではなく、
現在の配線の定数の反映である。あわせて、遮断が他に1つでもあれば遮断入力が2件
以上になるため、JOINT判定のための追加の再評価(全入力を同時に通した1回)が
その評価で走る。
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
    """遮断側にある入力1つ分。gate名と、通過側へ置換する更新だけを持つ。"""

    gate_name: str
    # 遮断側の値を通過側へ置換する更新(ProfitTakingConditionInputsのfield名 -> 通過側の値)
    updates: dict[str, Any]


def _blocked_inputs(
    inputs: ProfitTakingConditionInputs, min_earnings_business_days: int
) -> list[_BlockedInput]:
    """遮断側にある入力だけを列挙する(通過中の入力は置換不要)。"""
    blocked: list[_BlockedInput] = []
    if not inputs.industry_model_applied:
        blocked.append(
            _BlockedInput("INDUSTRY_MODEL_NOT_APPLIED", {"industry_model_applied": True})
        )
    days = inputs.days_to_next_earnings_business_days
    if days is None:
        blocked.append(
            _BlockedInput(
                "EARNINGS_DAYS_UNKNOWN",
                {"days_to_next_earnings_business_days": min_earnings_business_days},
            )
        )
    elif days < min_earnings_business_days:
        blocked.append(
            _BlockedInput(
                "EARNINGS_TOO_CLOSE",
                {"days_to_next_earnings_business_days": min_earnings_business_days},
            )
        )
    if not inputs.partial_sale_executable:
        blocked.append(
            _BlockedInput("PARTIAL_SALE_NOT_EXECUTABLE", {"partial_sale_executable": True})
        )
    if inputs.has_strong_counter_material:
        blocked.append(
            _BlockedInput("STRONG_COUNTER_MATERIAL_PRESENT", {"has_strong_counter_material": False})
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
    joint_result: ProfitTakingResult | None = None
    needs_joint = len(blocked) >= 2 and any(
        r.final_action == baseline.final_action and r.origin == baseline.origin
        for r in single_results
    )
    if needs_joint:
        joint_updates: dict[str, Any] = {}
        for b in blocked:
            joint_updates.update(b.updates)
        joint_result = evaluate(dataclasses.replace(condition_inputs, **joint_updates))
    joint_changes_action = (
        joint_result is not None and joint_result.final_action != baseline.final_action
    )

    records: list[dict[str, object]] = []
    for b, single in zip(blocked, single_results, strict=True):
        candidate = single
        if single.final_action != baseline.final_action:
            effect = EFFECT_ACTION_CHANGED
        elif single.origin != baseline.origin:
            effect = EFFECT_ORIGIN_ONLY
        elif joint_changes_action:
            effect = EFFECT_JOINT_ONLY
            assert joint_result is not None
            candidate = joint_result
        else:
            effect = EFFECT_NONE
        records.append(
            {
                "gate_name": b.gate_name,
                "gate_result": GATE_RESULT_BLOCKED,
                "candidate_action": candidate.final_action.value,
                "actually_suppressed": effect in (EFFECT_ACTION_CHANGED, EFFECT_JOINT_ONLY),
                "superseded_by": baseline.origin if effect == EFFECT_ORIGIN_ONLY else None,
            }
        )
    return records
