"""Arbiter の出力 Decision の型と、型の側で守る不変条件(Issue #878 PR-1 = C0)。

型の不変条件(構築時に検査する)
  ・HOLD と class NONE は同値(HOLD_OPTIMAL。何もしない = 不明、ではない)
  ・action と strength の整合(PARTIAL -> PARTIAL / FULL -> FULL / HOLD -> WATCH 以下)
  ・★ FULL は FULL の独立根拠(FE-1〜FE-3)を 1 つ以上持つ。valuation 枯渇のみでは FULL に
    ならない(UJ-4)。価格由来(PRICE_PATH)・ユーザー目標(USER_DIRECTIVE)・データ品質(DATA)・
    時機(EVENT_RISK)・集中(PORTFOLIO)の根は、FULL の独立根拠に使えない(OP-5・UJ-6・OP-7)
Arbiter 本体(強さの判定・cap・降格・4 ケースの区別)は #878 の PR-2 であり、ここには置かない。
"""

from __future__ import annotations

from dataclasses import dataclass

from jstock_advisor.domain.exit_architecture.evidence import Evidence, distinct_roots
from jstock_advisor.domain.exit_architecture.vocabulary import (
    ExitAction,
    ExitClass,
    ExitLayer,
    FullEvidenceKind,
    ReviewFlag,
    RootFactor,
    Strength,
    SuppressionReason,
)

#: FULL の独立根拠に使えない root(価格由来・ユーザー目標・データ品質・時機・集中)
FORBIDDEN_FULL_ROOTS: frozenset[RootFactor] = frozenset(
    {
        RootFactor.PRICE_PATH,
        RootFactor.USER_DIRECTIVE,
        RootFactor.DATA,
        RootFactor.EVENT_RISK,
        RootFactor.PORTFOLIO,
    }
)


class ContractViolationError(ValueError):
    """型の不変条件に反する Decision / FullEvidence を作ろうとした。"""


@dataclass(frozen=True)
class FullEvidence:
    """FULL の独立根拠 1 件(FE-1〜FE-3 のいずれか)と、それを支える根拠。"""

    kind: FullEvidenceKind
    evidence: tuple[Evidence, ...]

    def __post_init__(self) -> None:
        if not self.evidence:
            raise ContractViolationError("FULL の独立根拠には支える根拠が要る")
        if not any(e.counts_as_independent for e in self.evidence):
            raise ContractViolationError("推定のみ・未評価の根拠だけでは FULL の独立根拠にならない")
        forbidden = {e.root_factor for e in self.evidence} & FORBIDDEN_FULL_ROOTS
        if forbidden:
            raise ContractViolationError(
                f"FULL の独立根拠に使えない root: {sorted(r.value for r in forbidden)}"
            )


@dataclass(frozen=True)
class SuppressedCandidate:
    """選ばれなかった候補と、その理由(因果の記録)。"""

    exit_class: ExitClass
    strength: Strength
    reason: SuppressionReason


@dataclass(frozen=True)
class Decision:
    """Arbiter の出力。売却の必要性(action・class・strength)。数量は sizing の責務。"""

    action: ExitAction
    exit_class: ExitClass
    strength: Strength
    primary_layer: ExitLayer | None = None
    full_evidence: tuple[FullEvidence, ...] = ()
    suppressed: tuple[SuppressedCandidate, ...] = ()
    review_flag: ReviewFlag = ReviewFlag.NONE
    hold_optimal: bool = False

    def __post_init__(self) -> None:
        if (self.action is ExitAction.HOLD) != (self.exit_class is ExitClass.NONE):
            raise ContractViolationError("HOLD と class NONE は同値(HOLD_OPTIMAL)")
        if self.hold_optimal and self.action is not ExitAction.HOLD:
            raise ContractViolationError("hold_optimal は HOLD のときだけ")
        if self.action is ExitAction.HOLD and self.strength > Strength.WATCH:
            raise ContractViolationError("HOLD の strength は WATCH 以下")
        if self.action is ExitAction.PARTIAL and self.strength is not Strength.PARTIAL:
            raise ContractViolationError("PARTIAL の strength は PARTIAL")
        if self.action is ExitAction.FULL and self.strength is not Strength.FULL:
            raise ContractViolationError("FULL の strength は FULL")
        if self.action is not ExitAction.HOLD and self.primary_layer is None:
            raise ContractViolationError("売却の action には主たる層が要る")
        if self.action is ExitAction.FULL and not self.full_evidence:
            raise ContractViolationError(
                "FULL には FULL の独立根拠(FE-1〜FE-3)が要る。"
                "valuation 枯渇のみでは FULL にならない"
            )

    @property
    def independent_roots(self) -> frozenset[RootFactor]:
        """FULL の根拠にした独立 root(R-A。同じ root は 1)。"""
        roots: frozenset[RootFactor] = frozenset()
        for item in self.full_evidence:
            roots |= distinct_roots(item.evidence)
        return roots
