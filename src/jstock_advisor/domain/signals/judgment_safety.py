"""判断の安全条件の評価(shadow計測・将来のenforcement共用の純関数。Issue #160 PR-1)。

## 位置づけ

判定・通知・保存を**変えずに**、「強い判定が、安全条件を満たさないまま出ていないか」を評価する。
本モジュールは純関数と型だけを持ち、判定経路のどこにも接続されていない(挙動不変)。
shadow(観測のみ)とenforcement(将来)で**同じ関数**を使い、実装を2つに割らない。

## 純関数であること

I/O・時刻・module-globalを持たない。入力(推奨・非永続のfacts・設定)だけから結果が決まる。
入力を変更しない。

## 評価する条件(いずれも「validator型」= 問題を検出するだけで、判定の種別は書き換えない)

  G1 EARNINGS_DATE_UNKNOWN            決算日が不明のまま強い判定が出る
  G2 STALE_FINANCIALS                 BUYの強い判定で財務が古い(BUYのみ。USER決定 Q-D)
  G3 REQUIRED_INPUT_MISSING:<field>   利確FULLの緩和要因が不明のまま強い判定が出る
  G4 CORPORATE_ACTION_UNRESOLVED:<T>  株式分割・併合が未解決のまま強い判定が出る(Q-A)

G5(単一根拠の重複checkの整理)と条件8(適正価格LOW)は既存validatorとの整理を伴うため本モジュールには
含めない(別PR)。

## 強い判定

  BUY系     `buy_action` が STRONG_BUY / BUY / SMALL_ENTRY
  利確FULL  `recommendation_type` が FULL_PROFIT_TAKE
SELL / URGENT_REVIEW等へは拡張しない(USER決定 Q-B)。

## 「評価していない」を「該当しない」と混同しない

入力が無い(未供給・評価失敗)条件は`not_evaluated`へ列挙し、findingを作らない。**Falseや欠損を
UNKNOWNと推測しない**(USER決定 F1)。G3は、UNKNOWN(None)を事実として識別できる2項目だけを
対象にする。識別できない項目(`UNMEASURABLE_G3_INPUTS`)は測定不能として明示し、件数へ含めない。

## findingの内容

条件ID・reason code・suppress対象かだけを持つ。銘柄コード・識別子・保有情報・価格を含めない(#135)。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

from jstock_advisor.domain.entities.enums import (
    BUY_FAMILY_ACTIONS,
    EarningsDateStatus,
    RecommendationType,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.judgment_safety_shadow_config import (
    G3MeasurableInput,
    JudgmentSafetyShadowConfig,
)

ConditionId = Literal["G1", "G2", "G3", "G4"]

REASON_EARNINGS_DATE_UNKNOWN: Final = "EARNINGS_DATE_UNKNOWN"
REASON_STALE_FINANCIALS: Final = "STALE_FINANCIALS"
REASON_REQUIRED_INPUT_MISSING: Final = "REQUIRED_INPUT_MISSING"
REASON_CORPORATE_ACTION_UNRESOLVED: Final = "CORPORATE_ACTION_UNRESOLVED"

#: 決算日が「不明」と言える状態(評価日より過去の値=更新遅延も、有効な次回決算日ではないため含める)。
_EARNINGS_DATE_UNKNOWN_STATUSES: Final = frozenset(
    {EarningsDateStatus.UNAVAILABLE, EarningsDateStatus.STALE_PAST_DATE}
)

#: G3の対象外(今回のshadowでは測定不能)。現在のコードでUNKNOWNを事実として識別できない。
#: 件数へ含めない(FalseをUNKNOWNと推測しない。USER決定 F1 / U3 / U4)。
UNMEASURABLE_G3_INPUTS: Final[tuple[str, ...]] = (
    "fair_value_rising_with_earnings_growth",
    "long_term_holding_benefit_imminent",
    "few_reinvestment_alternatives",
)

CorporateActionState = Literal["EVALUATED", "NOT_EVALUATED", "COMPUTATION_FAILED"]
UnresolvedCorporateAction = Literal["SPLIT", "REVERSE_SPLIT"]


@dataclass(frozen=True)
class ProfitTakingMitigationFacts:
    """利確の緩和要因のうち、UNKNOWN(None)を事実として識別できる2項目の実値。

    値は`profit_taking_service`が`MitigatingFactorInputs`を構築する時点の実値であり、
    Noneは「取得できていない(UNKNOWN)」、0/False は「確認した結果」を意味する。
    """

    continuous_dividend_increase_years: int | None
    is_progressive_or_doe_policy: bool | None


@dataclass(frozen=True)
class CorporateActionFacts:
    """株式分割・併合の評価結果(SPLIT / REVERSE_SPLIT のみ。MERGER等は表現しない)。"""

    state: CorporateActionState
    unresolved: tuple[UnresolvedCorporateAction, ...] = ()


@dataclass(frozen=True)
class SafetyFacts:
    """判定に入る前の事実(非永続。Recommendation等のschemaへは載せない)。

    Noneは「供給されていない」を意味し、該当なしとは区別する(not_evaluatedになる)。
    """

    #: 財務データが古いか(BUY側。BUYの財務鮮度判定(STALE)から供給)。
    financials_are_stale: bool | None = None
    profit_taking_mitigation: ProfitTakingMitigationFacts | None = None
    corporate_action: CorporateActionFacts | None = None


@dataclass(frozen=True)
class SafetyFinding:
    condition_id: ConditionId
    reason_code: str
    #: 将来のenforcementで、manual review要求 / actionable通知の抑止の対象になるか(validator型)。
    would_suppress: bool = True


@dataclass(frozen=True)
class SafetyEvaluation:
    findings: tuple[SafetyFinding, ...]
    #: 入力が無く評価できなかった条件(「該当なし」ではない。件数へ含めない)。
    not_evaluated: tuple[ConditionId, ...]


def is_strong_buy_side(recommendation: Recommendation) -> bool:
    return recommendation.buy_action in BUY_FAMILY_ACTIONS


def is_strong_full_profit_take(recommendation: Recommendation) -> bool:
    return recommendation.recommendation_type is RecommendationType.FULL_PROFIT_TAKE


def _is_strong(recommendation: Recommendation) -> bool:
    return is_strong_buy_side(recommendation) or is_strong_full_profit_take(recommendation)


def _g3_value(facts: ProfitTakingMitigationFacts, name: G3MeasurableInput) -> object:
    return getattr(facts, name)


def evaluate_safety_conditions(
    recommendation: Recommendation,
    facts: SafetyFacts,
    config: JudgmentSafetyShadowConfig,
) -> SafetyEvaluation:
    """強い判定が安全条件を満たしているかを評価する(純関数。判定・入力を変更しない)。

    `config.mode`は参照しない(modeによる実行の有無は呼び出し側の責務)。
    """
    findings: list[SafetyFinding] = []
    not_evaluated: list[ConditionId] = []

    strong = _is_strong(recommendation)

    # G1: 強い判定で決算日が不明
    if strong:
        status = recommendation.earnings_date_status
        if status is None:
            not_evaluated.append("G1")
        elif status in _EARNINGS_DATE_UNKNOWN_STATUSES:
            findings.append(SafetyFinding("G1", REASON_EARNINGS_DATE_UNKNOWN))

    # G2: BUYの強い判定で財務が古い(BUYのみ)
    if is_strong_buy_side(recommendation):
        if facts.financials_are_stale is None:
            not_evaluated.append("G2")
        elif facts.financials_are_stale:
            findings.append(SafetyFinding("G2", REASON_STALE_FINANCIALS))

    # G3: 利確FULLで、測定可能な必須入力のいずれかがUNKNOWN
    if is_strong_full_profit_take(recommendation):
        if facts.profit_taking_mitigation is None:
            not_evaluated.append("G3")
        else:
            # 設定側でも重複は弾くが、評価側でも除去する(件数の水増しを構造的に防ぐ。G4と同じ扱い)
            for name in dict.fromkeys(config.g3_required_inputs):
                if _g3_value(facts.profit_taking_mitigation, name) is None:
                    findings.append(SafetyFinding("G3", f"{REASON_REQUIRED_INPUT_MISSING}:{name}"))

    # G4: 強い判定で株式分割・併合が未解決
    if strong:
        action = facts.corporate_action
        if action is None or action.state != "EVALUATED":
            not_evaluated.append("G4")
        else:
            for kind in _sorted_unique(action.unresolved):
                findings.append(SafetyFinding("G4", f"{REASON_CORPORATE_ACTION_UNRESOLVED}:{kind}"))

    return SafetyEvaluation(findings=tuple(findings), not_evaluated=tuple(not_evaluated))


def _sorted_unique(values: Iterable[UnresolvedCorporateAction]) -> list[UnresolvedCorporateAction]:
    return sorted(set(values))
