"""L2(将来期待リターン)の verdict adapter(Issue #601 PR-1b。dormant)。

Expected Return の結果(`ExpectedReturnResult`。#601 PR-1a)を、**新しい計算をせずに**、L2 の verdict
(`ExpectedReturnVerdict`)へ写す純粋関数。どこからも import されない(配線なし・保存なし・flag なし)。
USER 承認: #122 issuecomment-6102822046 の『2. #601 PR-1b』。

## 性質(契約テストで固定する)

  ・読取専用の純粋関数: `ExpectedReturnResult`(メモリ上の型)を引数で受け取るだけ。値を再計算しない
  ・値を捏造しない: 確定した値はそのまま写す(丸め・符号の変更・補正なし)。値が無い構成要素は
    UNDETERMINED とし、0 や中立値で埋めない
  ・UPSIDE / INCOME / REVISION は `ExpectedReturnResult` の構成要素をそのまま写す。
    RISK_ADJUSTMENT は RAER(#602)の軸で、本 adapter は値を作らない = 常に UNDETERMINED
    (COMPONENT_NOT_IMPLEMENTED)
  ・★ composite・年率換算は verdict の構成要素ではない ★ 写さない(式が未決。USER 承認の範囲外)
  ・valuation_exhaustion / severely_low は決めない(UNDETERMINED)。これらは Exit 側が、全構成要素の
    確定を条件に決める(`ExpectedReturnVerdict` の検証が、全構成要素が確定でなければ severely_low を
    確定させない)
  ・reliability(L0)は**引数で受け取る**。fair_value_confidence や注記を L0 に写す方針は Exit 側
    (#846)の判断で、本 adapter は決めない
  ・注記(`Note`)を黙って捨てない: 優待が『制度なし(未登録を含む)』として寄与 0 で確定した INCOME、
    価格が 1 取引セッション前の UPSIDE は、確定した値の `detail` に注記名を残す(USER 決定 D-601-4:
    この注記が付いた INCOME は、その事実を区別して扱う)
  ・理由コードの対応は全メンバーについて明示する(下の `_REASON_TO_UNDETERMINED`)。対応が無い理由が
    構成要素に現れたら、推測で写さず拒否する

## 持たないもの

  arbiter・FULL の判定・Evidence の生成(根拠を作らない = `evidence` は既定の ())・RAER の値・
  取得価格 / 買付余力 / 時計・services / infrastructure / providers への依存
"""

from __future__ import annotations

from jstock_advisor.domain.exit_architecture.determination import Determination
from jstock_advisor.domain.exit_architecture.verdicts import (
    ComponentValue,
    ExpectedReturnComponent,
    ExpectedReturnVerdict,
)
from jstock_advisor.domain.exit_architecture.vocabulary import ReliabilityClass, UndeterminedReason
from jstock_advisor.domain.valuation.expected_return import (
    ComponentResult,
    ExpectedReturnResult,
    Note,
    ReasonCode,
)

#: `ReasonCode` -> verdict の UNDETERMINED の理由。None は『verdict の構成要素の理由ではない』
#: (composite・年率換算は verdict に写さないため、これらの理由は構成要素に現れない)。
#: 全メンバーを明示する(メンバーが増えたら契約テストが落ち、対応を決めさせる)。
_REASON_TO_UNDETERMINED: dict[ReasonCode, UndeterminedReason | None] = {
    ReasonCode.CURRENT_PRICE_UNAVAILABLE: UndeterminedReason.INPUT_MISSING,
    ReasonCode.FAIR_VALUE_UNAVAILABLE: UndeterminedReason.INPUT_MISSING,
    ReasonCode.DIVIDEND_UNKNOWN: UndeterminedReason.INPUT_MISSING,
    ReasonCode.BENEFIT_UNVALUABLE: UndeterminedReason.INPUT_MISSING,
    ReasonCode.CURRENT_PRICE_STALE: UndeterminedReason.RELIABILITY_UNUSABLE,
    ReasonCode.FAIR_VALUE_NOT_USABLE: UndeterminedReason.RELIABILITY_UNUSABLE,
    ReasonCode.REVISION_NOT_IMPLEMENTED: UndeterminedReason.COMPONENT_NOT_IMPLEMENTED,
    ReasonCode.COMPOSITION_RULE_NOT_DECIDED: None,
    ReasonCode.HORIZON_NOT_ESTIMATED: None,
}

#: 複数の理由があるときに代表として選ぶ順(入力が無いことが最も基本。次に信頼できない。
#: 未実装は最後)。
#: 全件は `detail` に残す。
_PRIORITY: tuple[UndeterminedReason, ...] = (
    UndeterminedReason.INPUT_MISSING,
    UndeterminedReason.RELIABILITY_UNUSABLE,
    UndeterminedReason.COMPONENT_NOT_IMPLEMENTED,
)

#: 注記 -> その注記が意味を限定する構成要素(全メンバーを明示する)。
_NOTE_COMPONENT: dict[Note, ExpectedReturnComponent] = {
    Note.PRICE_FRESHNESS_WARNING: ExpectedReturnComponent.UPSIDE,
    Note.BENEFIT_NO_PROGRAM_OR_UNREGISTERED: ExpectedReturnComponent.INCOME,
}


def _detail_of_notes(notes: tuple[Note, ...], component: ExpectedReturnComponent) -> str:
    names = [note.value for note in notes if _NOTE_COMPONENT[note] is component]
    return f"notes={','.join(names)}" if names else ""


def _to_determination(
    result: ComponentResult, component: ExpectedReturnComponent, notes: tuple[Note, ...]
) -> Determination[float]:
    """構成要素 1 つを写す。確定した値はそのまま、無ければ理由を写して UNDETERMINED。"""
    if result.value is not None:
        return Determination(value=result.value, detail=_detail_of_notes(notes, component))
    mapped = {_REASON_TO_UNDETERMINED[reason] for reason in result.reasons}
    mapped.discard(None)
    if not mapped:
        raise ValueError(
            f"verdict の構成要素 {component.value} に写せない理由: "
            f"{sorted(reason.value for reason in result.reasons)}"
        )
    chosen = next(reason for reason in _PRIORITY if reason in mapped)
    parts = [f"er_reasons={','.join(reason.value for reason in result.reasons)}"]
    if result.detail:
        parts.append(result.detail)
    return Determination.undetermined(chosen, ";".join(parts))


def to_expected_return_verdict(
    result: ExpectedReturnResult, *, reliability: ReliabilityClass
) -> ExpectedReturnVerdict:
    """`ExpectedReturnResult` を L2 の `ExpectedReturnVerdict` へ写す(純粋関数。計算しない)。

    reliability(L0)は呼び出し側(Exit 側)が決めて渡す。valuation_exhaustion / severely_low は決めない
    (UNDETERMINED)。構成要素は UPSIDE・INCOME・REVISION・RISK_ADJUSTMENT を固定の順で必ず持つ。
    """
    components = (
        ComponentValue(
            ExpectedReturnComponent.UPSIDE,
            _to_determination(result.upside, ExpectedReturnComponent.UPSIDE, result.notes),
        ),
        ComponentValue(
            ExpectedReturnComponent.INCOME,
            _to_determination(result.income, ExpectedReturnComponent.INCOME, result.notes),
        ),
        ComponentValue(
            ExpectedReturnComponent.REVISION,
            _to_determination(result.revision, ExpectedReturnComponent.REVISION, result.notes),
        ),
        ComponentValue(
            ExpectedReturnComponent.RISK_ADJUSTMENT,
            Determination.undetermined(
                UndeterminedReason.COMPONENT_NOT_IMPLEMENTED, "risk_adjustment_not_computed_here"
            ),
        ),
    )
    return ExpectedReturnVerdict(
        components=components,
        valuation_exhaustion=Determination.undetermined(
            UndeterminedReason.NOT_EVALUATED, "decided_by_the_exit_side"
        ),
        severely_low=Determination.undetermined(
            UndeterminedReason.NOT_EVALUATED, "decided_by_the_exit_side"
        ),
        reliability=reliability,
    )
