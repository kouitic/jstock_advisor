"""各層の verdict の型(Issue #878 PR-1 = C0)。Arbiter への入力の契約。

入力は **層ごとの判定の結果** だけで、現行のエンジンの内部には依存しない。
買付余力(available_cash)は **型として存在しない**(資金不足だけを理由に売りを強めない。
OP-7。契約テストで固定)。未実装・未承認の評価軸(Expected Return / RAER / 資本入替)は
Determination の UNDETERMINED で表し、値を作らない。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from jstock_advisor.domain.exit_architecture.determination import Determination
from jstock_advisor.domain.exit_architecture.evidence import Evidence
from jstock_advisor.domain.exit_architecture.vocabulary import (
    RegimeState,
    ReliabilityClass,
    ThesisState,
)


@dataclass(frozen=True)
class InputReliability:
    """入力グループごとの信頼クラス(L0)。"""

    group: str
    reliability: ReliabilityClass
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.group.strip():
            raise ValueError("group は空にできない")


@dataclass(frozen=True)
class ReliabilityVerdict:
    """L0。売る理由にはならず、強い action を許すかの上限(cap)だけを決める。"""

    inputs: tuple[InputReliability, ...] = ()


@dataclass(frozen=True)
class ThesisVerdict:
    """L1(投資前提)。保有判断スコアなど既存の評価を写した結果。

    thesis_state は evidence(非価格の root)から決める。最終スコアだけを根拠にしない。
    ハードゲートが発動したときは BROKEN。ハードゲートの理由コードの多くは入力側で一次情報の確認を
    要する(DEBT_EXCESS・DIVIDEND_OMISSION_AND_CASHFLOW_CRISIS など)が、GOING_CONCERN_DOUBT
    (snapshot の判定フラグ)と INVESTMENT_THESIS_COLLAPSE(点数ベース)は要しない。開示キーワード
    由来の rule は、キーワード一致のみでも確認済みとして扱われうる。したがって『発動 = 一次情報で
    確認済み』とは一律に言えず、一次情報の確認は Evidence.primary_source_confirmed に理由コード
    ごとに表す(thesis_adapter の対応表。確認できないものは False)。
    """

    thesis_state: Determination[ThesisState]
    reliability: ReliabilityClass
    evidence: tuple[Evidence, ...] = ()
    hard_gate_triggered: bool = False
    hard_gate_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.hard_gate_triggered != bool(self.hard_gate_reasons):
            raise ValueError("hard_gate_triggered と hard_gate_reasons は同時に成立する")
        if self.hard_gate_triggered and self.thesis_state.value is not ThesisState.BROKEN:
            raise ValueError("ハードゲートが発動したら thesis_state は BROKEN")


class ExpectedReturnComponent(StrEnum):
    """L2(将来期待リターン)の構成要素。composite は 1 票(R-A)。"""

    UPSIDE = "UPSIDE"  # 上値余地
    INCOME = "INCOME"  # 配当 + 優待
    REVISION = "REVISION"  # Fair Value の修正
    RISK_ADJUSTMENT = "RISK_ADJUSTMENT"


@dataclass(frozen=True)
class ComponentValue:
    component: ExpectedReturnComponent
    value: Determination[float]


@dataclass(frozen=True)
class ExpectedReturnVerdict:
    """L2。valuation 枯渇は構成要素の 1 つの状態であり、FULL の十分条件ではない(UJ-4)。

    severely_low(FE-2: 将来期待総リターンの著しい悪化)は、全ての構成要素が確定した
    値のときだけ確定できる。未実装の軸から値を作って確定させない。
    """

    components: tuple[ComponentValue, ...]
    valuation_exhaustion: Determination[bool]
    severely_low: Determination[bool]
    reliability: ReliabilityClass
    # この verdict を支える根拠(FE-2 の FullEvidence の材料)。既定 () = 根拠なし
    # (Issue #878 PR-2 の P-2。additive)
    evidence: tuple[Evidence, ...] = ()

    def __post_init__(self) -> None:
        names = [c.component for c in self.components]
        if len(names) != len(set(names)):
            raise ValueError("構成要素が重複している")
        if self.severely_low.is_determined:
            missing = set(ExpectedReturnComponent) - {
                c.component for c in self.components if c.value.is_determined
            }
            if missing:
                raise ValueError(
                    "全ての構成要素が確定していないのに severely_low は確定できない: "
                    f"{sorted(m.value for m in missing)}"
                )


@dataclass(frozen=True)
class RegimeVerdict:
    """L3(価格の動き)。cushion(含み益)は修飾子であり、入口 gate ではない(OP-4)。"""

    state: Determination[RegimeState]
    previous_state: Determination[RegimeState]
    current_gain_pct: Determination[float]
    peak_gain_pct: Determination[float]


@dataclass(frozen=True)
class RotationVerdict:
    """L4(機会費用)。未実装の間は UNDETERMINED。"""

    gap_clear: Determination[bool]
    # この verdict を支える根拠(FE-3 の FullEvidence の材料)。既定 () = 根拠なし(P-2。additive)
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class PortfolioContext:
    """売却量・時機の文脈。買付余力(available_cash)は意図的に持たない(OP-7)。"""

    concentrated: bool
    trading_unit_feasible: bool
    earnings_window_near: bool


@dataclass(frozen=True)
class LayerVerdicts:
    """Arbiter への入力。全ての層を必ず明示する(評価しない層も UNDETERMINED で明示)。"""

    reliability: ReliabilityVerdict
    thesis: ThesisVerdict
    expected_return: ExpectedReturnVerdict
    regime: RegimeVerdict
    rotation: RotationVerdict
    context: PortfolioContext
