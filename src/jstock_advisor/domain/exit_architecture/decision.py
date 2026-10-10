"""Arbiter の出力 Decision の型と、型の側で守る不変条件(Issue #878 PR-1 = C0)。

型の不変条件(構築時に検査する)
  ・HOLD と class NONE は同値(HOLD_OPTIMAL。何もしない = 不明、ではない)
  ・action と strength の整合(PARTIAL -> PARTIAL / FULL -> FULL / HOLD -> WATCH 以下)
  ・FULL は FULL の独立根拠(FE-1〜FE-3)を 1 つ以上持つ
  ・★ FullEvidence は、kind ごとの root の対応と、裏づけとなる verdict を検査する(UJ-4):
      FE-1 は L1 の非価格の root だけから成り、裏づけの ThesisVerdict が WEAKENING か BROKEN
      FE-2 は VALUATION_LEVEL だけでは構成できず(他の構成要素の root を要する)、裏づけの
           ExpectedReturnVerdict の severely_low が確定した True
      FE-3 は L4 の root(OPPORTUNITY_COST)だけから成り、裏づけの RotationVerdict の
           gap_clear が確定した True(UNDETERMINED の間は構築できない)
    したがって『valuation 枯渇の根拠だけに、どの kind のラベルを付けても』FullEvidence も
    FULL の Decision も構築できない。価格由来(PRICE_PATH)・ユーザー目標(USER_DIRECTIVE)・
    データ品質(DATA)・時機(EVENT_RISK)・集中(PORTFOLIO)の根は、どの kind でも使えない
    (OP-5・UJ-6・OP-7)

型で防げる範囲と、Arbiter(PR-2)に委ねる範囲
  型で防ぐ  語彙の対応(kind と root の整合)・裏づけの verdict が確定した値であること
  PR-2      『強さ』の判定(どの水準の悪化を『著しい』とするか。事前登録・USER 承認で確定)・cap・
            降格・4 ケースの区別・L0 の信頼度の反映。FullEvidence は『FULL を許す入口の資格』を
            検査するだけで、FULL にすべきかは決めない
FE-1 の支える根拠に価格系の文脈(L1 の悪化と同時の価格の下落など)を記録したい場合、その文脈は
FullEvidence ではなく Decision の別の field(suppressed や context)に置く(PR-2 の設計)。
"""

from __future__ import annotations

from dataclasses import dataclass

from jstock_advisor.domain.exit_architecture.evidence import Evidence, distinct_roots
from jstock_advisor.domain.exit_architecture.verdicts import (
    ExpectedReturnVerdict,
    RotationVerdict,
    ThesisVerdict,
)
from jstock_advisor.domain.exit_architecture.vocabulary import (
    ExitAction,
    ExitClass,
    ExitLayer,
    FullEvidenceKind,
    ReviewFlag,
    RootFactor,
    Strength,
    SuppressionReason,
    ThesisState,
    TriggerKind,
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


#: kind ごとに、支える根拠の root として許す集合(語彙の対応であり、数値ではない)
ALLOWED_ROOTS_BY_KIND: dict[FullEvidenceKind, frozenset[RootFactor]] = {
    FullEvidenceKind.THESIS_DETERIORATION: frozenset(
        {
            RootFactor.EARNINGS,
            RootFactor.CASHFLOW,
            RootFactor.BALANCE_SHEET,
            RootFactor.RETURN_POLICY,
            RootFactor.GOVERNANCE_EVENT,
        }
    ),
    FullEvidenceKind.EXPECTED_RETURN_DETERIORATION: frozenset(
        {RootFactor.VALUATION_LEVEL, RootFactor.RETURN_POLICY, RootFactor.EARNINGS}
    ),
    FullEvidenceKind.ROTATION_OPPORTUNITY: frozenset({RootFactor.OPPORTUNITY_COST}),
}


class ContractViolationError(ValueError):
    """型の不変条件に反する Decision / FullEvidence を作ろうとした。"""


@dataclass(frozen=True)
class FullEvidence:
    """FULL の独立根拠 1 件(FE-1〜FE-3 のいずれか)と、それを支える根拠・裏づけの verdict。

    kind に対応する裏づけの verdict(thesis / expected_return / rotation のどれか 1 つ)が
    必須。裏づけが UNDETERMINED の間は、その kind の FullEvidence を構築できない。
    """

    kind: FullEvidenceKind
    evidence: tuple[Evidence, ...]
    thesis: ThesisVerdict | None = None
    expected_return: ExpectedReturnVerdict | None = None
    rotation: RotationVerdict | None = None

    def __post_init__(self) -> None:
        if not self.evidence:
            raise ContractViolationError("FULL の独立根拠には支える根拠が要る")
        if not any(e.counts_as_independent for e in self.evidence):
            raise ContractViolationError("推定のみ・未評価の根拠だけでは FULL の独立根拠にならない")
        roots = {e.root_factor for e in self.evidence}
        forbidden = roots & FORBIDDEN_FULL_ROOTS
        if forbidden:
            raise ContractViolationError(
                f"FULL の独立根拠に使えない root: {sorted(r.value for r in forbidden)}"
            )
        not_allowed = roots - ALLOWED_ROOTS_BY_KIND[self.kind]
        if not_allowed:
            raise ContractViolationError(
                f"{self.kind.value} の根拠に使えない root: {sorted(r.value for r in not_allowed)}"
            )
        self._check_backing()

    def _check_backing(self) -> None:
        if self.kind is FullEvidenceKind.THESIS_DETERIORATION:
            state = None if self.thesis is None else self.thesis.thesis_state.value
            if state not in (ThesisState.WEAKENING, ThesisState.BROKEN):
                raise ContractViolationError(
                    "FE-1 には、WEAKENING か BROKEN と確定した ThesisVerdict が要る"
                )
        elif self.kind is FullEvidenceKind.EXPECTED_RETURN_DETERIORATION:
            # valuation 枯渇(VALUATION_LEVEL)だけで FE-2 のラベルを付けられない
            if not distinct_roots(self.evidence) - {RootFactor.VALUATION_LEVEL}:
                raise ContractViolationError(
                    "FE-2 は VALUATION_LEVEL の根拠だけでは構成できない(他の構成要素の根拠が要る)"
                )
            low = None if self.expected_return is None else self.expected_return.severely_low.value
            if low is not True:
                raise ContractViolationError(
                    "FE-2 には、severely_low が確定した True の ExpectedReturnVerdict が要る"
                )
        else:
            gap = None if self.rotation is None else self.rotation.gap_clear.value
            if gap is not True:
                raise ContractViolationError(
                    "FE-3 には、gap_clear が確定した True の RotationVerdict が要る"
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
    # UJ-15: 『RISK_EXIT の候補 + review_flag』を、action と別の軸で表す(review_flag は売却の推奨
    # ではなく、人の確認を要することを表す)。review_class を持つなら review_flag != NONE が要る。
    # 逆(review_flag だけ)は既存の構築経路〔C0〕のために許し、Arbiter は常に両方を設定する
    # (P-4。additive)
    review_class: ExitClass | None = None
    # HOLD の根拠が確定(hold_optimal)か不明かを区別する。評価できなかった層の一覧。空でない間は
    # hold_optimal = True にならない(不明は HOLD_OPTIMAL ではない)(P-4。additive)
    undetermined_layers: tuple[ExitLayer, ...] = ()
    # 勝った候補の種別(HOLD のとき None)。N5 が成立経路〔origin〕へ戻すために使う。
    # supporting_triggers = 同じ強さに達した、勝たなかった種別(表示・trace 用。
    # 強さの算出には使わない)(P-5。additive)
    trigger_kind: TriggerKind | None = None
    supporting_triggers: tuple[TriggerKind, ...] = ()

    def __post_init__(self) -> None:
        if (self.action is ExitAction.HOLD) != (self.exit_class is ExitClass.NONE):
            raise ContractViolationError("HOLD と class NONE は同値(HOLD_OPTIMAL)")
        if self.hold_optimal and self.action is not ExitAction.HOLD:
            raise ContractViolationError("hold_optimal は HOLD のときだけ")
        if self.hold_optimal and self.undetermined_layers:
            raise ContractViolationError("評価できなかった層がある間は hold_optimal にならない")
        if self.review_class is not None and self.review_flag is ReviewFlag.NONE:
            raise ContractViolationError("review_class を持つには review_flag が要る")
        if self.action is ExitAction.HOLD and (self.trigger_kind or self.supporting_triggers):
            raise ContractViolationError("HOLD には勝った候補の種別がない")
        if self.supporting_triggers and self.trigger_kind is None:
            raise ContractViolationError("supporting_triggers は trigger_kind とともに持つ")
        if self.trigger_kind in self.supporting_triggers:
            raise ContractViolationError("勝った種別を supporting_triggers に重ねて持たない")
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
