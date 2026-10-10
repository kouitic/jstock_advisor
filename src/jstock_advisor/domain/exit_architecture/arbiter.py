"""Arbiter: 各層の verdict と候補から、最終 action を決める純粋関数(Issue #878 PR-2。dormant)。

どこからも import されない(配線なし・保存なし・flag なし・AWS なし)。現行の判定・通知・閾値・
売却比率は変えない。設計は #878 の rev1〜rev5(USER の方針 9 項目 = 推奨どおり承認:
#122 issuecomment-6094041923)。

手順(順序は固定。現行の profit_taking.py と同じ順序を保つ = 売却比率を変えない)
  0 L0 が全体として UNUSABLE なら UNDECIDABLE(HOLD ではない。Determination の UNDETERMINED)
  1 根拠の重複排除(event_id があればそれ、無ければ fact_key)
  2 FE-1〜FE-3 の適格判定(型の検査 + policy の条件)
  3 候補の生成。★ 資格の上限 (a) をここで適用する ★: FULL は適格な FE があるときだけ・
    PROFIT_PROTECTION は FE が適格でなければ PARTIAL 止まり・評価できない層は候補を作らない・
    信頼性が使えない入力に依存する候補は作らない・DEGRADED の入力に依存する候補は降格
  4 ★ 勝者の選択 ★: 強さ最大 -> origin の優先順位最大 -> TriggerKind の固定の順序。
    選択に使う強さは (b) の降格の前の値
  5 ★ 勝者にだけ降格 (b) ★(現行の profit_taking.py と同じ順序): 緩和要因 -> 監視の下限 ->
    origin 別の床 -> タイミング層 -> 監視の下限 -> origin 別の最終の床。決算直前は降格として
    持たない(現行は ceiling の利用可否 = E2 の入力側の gate。Arbiter が二重に降格しない)
  6 trace: 選ばれなかった候補・上限・降格を suppressed に理由つきで残す

候補の出所と強さ
  ・候補(CandidateProposal)は adapter(PR-3)が、現行エンジンのどの経路で成立したかを表す
    TriggerKind と、その経路が主張する強さ(claimed_strength)で出す。TriggerKind = origin は
    『現行の事実』であり、Arbiter が層の状態から決めない(売却比率が origin で決まるため、
    Arbiter が origin を作ると現行の比率が変わりうる)
  ・regime の状態 -> 強さの写像は policy.regime_strength。regime_claimed_strength() は、adapter が
    claimed_strength を作るときに使う純粋関数(Arbiter 自身は regime 候補を作らない)
  ・root 数ベースの強さの算出(M1: 冗長な根拠で強さが増えない・M2・M6)は adapter の契約(PR-3)。
    Arbiter は主張された強さを受け、資格の上限・選択・降格を決める
  ・L0 の入力が空(inputs = ())は『上限なし』として進む(現行のエンジンに L0 が無いため)。
    UNDECIDABLE は『全ての入力グループが UNUSABLE』のときだけ

型が決めること(C0)と Arbiter が決めること
  型  語彙の対応・裏づけの verdict が確定した値であること
  本 module  どの水準を適格とするか(policy の値)・cap・降格・選択
値(root の数・降格の段数・regime の強さ)は policy で受け取り、既定値を置かない
(事前登録 -> 並行計算 -> 過去データで検証 -> USER の個別承認)。

USER の方針で『配線しない / 通知のみ』のもの
  ・ユーザー設定の目標到達(USER_TARGET_*)は入力にできない(通知の pass-through は別の出力)
  ・投資前提の悪化(L1)は売却の推奨にしない: review_flag + review_class(RISK_EXIT)で渡す
  ・FE-2 / FE-3 は L2 / L4 が UNDETERMINED の間は適格にならない(擬似的な期待リターンを作らない)
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum

from jstock_advisor.domain.exit_architecture.decision import (
    ALLOWED_ROOTS_BY_KIND,
    ContractViolationError,
    Decision,
    FullEvidence,
    SuppressedCandidate,
)
from jstock_advisor.domain.exit_architecture.determination import Determination
from jstock_advisor.domain.exit_architecture.evidence import (
    Evidence,
    EvidenceStatus,
    dedupe_evidence,
    distinct_roots,
)
from jstock_advisor.domain.exit_architecture.price_regime import REGIME_ORDER, REGIME_VOTE_SOURCE
from jstock_advisor.domain.exit_architecture.verdicts import (
    ExpectedReturnVerdict,
    LayerVerdicts,
    RotationVerdict,
    ThesisVerdict,
)
from jstock_advisor.domain.exit_architecture.vocabulary import (
    ExitAction,
    ExitClass,
    ExitLayer,
    FullEvidenceKind,
    RegimeState,
    ReliabilityClass,
    ReviewFlag,
    RootFactor,
    Strength,
    SuppressionReason,
    ThesisState,
    TriggerKind,
    UndeterminedReason,
)


class Origin(IntEnum):
    """成立経路の優先順位(現行の _RawLevelOrigin と同じ順序。数値の大きいほうが優先)。"""

    OTHER_CONDITIONS = 1
    PRICE_POSITION = 2
    PROFIT_PROTECTION_STRONG = 3
    FAIR_VALUE_STRONG = 4
    FUNDAMENTAL_CRITICAL_RISK = 5


#: Arbiter の入力になる TriggerKind 7 種 -> origin。USER_TARGET_PRICE / USER_TARGET_RATE は入れない
#: (ユーザー目標の到達は通知のみ。UJ-6)
ORIGIN_OF_TRIGGER: dict[TriggerKind, Origin] = {
    TriggerKind.PARTIAL_CONDITIONS: Origin.OTHER_CONDITIONS,
    TriggerKind.FULL_MODERATE_CONDITIONS: Origin.OTHER_CONDITIONS,
    TriggerKind.PRICE_UPSIDE_MATRIX: Origin.PRICE_POSITION,
    TriggerKind.PROFIT_PROTECTION_STRONG: Origin.PROFIT_PROTECTION_STRONG,
    TriggerKind.FAIR_VALUE_STRONG: Origin.FAIR_VALUE_STRONG,
    TriggerKind.FAIR_VALUE_PARTIAL_GATE: Origin.FAIR_VALUE_STRONG,
    TriggerKind.FULL_STRONG_CRITICAL: Origin.FUNDAMENTAL_CRITICAL_RISK,
}

#: 同じ強さ・同じ origin の候補が複数あるときの、勝った TriggerKind の固定の順序(先頭が優先)。
#: C0 の語彙の定義順に依存させない。変えても強さ・origin・売却比率は変わらず、報告される
#: trigger_kind だけが変わる(同じ origin の中の順序のため)
TRIGGER_TIE_ORDER: tuple[TriggerKind, ...] = (
    TriggerKind.FULL_STRONG_CRITICAL,
    TriggerKind.FAIR_VALUE_STRONG,
    TriggerKind.FAIR_VALUE_PARTIAL_GATE,
    TriggerKind.PROFIT_PROTECTION_STRONG,
    TriggerKind.PRICE_UPSIDE_MATRIX,
    TriggerKind.FULL_MODERATE_CONDITIONS,
    TriggerKind.PARTIAL_CONDITIONS,
)

#: TriggerKind ごとに許す class
ALLOWED_CLASSES_BY_TRIGGER: dict[TriggerKind, frozenset[ExitClass]] = {
    TriggerKind.PARTIAL_CONDITIONS: frozenset({ExitClass.PROFIT_PROTECTION, ExitClass.VALUE_EXIT}),
    TriggerKind.FULL_MODERATE_CONDITIONS: frozenset(
        {ExitClass.PROFIT_PROTECTION, ExitClass.VALUE_EXIT}
    ),
    TriggerKind.PRICE_UPSIDE_MATRIX: frozenset({ExitClass.VALUE_EXIT}),
    TriggerKind.PROFIT_PROTECTION_STRONG: frozenset({ExitClass.PROFIT_PROTECTION}),
    TriggerKind.FAIR_VALUE_STRONG: frozenset({ExitClass.VALUE_EXIT}),
    TriggerKind.FAIR_VALUE_PARTIAL_GATE: frozenset({ExitClass.VALUE_EXIT}),
    TriggerKind.FULL_STRONG_CRITICAL: frozenset({ExitClass.RISK_EXIT}),
}

#: 下限の保証(PARTIAL 未満へ下げない)の対象 origin。USER の方針: 現行の保証を保存する(U-2)
FLOOR_ORIGINS: frozenset[Origin] = frozenset(
    {Origin.PRICE_POSITION, Origin.FAIR_VALUE_STRONG, Origin.PROFIT_PROTECTION_STRONG}
)

#: 緩和・タイミング・決算直前の降格の対象外の origin(現行維持)
SOFTENING_EXEMPT_ORIGINS: frozenset[Origin] = frozenset({Origin.FUNDAMENTAL_CRITICAL_RISK})

_CLASS_ORDER: tuple[ExitClass, ...] = (
    ExitClass.RISK_EXIT,
    ExitClass.VALUE_EXIT,
    ExitClass.PROFIT_PROTECTION,
    ExitClass.CAPITAL_ROTATION,
)


def _down(strength: Strength, steps: int) -> Strength:
    return Strength(max(int(Strength.NONE), int(strength) - steps))


# ---------------------------------------------------------------------------
# 入力の型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateProposal:
    """層の評価(adapter。PR-3)が出す候補。claimed_strength は資格の上限 (a) の前の主張。

    evidence      この候補を支える根拠(USER_DIRECTIVE の root は入れられない)
    input_groups  依存する L0 の入力グループ(信頼性が使えない / DEGRADED の入力を判定する)
    """

    trigger_kind: TriggerKind
    exit_class: ExitClass
    claimed_strength: Strength
    layer: ExitLayer
    evidence: tuple[Evidence, ...] = ()
    input_groups: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.trigger_kind not in ORIGIN_OF_TRIGGER:
            raise ContractViolationError(
                f"Arbiter の入力にできない種別(ユーザー目標は通知のみ): {self.trigger_kind.value}"
            )
        if self.exit_class not in ALLOWED_CLASSES_BY_TRIGGER[self.trigger_kind]:
            raise ContractViolationError(
                f"{self.trigger_kind.value} に対応しない class: {self.exit_class.value}"
            )
        if self.claimed_strength < Strength.WATCH:
            raise ContractViolationError("候補の強さは WATCH 以上")
        if self.layer is ExitLayer.L0_DATA_RELIABILITY:
            raise ContractViolationError("L0 は候補を出さない(上限を決めるだけ)")
        _reject_user_directive(self.evidence)


@dataclass(frozen=True)
class SofteningFacts:
    """勝者にだけ適用する降格 (b) の事実。値(段数)は policy。事実は呼び出し側(adapter)が出す。

    mitigation_steps  緩和要因による降格の段数(現行の _apply_mitigating_factors の結果)
    uptrend           上昇トレンド
    hard_overvalued   上限価格を超過し、信頼度が LOW でない(タイミング層の降格を免れる)
    """

    mitigation_steps: int
    uptrend: bool
    hard_overvalued: bool

    def __post_init__(self) -> None:
        if isinstance(self.mitigation_steps, bool) or not isinstance(self.mitigation_steps, int):
            raise TypeError("mitigation_steps は int")
        if self.mitigation_steps < 0:
            raise ValueError("mitigation_steps は 0 以上")


@dataclass(frozen=True)
class ArbiterPolicy:
    """Arbiter の値。**既定値は無い**(事前登録 -> 並行計算 -> 過去データで検証 -> USER の個別承認)。

    fe1_min_distinct_roots_when_weakening  WEAKENING で FE-1 が適格になる、独立な非価格の root の
                                           数
    fe1_min_primary_confirmed_roots        同、一次情報で確認された root の数(どちらかで適格)
    fe1_broken_needs_primary_root          BROKEN の FE-1 に、一次情報で確認された root を要するか
    degraded_downgrade_steps               L0 が DEGRADED の入力に依存する候補の降格の段数
    timing_downgrade_steps                 タイミング層(上昇トレンド)の降格の段数(現行は 1)
    regime_strength                        regime の状態 -> 候補の強さ(単調・FULL を含まない)
    """

    fe1_min_distinct_roots_when_weakening: int
    fe1_min_primary_confirmed_roots: int
    fe1_broken_needs_primary_root: bool
    degraded_downgrade_steps: int
    timing_downgrade_steps: int
    regime_strength: Mapping[RegimeState, Strength]

    def __post_init__(self) -> None:
        for name in (
            "fe1_min_distinct_roots_when_weakening",
            "fe1_min_primary_confirmed_roots",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} は 1 以上の整数")
        for name in (
            "degraded_downgrade_steps",
            "timing_downgrade_steps",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} は 0 以上の整数")
        if not isinstance(self.fe1_broken_needs_primary_root, bool):
            raise TypeError("fe1_broken_needs_primary_root は bool")
        if set(self.regime_strength) != set(RegimeState):
            raise ValueError("regime_strength は全ての RegimeState を持つ")
        ranks = [self.regime_strength[state] for state in REGIME_ORDER]
        if ranks != sorted(ranks):
            raise ValueError("regime_strength は状態が重いほど軽くならない(単調)")
        if any(strength >= Strength.FULL for strength in ranks):
            raise ValueError("regime(価格由来)は FULL の独立根拠にならない: FULL を含めない")
        if self.regime_strength[RegimeState.HEALTHY] is not Strength.NONE:
            raise ValueError("HEALTHY は候補を作らない(NONE)")


@dataclass(frozen=True)
class ArbiterInput:
    layers: LayerVerdicts
    proposals: tuple[CandidateProposal, ...]
    softening: SofteningFacts
    policy: ArbiterPolicy


@dataclass(frozen=True)
class Candidate:
    """資格の上限 (a) を適用した後の候補。選択に使う強さ(降格 (b) の前)。"""

    proposal: CandidateProposal
    strength: Strength
    origin: Origin

    @property
    def sort_key(self) -> tuple[int, int, int, int]:
        """大きいほうが勝つ。強さ -> origin の優先順位 -> TriggerKind の固定の順序 -> class。"""
        tie = len(TRIGGER_TIE_ORDER) - TRIGGER_TIE_ORDER.index(self.proposal.trigger_kind)
        cls = len(_CLASS_ORDER) - _CLASS_ORDER.index(self.proposal.exit_class)
        return (int(self.strength), int(self.origin), tie, cls)


# ---------------------------------------------------------------------------
# 検証・根拠の収集
# ---------------------------------------------------------------------------


def _reject_user_directive(evidence: tuple[Evidence, ...]) -> None:
    if any(e.root_factor is RootFactor.USER_DIRECTIVE for e in evidence):
        raise ContractViolationError(
            "ユーザー設定の目標(USER_DIRECTIVE)の根拠は Arbiter の入力にできない(通知のみ)"
        )


def _regime_vote(layers: LayerVerdicts) -> tuple[Evidence, ...]:
    """regime(価格由来)の 1 票。状態が確定していて、HEALTHY でないときだけ。"""
    state = layers.regime.state.value
    if state is None or state is RegimeState.HEALTHY:
        return ()
    return (
        Evidence(
            root_factor=RootFactor.PRICE_PATH,
            source=REGIME_VOTE_SOURCE,
            fact_key=f"{REGIME_VOTE_SOURCE}:{state.value}",
            status=EvidenceStatus.TRIGGERED,
            layer=ExitLayer.L3_PRICE_REGIME,
        ),
    )


def collect_evidence(
    layers: LayerVerdicts, proposals: tuple[CandidateProposal, ...] = ()
) -> tuple[Evidence, ...]:
    """全ての根拠を集め、同じ事実(event_id があればそれ、無ければ fact_key)を 1 件にする。

    adapter・trace 用の公開関数(arbitrate 自身は検証にだけ使う: _validate_evidence)。
    入力の並び順に依存しない。ユーザー設定の目標の根拠が混ざっていたら拒否する(M14)。
    """
    items: list[Evidence] = [
        *layers.thesis.evidence,
        *layers.expected_return.evidence,
        *layers.rotation.evidence,
        *_regime_vote(layers),
    ]
    for proposal in proposals:
        items.extend(proposal.evidence)
    _reject_user_directive(tuple(items))
    return dedupe_evidence(items)


def _validate_evidence(layers: LayerVerdicts, proposals: tuple[CandidateProposal, ...]) -> None:
    """入力の検証だけ(戻りは使わない)。ユーザー設定の目標の根拠が混ざっていたら拒否する。"""
    collect_evidence(layers, proposals)


# ---------------------------------------------------------------------------
# FE-1〜FE-3 の適格判定
# ---------------------------------------------------------------------------


def _independent(
    evidence: tuple[Evidence, ...], kind: FullEvidenceKind, layers: tuple[ExitLayer | None, ...]
) -> tuple[Evidence, ...]:
    allowed = ALLOWED_ROOTS_BY_KIND[kind]
    return tuple(
        e
        for e in dedupe_evidence(evidence)
        if e.counts_as_independent and e.root_factor in allowed and e.layer in layers
    )


def _try_full_evidence(
    kind: FullEvidenceKind,
    evidence: tuple[Evidence, ...],
    thesis: ThesisVerdict | None = None,
    expected_return: ExpectedReturnVerdict | None = None,
    rotation: RotationVerdict | None = None,
) -> FullEvidence | None:
    """型(C0)が構築を拒否するものは適格にしない(拒否の理由は握りつぶさず『適格でない』)。"""
    try:
        return FullEvidence(
            kind, evidence, thesis=thesis, expected_return=expected_return, rotation=rotation
        )
    except ContractViolationError:
        return None


def _fe1(layers: LayerVerdicts, policy: ArbiterPolicy) -> FullEvidence | None:
    thesis = layers.thesis
    state = thesis.thesis_state.value
    if state not in (ThesisState.WEAKENING, ThesisState.BROKEN):
        return None
    if thesis.reliability is not ReliabilityClass.RELIABLE:
        return None
    # 総合利回りのように L2 由来の印を持つ根拠は、FE-1 の root に数えない
    independent = _independent(
        thesis.evidence,
        FullEvidenceKind.THESIS_DETERIORATION,
        (None, ExitLayer.L1_THESIS),
    )
    if not independent:
        return None
    roots = distinct_roots(independent)
    primary_roots = {e.root_factor for e in independent if e.primary_source_confirmed}
    if state is ThesisState.BROKEN:
        eligible = bool(primary_roots) if policy.fe1_broken_needs_primary_root else True
    else:
        eligible = (
            len(roots) >= policy.fe1_min_distinct_roots_when_weakening
            or len(primary_roots) >= policy.fe1_min_primary_confirmed_roots
        )
    if not eligible:
        return None
    canonical = dataclasses.replace(thesis, evidence=dedupe_evidence(thesis.evidence))
    return _try_full_evidence(FullEvidenceKind.THESIS_DETERIORATION, independent, thesis=canonical)


def _fe2(layers: LayerVerdicts) -> FullEvidence | None:
    expected = layers.expected_return
    if expected.severely_low.value is not True:
        return None
    if expected.reliability is not ReliabilityClass.RELIABLE:
        return None
    independent = _independent(
        expected.evidence,
        FullEvidenceKind.EXPECTED_RETURN_DETERIORATION,
        (None, ExitLayer.L2_EXPECTED_RETURN),
    )
    if not independent:
        return None
    canonical = dataclasses.replace(expected, evidence=dedupe_evidence(expected.evidence))
    return _try_full_evidence(
        FullEvidenceKind.EXPECTED_RETURN_DETERIORATION, independent, expected_return=canonical
    )


def _fe3(layers: LayerVerdicts) -> FullEvidence | None:
    rotation = layers.rotation
    if rotation.gap_clear.value is not True:
        return None
    independent = _independent(
        rotation.evidence,
        FullEvidenceKind.ROTATION_OPPORTUNITY,
        (None, ExitLayer.L4_OPPORTUNITY_COST),
    )
    if not independent:
        return None
    canonical = dataclasses.replace(rotation, evidence=dedupe_evidence(rotation.evidence))
    return _try_full_evidence(
        FullEvidenceKind.ROTATION_OPPORTUNITY, independent, rotation=canonical
    )


def eligible_full_evidence(
    layers: LayerVerdicts, policy: ArbiterPolicy
) -> tuple[FullEvidence, ...]:
    """適格な FULL の独立根拠(FE-1〜FE-3)。型の検査(C0)に加え、policy の条件を満たすもの。

    FE-2 / FE-3 は L2 / L4 が UNDETERMINED の間は適格にならない(擬似的な期待リターンを作らない)。
    """
    found = (_fe1(layers, policy), _fe2(layers), _fe3(layers))
    return tuple(item for item in found if item is not None)


# ---------------------------------------------------------------------------
# L0・層の確定状態
# ---------------------------------------------------------------------------


def _reliability_by_group(layers: LayerVerdicts) -> dict[str, ReliabilityClass]:
    return {item.group: item.reliability for item in layers.reliability.inputs}


def _all_unusable(layers: LayerVerdicts) -> bool:
    """全ての入力グループが UNUSABLE のときだけ True。入力が空は『上限なし』(False)。

    現行のエンジンに L0 が無いため、L0 の入力が無い(= inputs が空)ことは使えないことを
    意味しない。
    """
    inputs = layers.reliability.inputs
    return bool(inputs) and all(i.reliability is ReliabilityClass.UNUSABLE for i in inputs)


def _layer_determined(layers: LayerVerdicts, layer: ExitLayer) -> bool:
    if layer is ExitLayer.L1_THESIS:
        return layers.thesis.thesis_state.is_determined
    if layer is ExitLayer.L2_EXPECTED_RETURN:
        return layers.expected_return.valuation_exhaustion.is_determined
    if layer is ExitLayer.L3_PRICE_REGIME:
        return layers.regime.state.is_determined
    return layers.rotation.gap_clear.is_determined


def undetermined_layers(layers: LayerVerdicts) -> tuple[ExitLayer, ...]:
    """評価できなかった層(L1〜L4)。空でない間は HOLD_OPTIMAL にならない。

    L2 は composite(severely_low)が UNDETERMINED の間は評価できていないとみなす。
    """
    found: list[ExitLayer] = []
    if not layers.thesis.thesis_state.is_determined:
        found.append(ExitLayer.L1_THESIS)
    if not layers.expected_return.severely_low.is_determined:
        found.append(ExitLayer.L2_EXPECTED_RETURN)
    if not layers.regime.state.is_determined:
        found.append(ExitLayer.L3_PRICE_REGIME)
    if not layers.rotation.gap_clear.is_determined:
        found.append(ExitLayer.L4_OPPORTUNITY_COST)
    return tuple(found)


# ---------------------------------------------------------------------------
# 候補の生成(資格の上限 (a))
# ---------------------------------------------------------------------------


def regime_claimed_strength(state: RegimeState, policy: ArbiterPolicy) -> Strength:
    """regime の状態が主張する候補の強さ(policy.regime_strength。単調・FULL を含まない)。

    adapter が claimed_strength を作るときに使う純粋関数。どの TriggerKind(= 現行のどの経路)で
    候補を出すかは adapter が現行の結果から決める(Arbiter は決めない)。
    """
    return policy.regime_strength[state]


def build_candidates(
    inp: ArbiterInput, full_evidence: tuple[FullEvidence, ...]
) -> tuple[tuple[Candidate, ...], tuple[SuppressedCandidate, ...]]:
    """資格の上限 (a) を適用して候補を作る。作れなかった・上限で下がったものは trace に残す。"""
    layers, policy = inp.layers, inp.policy
    groups = _reliability_by_group(layers)
    candidates: list[Candidate] = []
    trace: list[SuppressedCandidate] = []
    for proposal in inp.proposals:
        claimed = proposal.claimed_strength
        cls = proposal.exit_class
        if not _layer_determined(layers, proposal.layer):
            trace.append(SuppressedCandidate(cls, claimed, SuppressionReason.UNDETERMINED_INPUT))
            continue
        dependent = [groups.get(g, ReliabilityClass.RELIABLE) for g in proposal.input_groups]
        if ReliabilityClass.UNUSABLE in dependent:
            trace.append(SuppressedCandidate(cls, claimed, SuppressionReason.RELIABILITY_CAP))
            continue
        strength = claimed
        if strength is Strength.FULL and not full_evidence:
            strength = Strength.PARTIAL
            trace.append(SuppressedCandidate(cls, claimed, SuppressionReason.NO_FULL_EVIDENCE))
        if ReliabilityClass.DEGRADED in dependent and policy.degraded_downgrade_steps:
            lowered = _down(strength, policy.degraded_downgrade_steps)
            trace.append(SuppressedCandidate(cls, strength, SuppressionReason.RELIABILITY_CAP))
            strength = lowered
        if strength < Strength.WATCH:
            continue
        candidates.append(Candidate(proposal, strength, ORIGIN_OF_TRIGGER[proposal.trigger_kind]))
    return tuple(candidates), tuple(trace)


def select_winner(candidates: tuple[Candidate, ...]) -> Candidate | None:
    """勝者を選ぶ(降格 (b) の前の強さと origin で。入力の並び順に依存しない)。"""
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.sort_key)


# ---------------------------------------------------------------------------
# 勝者にだけ降格 (b)
# ---------------------------------------------------------------------------


def apply_softening(
    winner: Candidate, inp: ArbiterInput
) -> tuple[Strength, tuple[SuppressionReason, ...]]:
    """勝者にだけ降格 (b) を適用する。順序は現行の profit_taking.py と同じ。

    現行コードとの対応(evaluate_profit_taking の raw_level 決定のあと)
      1 緩和要因 = _apply_mitigating_factors(合計の段数だけ下げる。FUNDAMENTAL_CRITICAL_RISK は
        downgrade_disabled)
      2 監視の下限 = 『緩和でHOLDになっても、最低でもWATCH』(raw_level > HOLD のとき)
      3 origin 別の床(1 回目)= PRICE_POSITION / FAIR_VALUE_STRONG / PROFIT_PROTECTION_STRONG で
        raw が PARTIAL 以上なら PARTIAL 未満にしない(緩和直後の fundamental_level に適用)
      4 タイミング層 = 上昇トレンドで 1 段降格(FUNDAMENTAL_CRITICAL_RISK と hard_overvalued は除く)
      5 監視の下限(2 回目)= タイミング層でHOLDに落とさない
      6 origin 別の最終の床(2 回目)= 緩和 + タイミングの合計でも PARTIAL 未満にしない
    決算直前は降格として持たない(現行は E2 の入力側の gate = ceiling の利用可否)。
    """
    if winner.origin in SOFTENING_EXEMPT_ORIGINS:
        return winner.strength, ()
    facts, policy = inp.softening, inp.policy
    raw = winner.strength
    floor = winner.origin in FLOOR_ORIGINS and raw >= Strength.PARTIAL
    reasons: list[SuppressionReason] = []
    strength = raw
    if facts.mitigation_steps:
        strength = _down(strength, facts.mitigation_steps)
        reasons.append(SuppressionReason.MITIGATION)
    strength = max(strength, Strength.WATCH)
    if floor:
        strength = max(strength, Strength.PARTIAL)
    if facts.uptrend and not facts.hard_overvalued and policy.timing_downgrade_steps:
        strength = _down(strength, policy.timing_downgrade_steps)
        reasons.append(SuppressionReason.MITIGATION)
    strength = max(strength, Strength.WATCH)
    if floor:
        strength = max(strength, Strength.PARTIAL)
    return strength, tuple(reasons)


# ---------------------------------------------------------------------------
# review(L1 の悪化は売却の推奨にしない)
# ---------------------------------------------------------------------------


def _review(layers: LayerVerdicts) -> tuple[ReviewFlag, ExitClass | None]:
    state = layers.thesis.thesis_state.value
    if state is ThesisState.BROKEN:
        flag = (
            ReviewFlag.URGENT_REVIEW
            if layers.thesis.hard_gate_triggered
            else ReviewFlag.MANUAL_REVIEW
        )
        return flag, ExitClass.RISK_EXIT
    if state is ThesisState.WEAKENING:
        return ReviewFlag.MANUAL_REVIEW, ExitClass.RISK_EXIT
    return ReviewFlag.NONE, None


# ---------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------


def arbitrate(inp: ArbiterInput) -> Determination[Decision]:
    """最終の action を決める純粋関数。UNDETERMINED = UNDECIDABLE(L0 が全体として使えない)。

    同じ入力から同じ結果。入力(根拠・候補)の並び順に依存しない。時計・乱数・I/O なし。
    """
    layers, policy = inp.layers, inp.policy
    if _all_unusable(layers):
        return Determination.undetermined(
            UndeterminedReason.RELIABILITY_UNUSABLE, "L0 が全体として使えないため決められない"
        )
    # 検証だけ(ユーザー目標の根拠を拒否)。重複排除の結果は使わない: FE の適格判定は層ごとの
    # 根拠(層の verdict の evidence)から行い、強さは adapter が主張した値を受ける
    _validate_evidence(layers, inp.proposals)
    full_evidence = eligible_full_evidence(layers, policy)
    candidates, trace = build_candidates(inp, full_evidence)
    winner = select_winner(candidates)
    review_flag, review_class = _review(layers)
    unknown_layers = undetermined_layers(layers)
    suppressed: list[SuppressedCandidate] = list(trace)

    if winner is None:
        return Determination.of(
            Decision(
                action=ExitAction.HOLD,
                exit_class=ExitClass.NONE,
                strength=Strength.NONE,
                suppressed=_ordered(suppressed),
                review_flag=review_flag,
                hold_optimal=not unknown_layers and review_flag is ReviewFlag.NONE,
                review_class=review_class,
                undetermined_layers=unknown_layers,
            )
        )

    pre = winner.strength
    for other in candidates:
        if other is winner:
            continue
        suppressed.append(
            SuppressedCandidate(
                other.proposal.exit_class, other.strength, SuppressionReason.SUPERSEDED_BY_STRONGER
            )
        )
    final, reasons = apply_softening(winner, inp)
    for reason in dict.fromkeys(reasons):
        if final < pre:
            suppressed.append(SuppressedCandidate(winner.proposal.exit_class, pre, reason))

    supporting = tuple(
        kind
        for kind in TRIGGER_TIE_ORDER
        if kind is not winner.proposal.trigger_kind
        and any(c.proposal.trigger_kind is kind and c.strength == pre for c in candidates)
    )
    if final >= Strength.PARTIAL:
        action = ExitAction.FULL if final is Strength.FULL else ExitAction.PARTIAL
        return Determination.of(
            Decision(
                action=action,
                exit_class=winner.proposal.exit_class,
                strength=final,
                primary_layer=winner.proposal.layer,
                full_evidence=full_evidence if action is ExitAction.FULL else (),
                suppressed=_ordered(suppressed),
                review_flag=review_flag,
                review_class=review_class,
                undetermined_layers=unknown_layers,
                trigger_kind=winner.proposal.trigger_kind,
                supporting_triggers=supporting,
            )
        )
    return Determination.of(
        Decision(
            action=ExitAction.HOLD,
            exit_class=ExitClass.NONE,
            strength=min(final, Strength.WATCH),
            suppressed=_ordered(suppressed),
            review_flag=review_flag,
            hold_optimal=False,
            review_class=review_class,
            undetermined_layers=unknown_layers,
        )
    )


def _ordered(items: list[SuppressedCandidate]) -> tuple[SuppressedCandidate, ...]:
    """trace を入力の並び順に依存しない順序にそろえる(重複は 1 件)。"""
    unique = dict.fromkeys(items)
    return tuple(
        sorted(
            unique,
            key=lambda s: (
                _CLASS_ORDER.index(s.exit_class)
                if s.exit_class in _CLASS_ORDER
                else len(_CLASS_ORDER),
                -int(s.strength),
                s.reason.value,
            ),
        )
    )


__all__ = [
    "ALLOWED_CLASSES_BY_TRIGGER",
    "FLOOR_ORIGINS",
    "ORIGIN_OF_TRIGGER",
    "SOFTENING_EXEMPT_ORIGINS",
    "TRIGGER_TIE_ORDER",
    "ArbiterInput",
    "ArbiterPolicy",
    "Candidate",
    "CandidateProposal",
    "Origin",
    "SofteningFacts",
    "apply_softening",
    "arbitrate",
    "build_candidates",
    "collect_evidence",
    "eligible_full_evidence",
    "regime_claimed_strength",
    "select_winner",
    "undetermined_layers",
]
