"""Arbiter(Issue #878 PR-2。dormant)の契約テスト。

## 何を固定するか(#878 の受入条件 AC-1・AC-2 と、設計 rev1〜rev5 の M1〜M23)

    M1  冗長な入力の追加で強さが増えない / M2 異なる root の根拠を足すと FULL の適格は単調に増える
    M3  価格由来(PRICE_PATH)だけでは FULL にならない
    M7  valuation 枯渇『単独』では FULL にならない
    M8  UNDETERMINED は根拠にも否定にも数えない
    M9  適正価格の強い条件(valuation + FE-1)の FULL は保存
    M10 L1 の候補は売却の推奨にならず、review_flag + review_class を持つ(UJ-15)
    M11 推定(SUSPECTED)の根拠は FE-1 の独立根拠に数えない
    M12 同じ事実(event_id / fact_key)は 1 件 / M13 regime は単調で FULL を含まない
    M14 ユーザー目標(USER_DIRECTIVE)は入力にできない / M15 L0 の同じ原因は trace に 1 件
    M16 UNDECIDABLE と HOLD の区別・L2 が UNDETERMINED の間は hold_optimal にならない
    M17 入力の並び順に依存しない / M18 純粋性 / M19 VALUATION_LEVEL を足しても強さが増えない
    M20 fact_key を解析しない / M21 下限の保証(PX-1 を含む)を保存 / M23 勝敗の規則と順序

## 閾値について

ここで使う policy の数値は、**このテストの中だけの例示値**であり、運用の閾値ではない。
module は既定値を持たない(事前登録 -> 並行計算 -> 過去データで検証 -> USER の個別承認)。

## 何を固定しないか

adapter(E1 / E2 / E3 -> LayerVerdicts。PR-3)・早期 return の置換(PR-4)・通知の ratchet(#879)・
売却数量(#881)。
"""

from __future__ import annotations

import ast
import dataclasses
import itertools
from collections.abc import Iterable
from pathlib import Path

import pytest

from jstock_advisor.domain.exit_architecture import arbiter
from jstock_advisor.domain.exit_architecture.arbiter import (
    _CLASS_ORDER,
    ALLOWED_CLASSES_BY_TRIGGER,
    FLOOR_ORIGINS,
    MAX_STRENGTH_BY_TRIGGER,
    ORIGIN_OF_TRIGGER,
    SOFTENING_EXEMPT_ORIGINS,
    TRIGGER_TIE_ORDER,
    ArbiterInput,
    ArbiterPolicy,
    CandidateProposal,
    Origin,
    SofteningFacts,
    arbitrate,
    collect_evidence,
    eligible_full_evidence,
    regime_claimed_strength,
    undetermined_layers,
)
from jstock_advisor.domain.exit_architecture.decision import (
    ContractViolationError,
    Decision,
)
from jstock_advisor.domain.exit_architecture.determination import Determination
from jstock_advisor.domain.exit_architecture.evidence import (
    Evidence,
    EvidenceStatus,
    dedupe_by_fact_key,
    dedupe_evidence,
    dedupe_key,
    distinct_roots,
)
from jstock_advisor.domain.exit_architecture.verdicts import (
    ComponentValue,
    ExpectedReturnComponent,
    ExpectedReturnVerdict,
    InputReliability,
    LayerVerdicts,
    PortfolioContext,
    RegimeVerdict,
    ReliabilityVerdict,
    RotationVerdict,
    ThesisVerdict,
)
from jstock_advisor.domain.exit_architecture.vocabulary import (
    ExitAction,
    ExitClass,
    ExitLayer,
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
from jstock_advisor.domain.signals.profit_taking import _RawLevelOrigin

_SRC = Path(arbiter.__file__)

#: このテストの中だけの例示値(運用の閾値ではない)
_REGIME = {
    RegimeState.HEALTHY: Strength.NONE,
    RegimeState.PEAK_WARNING: Strength.WATCH,
    RegimeState.DOWNTREND_CONFIRMED: Strength.PARTIAL,
    RegimeState.BREAKDOWN: Strength.PARTIAL,
}


def _policy(**over: object) -> ArbiterPolicy:
    base: dict[str, object] = {
        "fe1_min_distinct_roots_when_weakening": 2,
        "fe1_min_primary_confirmed_roots": 1,
        "fe1_broken_needs_primary_root": False,
        "degraded_downgrade_steps": 1,
        "timing_downgrade_steps": 1,
        "regime_strength": dict(_REGIME),
    }
    base.update(over)
    return ArbiterPolicy(**base)  # type: ignore[arg-type]


def _ev(
    root: RootFactor,
    key: str = "k",
    *,
    status: EvidenceStatus = EvidenceStatus.TRIGGERED,
    primary: bool = False,
    event: str | None = None,
    layer: ExitLayer | None = None,
    source: str = "s",
) -> Evidence:
    return Evidence(root, source, f"{root.value}:{key}", status, primary, event, layer)


_UNKNOWN = UndeterminedReason.COMPONENT_NOT_IMPLEMENTED


def _thesis(
    state: ThesisState | None = ThesisState.INTACT,
    evidence: tuple[Evidence, ...] = (),
    *,
    reliability: ReliabilityClass = ReliabilityClass.RELIABLE,
    gate: bool = False,
) -> ThesisVerdict:
    return ThesisVerdict(
        thesis_state=(
            Determination.undetermined(UndeterminedReason.COVERAGE_INSUFFICIENT)
            if state is None
            else Determination.of(state)
        ),
        reliability=reliability,
        evidence=evidence,
        hard_gate_triggered=gate,
        hard_gate_reasons=("DEBT_EXCESS",) if gate else (),
    )


def _er(
    *,
    exhaustion: bool | None = False,
    low: bool | None = None,
    evidence: tuple[Evidence, ...] = (),
    reliability: ReliabilityClass = ReliabilityClass.RELIABLE,
) -> ExpectedReturnVerdict:
    """low が None のとき severely_low は UNDETERMINED(#601 / #602 が未実装の現状)。"""
    components: tuple[ComponentValue, ...] = ()
    if low is not None:
        components = tuple(
            ComponentValue(c, Determination.of(1.0)) for c in ExpectedReturnComponent
        )
    return ExpectedReturnVerdict(
        components=components,
        valuation_exhaustion=(
            Determination.undetermined(_UNKNOWN)
            if exhaustion is None
            else Determination.of(exhaustion)
        ),
        severely_low=(
            Determination.undetermined(_UNKNOWN) if low is None else Determination.of(low)
        ),
        reliability=reliability,
        evidence=evidence,
    )


def _regime(state: RegimeState | None = RegimeState.HEALTHY) -> RegimeVerdict:
    unknown: Determination[float] = Determination.undetermined(_UNKNOWN)
    return RegimeVerdict(
        state=(
            Determination.undetermined(UndeterminedReason.NOT_EVALUATED)
            if state is None
            else Determination.of(state)
        ),
        previous_state=Determination.undetermined(UndeterminedReason.NOT_EVALUATED),
        current_gain_pct=unknown,
        peak_gain_pct=unknown,
    )


def _rotation(gap: bool | None = None, evidence: tuple[Evidence, ...] = ()) -> RotationVerdict:
    return RotationVerdict(
        gap_clear=Determination.undetermined(_UNKNOWN) if gap is None else Determination.of(gap),
        evidence=evidence,
    )


def _layers(
    *,
    thesis: ThesisVerdict | None = None,
    er: ExpectedReturnVerdict | None = None,
    regime: RegimeVerdict | None = None,
    rotation: RotationVerdict | None = None,
    groups: tuple[tuple[str, ReliabilityClass], ...] = (),
    earnings_near: bool = False,
) -> LayerVerdicts:
    return LayerVerdicts(
        reliability=ReliabilityVerdict(inputs=tuple(InputReliability(g, r) for g, r in groups)),
        thesis=thesis or _thesis(),
        expected_return=er or _er(),
        regime=regime or _regime(),
        rotation=rotation or _rotation(),
        context=PortfolioContext(
            concentrated=False, trading_unit_feasible=True, earnings_window_near=earnings_near
        ),
    )


def _prop(
    trigger: TriggerKind = TriggerKind.PRICE_UPSIDE_MATRIX,
    cls: ExitClass = ExitClass.VALUE_EXIT,
    strength: Strength = Strength.PARTIAL,
    layer: ExitLayer = ExitLayer.L2_EXPECTED_RETURN,
    evidence: tuple[Evidence, ...] = (),
    groups: Iterable[str] = (),
) -> CandidateProposal:
    return CandidateProposal(trigger, cls, strength, layer, evidence, frozenset(groups))


def _soft(*, steps: int = 0, uptrend: bool = False, hard: bool = False) -> SofteningFacts:
    return SofteningFacts(mitigation_steps=steps, uptrend=uptrend, hard_overvalued=hard)


def _inp(
    proposals: Iterable[CandidateProposal] = (),
    *,
    layers: LayerVerdicts | None = None,
    softening: SofteningFacts | None = None,
    policy: ArbiterPolicy | None = None,
) -> ArbiterInput:
    return ArbiterInput(
        layers or _layers(),
        tuple(proposals),
        softening or _soft(),
        policy or _policy(),
    )


def _decide(inp: ArbiterInput) -> Decision:
    return arbitrate(inp).unwrap()


#: FE-1 が適格になる L1(WEAKENING + 非価格の異なる root が 2 つ。例示の policy で)
def _fe1_layers(**kw: object) -> LayerVerdicts:
    thesis = _thesis(
        ThesisState.WEAKENING,
        (_ev(RootFactor.EARNINGS, "a"), _ev(RootFactor.CASHFLOW, "b")),
    )
    return _layers(thesis=thesis, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# (1) 語彙・表・policy の固定
# ---------------------------------------------------------------------------


def test_origin_priority_is_the_same_as_the_current_profit_taking_origin() -> None:
    """勝敗の規則の基礎: origin の優先順位は現行の _RawLevelOrigin と同じ(名前と値で一致)。"""
    for origin in Origin:
        assert int(origin) == int(_RawLevelOrigin[origin.name]), origin.name
    assert [o.name for o in sorted(Origin)] == [
        "OTHER_CONDITIONS",
        "PRICE_POSITION",
        "PROFIT_PROTECTION_STRONG",
        "FAIR_VALUE_STRONG",
        "FUNDAMENTAL_CRITICAL_RISK",
    ]


def test_trigger_to_origin_table_is_fixed_and_excludes_the_user_targets() -> None:
    assert ORIGIN_OF_TRIGGER == {
        TriggerKind.PARTIAL_CONDITIONS: Origin.OTHER_CONDITIONS,
        TriggerKind.FULL_MODERATE_CONDITIONS: Origin.OTHER_CONDITIONS,
        TriggerKind.PRICE_UPSIDE_MATRIX: Origin.PRICE_POSITION,
        TriggerKind.PROFIT_PROTECTION_STRONG: Origin.PROFIT_PROTECTION_STRONG,
        TriggerKind.FAIR_VALUE_STRONG: Origin.FAIR_VALUE_STRONG,
        TriggerKind.FAIR_VALUE_PARTIAL_GATE: Origin.FAIR_VALUE_STRONG,
        TriggerKind.FULL_STRONG_CRITICAL: Origin.FUNDAMENTAL_CRITICAL_RISK,
    }
    assert TriggerKind.USER_TARGET_PRICE not in ORIGIN_OF_TRIGGER
    assert TriggerKind.USER_TARGET_RATE not in ORIGIN_OF_TRIGGER
    assert set(ALLOWED_CLASSES_BY_TRIGGER) == set(ORIGIN_OF_TRIGGER)


def test_tie_order_is_a_fixed_table_one_to_one_with_the_arbiter_input_kinds() -> None:
    """同点の勝った TriggerKind の順序を表で固定する(C0 の語彙の定義順に依存させない)。"""
    assert TRIGGER_TIE_ORDER == (
        TriggerKind.FULL_STRONG_CRITICAL,
        TriggerKind.FAIR_VALUE_STRONG,
        TriggerKind.FAIR_VALUE_PARTIAL_GATE,
        TriggerKind.PROFIT_PROTECTION_STRONG,
        TriggerKind.PRICE_UPSIDE_MATRIX,
        TriggerKind.FULL_MODERATE_CONDITIONS,
        TriggerKind.PARTIAL_CONDITIONS,
    )
    assert set(TRIGGER_TIE_ORDER) == set(ORIGIN_OF_TRIGGER)
    # origin の優先順位が高い順をそのまま下敷きにする(同じ origin の中の順序だけを明示)
    origins = [ORIGIN_OF_TRIGGER[kind] for kind in TRIGGER_TIE_ORDER]
    assert origins == sorted(origins, reverse=True)


def test_floor_and_softening_exemptions_are_the_current_ones() -> None:
    assert {
        Origin.PRICE_POSITION,
        Origin.FAIR_VALUE_STRONG,
        Origin.PROFIT_PROTECTION_STRONG,
    } == FLOOR_ORIGINS
    assert {Origin.FUNDAMENTAL_CRITICAL_RISK} == SOFTENING_EXEMPT_ORIGINS


def test_policy_has_no_default_values() -> None:
    for field in dataclasses.fields(ArbiterPolicy):
        assert field.default is dataclasses.MISSING, field.name
        assert field.default_factory is dataclasses.MISSING, field.name


@pytest.mark.parametrize(
    "field", ["fe1_min_distinct_roots_when_weakening", "fe1_min_primary_confirmed_roots"]
)
@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_policy_rejects_a_non_positive_or_non_integer_root_count(field: str, bad: object) -> None:
    with pytest.raises(ValueError):
        _policy(**{field: bad})


@pytest.mark.parametrize(
    "field",
    ["degraded_downgrade_steps", "timing_downgrade_steps"],
)
@pytest.mark.parametrize("bad", [-1, True, 0.5])
def test_policy_rejects_a_negative_or_non_integer_step_count(field: str, bad: object) -> None:
    with pytest.raises(ValueError):
        _policy(**{field: bad})


def test_policy_rejects_a_non_boolean_broken_flag() -> None:
    with pytest.raises(TypeError):
        _policy(fe1_broken_needs_primary_root=1)


def test_regime_strength_must_be_complete_monotone_and_never_full() -> None:
    missing = dict(_REGIME)
    del missing[RegimeState.BREAKDOWN]
    with pytest.raises(ValueError):
        _policy(regime_strength=missing)
    lighter_when_heavier = dict(_REGIME)
    lighter_when_heavier[RegimeState.BREAKDOWN] = Strength.WATCH
    with pytest.raises(ValueError):
        _policy(regime_strength=lighter_when_heavier)
    with_full = dict(_REGIME)
    with_full[RegimeState.BREAKDOWN] = Strength.FULL
    with pytest.raises(ValueError):
        _policy(regime_strength=with_full)
    unhealthy_healthy = dict(_REGIME)
    unhealthy_healthy[RegimeState.HEALTHY] = Strength.WATCH
    with pytest.raises(ValueError):
        _policy(regime_strength=unhealthy_healthy)


def test_softening_facts_reject_a_negative_or_non_integer_step_count() -> None:
    with pytest.raises(ValueError):
        SofteningFacts(-1, False, False)
    with pytest.raises(TypeError):
        SofteningFacts(True, False, False)


# ---------------------------------------------------------------------------
# (2) 入力の検証: ユーザー目標は通知のみ(M14)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [TriggerKind.USER_TARGET_PRICE, TriggerKind.USER_TARGET_RATE])
def test_user_target_triggers_cannot_be_arbiter_inputs(kind: TriggerKind) -> None:
    with pytest.raises(ContractViolationError):
        CandidateProposal(kind, ExitClass.VALUE_EXIT, Strength.FULL, ExitLayer.L2_EXPECTED_RETURN)


def test_a_user_directive_evidence_is_rejected_wherever_it_comes_from() -> None:
    user = _ev(RootFactor.USER_DIRECTIVE, "target")
    with pytest.raises(ContractViolationError):
        _prop(evidence=(user,))
    with pytest.raises(ContractViolationError):
        collect_evidence(_layers(thesis=_thesis(ThesisState.WEAKENING, (user,))))
    with pytest.raises(ContractViolationError):
        _decide(_inp(layers=_layers(er=_er(evidence=(user,)))))
    with pytest.raises(ContractViolationError):
        _decide(_inp(layers=_layers(rotation=_rotation(False, (user,)))))


def test_a_proposal_class_must_match_its_trigger_kind() -> None:
    with pytest.raises(ContractViolationError):
        _prop(TriggerKind.PRICE_UPSIDE_MATRIX, ExitClass.PROFIT_PROTECTION)
    with pytest.raises(ContractViolationError):
        _prop(TriggerKind.FULL_STRONG_CRITICAL, ExitClass.VALUE_EXIT)


def test_a_proposal_needs_at_least_a_watch_strength_and_never_comes_from_l0() -> None:
    with pytest.raises(ContractViolationError):
        _prop(strength=Strength.NONE)
    with pytest.raises(ContractViolationError):
        _prop(layer=ExitLayer.L0_DATA_RELIABILITY)


def test_only_user_target_free_inputs_can_win() -> None:
    """目標到達だけの入力(= 候補なし)は HOLD。売却の推奨にならない。"""
    decision = _decide(_inp())
    assert decision.action is ExitAction.HOLD


# ---------------------------------------------------------------------------
# (3) L0: UNDECIDABLE と HOLD の区別(M16)
# ---------------------------------------------------------------------------


def test_every_input_group_unusable_is_undecidable_not_hold() -> None:
    result = arbitrate(
        _inp(
            layers=_layers(
                groups=(("a", ReliabilityClass.UNUSABLE), ("b", ReliabilityClass.UNUSABLE))
            )
        )
    )
    assert not result.is_determined
    assert result.reason is UndeterminedReason.RELIABILITY_UNUSABLE
    with pytest.raises(TypeError):
        bool(result)


@pytest.mark.parametrize(
    "groups",
    [
        (),
        (("a", ReliabilityClass.UNUSABLE), ("b", ReliabilityClass.DEGRADED)),
        (("a", ReliabilityClass.UNUSABLE), ("b", ReliabilityClass.RELIABLE)),
    ],
)
def test_it_is_decidable_unless_every_group_is_unusable(
    groups: tuple[tuple[str, ReliabilityClass], ...],
) -> None:
    assert arbitrate(_inp(layers=_layers(groups=groups))).is_determined


# ---------------------------------------------------------------------------
# (4) FE-1〜FE-3 の適格判定(型 + policy)
# ---------------------------------------------------------------------------


def _kinds(layers: LayerVerdicts, policy: ArbiterPolicy | None = None) -> set[str]:
    return {fe.kind.value for fe in eligible_full_evidence(layers, policy or _policy())}


def test_fe1_needs_enough_distinct_non_price_roots_when_weakening() -> None:
    assert _kinds(_fe1_layers()) == {"THESIS_DETERIORATION"}
    one_root = _layers(
        thesis=_thesis(ThesisState.WEAKENING, (_ev(RootFactor.EARNINGS, "a"),)),
    )
    assert _kinds(one_root) == set()


def test_fe1_is_also_eligible_with_primary_confirmed_roots() -> None:
    confirmed = _layers(
        thesis=_thesis(
            ThesisState.WEAKENING, (_ev(RootFactor.RETURN_POLICY, "cut", primary=True),)
        ),
    )
    assert _kinds(confirmed) == {"THESIS_DETERIORATION"}
    stricter = _policy(fe1_min_primary_confirmed_roots=2)
    assert _kinds(confirmed, stricter) == set()


def test_the_same_root_counts_once_for_fe1() -> None:
    same = _layers(
        thesis=_thesis(
            ThesisState.WEAKENING,
            (_ev(RootFactor.EARNINGS, "a"), _ev(RootFactor.EARNINGS, "b")),
        ),
    )
    assert _kinds(same) == set()


def test_fe1_does_not_count_suspected_evidence() -> None:
    """M11: 推定のみの根拠は FE-1 の独立根拠に単独で数えられない。"""
    suspected = _layers(
        thesis=_thesis(
            ThesisState.WEAKENING,
            (
                _ev(RootFactor.EARNINGS, "a", status=EvidenceStatus.SUSPECTED),
                _ev(RootFactor.CASHFLOW, "b", status=EvidenceStatus.SUSPECTED),
            ),
        ),
    )
    assert _kinds(suspected) == set()


def test_the_full_evidence_keeps_only_the_independent_evidence() -> None:
    """推定(SUSPECTED)の根拠は、適格の判定にも FullEvidence の中身にも入らない(M11)。"""
    layers = _layers(
        thesis=_thesis(
            ThesisState.WEAKENING,
            (
                _ev(RootFactor.EARNINGS, "a"),
                _ev(RootFactor.CASHFLOW, "b"),
                _ev(RootFactor.BALANCE_SHEET, "c", status=EvidenceStatus.SUSPECTED),
            ),
        )
    )
    (fe,) = eligible_full_evidence(layers, _policy())
    assert {e.fact_key for e in fe.evidence} == {"EARNINGS:a", "CASHFLOW:b"}
    assert all(e.counts_as_independent for e in fe.evidence)


def test_fe1_does_not_count_evidence_marked_as_another_layer() -> None:
    """総合利回りのように L2 由来の印を持つ根拠は、FE-1 の root に数えない(P-1)。"""
    mixed = _layers(
        thesis=_thesis(
            ThesisState.WEAKENING,
            (
                _ev(RootFactor.EARNINGS, "a"),
                _ev(RootFactor.RETURN_POLICY, "yield", layer=ExitLayer.L2_EXPECTED_RETURN),
            ),
        ),
    )
    assert _kinds(mixed) == set()


@pytest.mark.parametrize("state", [ThesisState.INTACT, None])
def test_fe1_needs_a_weakening_or_broken_thesis(state: ThesisState | None) -> None:
    layers = _layers(
        thesis=_thesis(state, (_ev(RootFactor.EARNINGS, "a"), _ev(RootFactor.CASHFLOW, "b")))
    )
    assert _kinds(layers) == set()


def test_fe1_needs_a_reliable_thesis() -> None:
    layers = _layers(
        thesis=_thesis(
            ThesisState.WEAKENING,
            (_ev(RootFactor.EARNINGS, "a"), _ev(RootFactor.CASHFLOW, "b")),
            reliability=ReliabilityClass.DEGRADED,
        )
    )
    assert _kinds(layers) == set()


def test_broken_fe1_needs_independent_evidence_and_optionally_a_primary_root() -> None:
    gate_only = _layers(thesis=_thesis(ThesisState.BROKEN, (), gate=True))
    assert _kinds(gate_only) == set()
    unconfirmed = _layers(
        thesis=_thesis(
            ThesisState.BROKEN, (_ev(RootFactor.GOVERNANCE_EVENT, "scandal"),), gate=True
        )
    )
    assert _kinds(unconfirmed) == {"THESIS_DETERIORATION"}
    strict = _policy(fe1_broken_needs_primary_root=True)
    assert _kinds(unconfirmed, strict) == set()
    confirmed = _layers(
        thesis=_thesis(
            ThesisState.BROKEN,
            (_ev(RootFactor.BALANCE_SHEET, "insolvency", primary=True),),
            gate=True,
        )
    )
    assert _kinds(confirmed, strict) == {"THESIS_DETERIORATION"}


def test_fe2_and_fe3_are_not_eligible_while_l2_and_l4_are_undetermined() -> None:
    """#601 / #602 / #604 が未実装の現状: 個別の値をいくつ与えても FE-2 / FE-3 は成立しない。"""
    layers = _layers(
        er=_er(
            exhaustion=True,
            low=None,
            evidence=(_ev(RootFactor.VALUATION_LEVEL, "v"), _ev(RootFactor.EARNINGS, "e")),
        ),
        rotation=_rotation(None, (_ev(RootFactor.OPPORTUNITY_COST, "gap"),)),
    )
    assert _kinds(layers) == set()


def test_fe2_is_eligible_only_with_a_determined_low_other_roots_and_reliability() -> None:
    evidence = (_ev(RootFactor.VALUATION_LEVEL, "v"), _ev(RootFactor.EARNINGS, "e"))
    assert _kinds(_layers(er=_er(exhaustion=True, low=True, evidence=evidence))) == {
        "EXPECTED_RETURN_DETERIORATION"
    }
    assert _kinds(_layers(er=_er(exhaustion=True, low=False, evidence=evidence))) == set()
    valuation_only = (_ev(RootFactor.VALUATION_LEVEL, "v"),)
    assert _kinds(_layers(er=_er(exhaustion=True, low=True, evidence=valuation_only))) == set()
    degraded = _er(
        exhaustion=True, low=True, evidence=evidence, reliability=ReliabilityClass.DEGRADED
    )
    assert _kinds(_layers(er=degraded)) == set()


def test_fe3_is_eligible_only_with_a_determined_true_gap_and_its_root() -> None:
    gap = (_ev(RootFactor.OPPORTUNITY_COST, "gap"),)
    assert _kinds(_layers(rotation=_rotation(True, gap))) == {"ROTATION_OPPORTUNITY"}
    assert _kinds(_layers(rotation=_rotation(False, gap))) == set()
    assert _kinds(_layers(rotation=_rotation(True, ()))) == set()


# ---------------------------------------------------------------------------
# (5) 資格の上限 (a): FULL は適格な FE があるときだけ(M3・M7・M9・M19)
# ---------------------------------------------------------------------------


def _full_claim(trigger: TriggerKind = TriggerKind.PRICE_UPSIDE_MATRIX) -> CandidateProposal:
    return _prop(trigger, ExitClass.VALUE_EXIT, Strength.FULL)


def test_a_full_claim_without_an_eligible_fe_is_capped_at_partial() -> None:
    """PX-2(含み益と上値余地だけの FULL)は PARTIAL 止まり。意図した差(UJ-4)。"""
    decision = _decide(_inp([_full_claim()]))
    assert decision.action is ExitAction.PARTIAL
    assert decision.trigger_kind is TriggerKind.PRICE_UPSIDE_MATRIX
    assert SuppressionReason.NO_FULL_EVIDENCE in {s.reason for s in decision.suppressed}


def test_a_full_claim_with_an_eligible_fe_is_preserved() -> None:
    """ケース 3: valuation 枯渇 + 投資前提の悪化(FE-1)の FULL は保存される(M9)。"""
    decision = _decide(_inp([_full_claim(TriggerKind.FAIR_VALUE_STRONG)], layers=_fe1_layers()))
    assert decision.action is ExitAction.FULL
    assert decision.full_evidence
    assert decision.independent_roots == {RootFactor.EARNINGS, RootFactor.CASHFLOW}
    assert decision.trigger_kind is TriggerKind.FAIR_VALUE_STRONG


def test_valuation_exhaustion_alone_never_gives_full_whatever_is_added() -> None:
    """M7 / M19: VALUATION_LEVEL の根拠をいくつ足しても、FULL の claim は FULL にならない。"""
    many = tuple(_ev(RootFactor.VALUATION_LEVEL, str(i)) for i in range(5))
    layers = _layers(er=_er(exhaustion=True, evidence=many))
    assert _decide(_inp([_full_claim()], layers=layers)).action is ExitAction.PARTIAL
    fewer = _layers(er=_er(exhaustion=True))
    assert (
        _decide(_inp([_full_claim()], layers=fewer)).strength
        == _decide(_inp([_full_claim()], layers=layers)).strength
    )


def test_removing_every_non_valuation_independent_root_removes_full() -> None:
    """M7(metamorphic): FULL を返す入力から、独立根拠(非価格の root)を全て取り除くと、
    FULL を返さない。"""
    with_fe = _inp([_full_claim()], layers=_fe1_layers())
    assert _decide(with_fe).action is ExitAction.FULL
    without = _inp([_full_claim()], layers=_layers(thesis=_thesis(ThesisState.WEAKENING)))
    assert _decide(without).action is not ExitAction.FULL


def test_price_derived_evidence_alone_never_gives_full() -> None:
    """M3: 価格由来(PRICE_PATH)のみ。regime の票も FULL の独立根拠にならない。"""
    price = _prop(
        TriggerKind.PRICE_UPSIDE_MATRIX,
        ExitClass.VALUE_EXIT,
        Strength.FULL,
        ExitLayer.L3_PRICE_REGIME,
        (_ev(RootFactor.PRICE_PATH, "drawdown"),),
    )
    layers = _layers(regime=_regime(RegimeState.BREAKDOWN))
    decision = _decide(_inp([price], layers=layers))
    assert decision.action is ExitAction.PARTIAL


def test_profit_protection_stays_partial_even_with_an_eligible_non_price_fe() -> None:
    """現行の利益保全 strong は PARTIAL までしか成立しない。FE-1 が適格でも FULL にならない。"""
    claim = _prop(
        TriggerKind.PROFIT_PROTECTION_STRONG,
        ExitClass.PROFIT_PROTECTION,
        Strength.PARTIAL,
        ExitLayer.L3_PRICE_REGIME,
    )
    fe_layers = dataclasses.replace(_fe1_layers(), regime=_regime(RegimeState.BREAKDOWN))
    assert _decide(_inp([claim], layers=fe_layers)).action is ExitAction.PARTIAL


def test_fe2_through_arbitrate_needs_a_determined_composite() -> None:
    """個別の値(上値余地・利回りなど)から擬似的な期待リターンを作って FE-2 を成立させない。"""
    evidence = (_ev(RootFactor.VALUATION_LEVEL, "v"), _ev(RootFactor.EARNINGS, "e"))
    pseudo = _layers(er=_er(exhaustion=True, low=None, evidence=evidence))
    assert _decide(_inp([_full_claim()], layers=pseudo)).action is ExitAction.PARTIAL
    real = _layers(er=_er(exhaustion=True, low=True, evidence=evidence))
    assert _decide(_inp([_full_claim()], layers=real)).action is ExitAction.FULL


def test_an_undetermined_fe_cannot_build_a_full_even_for_a_full_claim() -> None:
    result = _decide(_inp([_full_claim()], layers=_layers(rotation=_rotation(None))))
    assert result.action is ExitAction.PARTIAL


# ---------------------------------------------------------------------------
# (6) UNDETERMINED は根拠にも否定にも数えない(M8)・trace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("layer", "layers"),
    [
        (ExitLayer.L1_THESIS, _layers(thesis=_thesis(None))),
        (ExitLayer.L2_EXPECTED_RETURN, _layers(er=_er(exhaustion=None))),
        (ExitLayer.L3_PRICE_REGIME, _layers(regime=_regime(None))),
        (ExitLayer.L4_OPPORTUNITY_COST, _layers(rotation=_rotation(None))),
    ],
)
def test_a_candidate_from_an_undetermined_layer_is_dropped_with_a_reason(
    layer: ExitLayer, layers: LayerVerdicts
) -> None:
    proposal = _prop(layer=layer)
    decision = _decide(_inp([proposal], layers=layers))
    assert decision.action is ExitAction.HOLD
    assert SuppressionReason.UNDETERMINED_INPUT in {s.reason for s in decision.suppressed}
    assert layer in decision.undetermined_layers or layer is ExitLayer.L2_EXPECTED_RETURN


def test_an_undetermined_layer_neither_supports_nor_denies_the_other_candidates() -> None:
    base = _decide(_inp([_prop()]))
    unknown = _decide(_inp([_prop()], layers=_layers(rotation=_rotation(None))))
    assert (base.action, base.strength, base.trigger_kind) == (
        unknown.action,
        unknown.strength,
        unknown.trigger_kind,
    )


def test_undetermined_layers_lists_what_could_not_be_evaluated() -> None:
    assert undetermined_layers(_layers()) == (
        ExitLayer.L2_EXPECTED_RETURN,
        ExitLayer.L4_OPPORTUNITY_COST,
    )
    everything = _layers(
        thesis=_thesis(None),
        er=_er(exhaustion=None),
        regime=_regime(None),
        rotation=_rotation(None),
    )
    assert undetermined_layers(everything) == (
        ExitLayer.L1_THESIS,
        ExitLayer.L2_EXPECTED_RETURN,
        ExitLayer.L3_PRICE_REGIME,
        ExitLayer.L4_OPPORTUNITY_COST,
    )


def test_hold_is_not_optimal_while_a_layer_is_undetermined() -> None:
    """M16: 現状は L2 の composite が常に UNDETERMINED = hold_optimal の HOLD は成立しない。"""
    decision = _decide(_inp())
    assert decision.action is ExitAction.HOLD
    assert decision.hold_optimal is False
    assert ExitLayer.L2_EXPECTED_RETURN in decision.undetermined_layers


def test_hold_is_optimal_only_when_every_layer_is_determined_and_nothing_fires() -> None:
    layers = _layers(er=_er(exhaustion=False, low=False), rotation=_rotation(False))
    decision = _decide(_inp(layers=layers))
    assert decision.action is ExitAction.HOLD
    assert decision.hold_optimal is True
    assert decision.undetermined_layers == ()


def test_hold_is_not_optimal_when_a_human_review_is_requested() -> None:
    weak = _thesis(ThesisState.WEAKENING, (_ev(RootFactor.EARNINGS, "a"),))
    layers = _layers(thesis=weak, er=_er(exhaustion=False, low=False), rotation=_rotation(False))
    assert _decide(_inp(layers=layers)).hold_optimal is False


# ---------------------------------------------------------------------------
# (7) M10 L1 の悪化は売却の推奨にしない(review_flag + review_class)
# ---------------------------------------------------------------------------


def test_a_weakening_thesis_requests_a_review_and_never_a_sale_by_itself() -> None:
    decision = _decide(_inp(layers=_fe1_layers()))
    assert decision.action is ExitAction.HOLD
    assert decision.review_flag is ReviewFlag.MANUAL_REVIEW
    assert decision.review_class is ExitClass.RISK_EXIT


def test_a_broken_thesis_with_a_hard_gate_is_an_urgent_review_not_a_sale() -> None:
    layers = _layers(
        thesis=_thesis(
            ThesisState.BROKEN, (_ev(RootFactor.GOVERNANCE_EVENT, "scandal"),), gate=True
        )
    )
    decision = _decide(_inp(layers=layers))
    assert decision.action is ExitAction.HOLD
    assert decision.review_flag is ReviewFlag.URGENT_REVIEW
    assert decision.review_class is ExitClass.RISK_EXIT


def test_a_broken_thesis_without_a_hard_gate_is_a_manual_review() -> None:
    layers = _layers(thesis=_thesis(ThesisState.BROKEN))
    assert _decide(_inp(layers=layers)).review_flag is ReviewFlag.MANUAL_REVIEW


@pytest.mark.parametrize("state", [ThesisState.INTACT, None])
def test_no_review_without_a_weakening_or_broken_thesis(state: ThesisState | None) -> None:
    decision = _decide(_inp(layers=_layers(thesis=_thesis(state))))
    assert decision.review_flag is ReviewFlag.NONE
    assert decision.review_class is None


def test_the_review_does_not_change_the_sale_decision() -> None:
    plain = _decide(_inp([_prop()]))
    reviewed = _decide(_inp([_prop()], layers=_fe1_layers()))
    assert (plain.action, plain.strength) == (reviewed.action, reviewed.strength) or (
        reviewed.action is ExitAction.FULL
    )


# ---------------------------------------------------------------------------
# (8) L0 の信頼性: 同じ原因は 1 件・効果は 1 回(M15)
# ---------------------------------------------------------------------------


def test_a_candidate_depending_on_an_unusable_input_is_dropped_once() -> None:
    layers = _layers(
        groups=(("price", ReliabilityClass.UNUSABLE), ("fin", ReliabilityClass.RELIABLE))
    )
    decision = _decide(_inp([_prop(groups=("price",))], layers=layers))
    assert decision.action is ExitAction.HOLD
    caps = [s for s in decision.suppressed if s.reason is SuppressionReason.RELIABILITY_CAP]
    assert len(caps) == 1


def test_a_candidate_depending_on_a_degraded_input_is_lowered_by_the_policy_steps() -> None:
    layers = _layers(
        groups=(("price", ReliabilityClass.DEGRADED), ("fin", ReliabilityClass.RELIABLE))
    )
    lowered = _decide(_inp([_prop(strength=Strength.FULL)], layers=layers, policy=_policy()))
    assert lowered.strength < Strength.FULL
    none = _decide(
        _inp(
            [_prop(strength=Strength.PARTIAL, groups=("price",))],
            layers=layers,
            policy=_policy(degraded_downgrade_steps=0),
        )
    )
    assert none.action is ExitAction.PARTIAL
    one_step = _decide(_inp([_prop(strength=Strength.PARTIAL, groups=("price",))], layers=layers))
    assert one_step.action is ExitAction.HOLD
    assert one_step.strength is Strength.WATCH


def test_an_unknown_group_is_not_a_reason_to_cap() -> None:
    layers = _layers(groups=(("fin", ReliabilityClass.UNUSABLE), ("x", ReliabilityClass.RELIABLE)))
    decision = _decide(_inp([_prop(groups=("not-listed",))], layers=layers))
    assert decision.action is ExitAction.PARTIAL


def test_reliability_is_applied_once_whichever_side_marks_it() -> None:
    """層の側で信頼性を反映して候補を作らない場合と、Arbiter の側で UNUSABLE を反映する場合で、
    最終の action は同じ(二重に適用されて余分に弱くならない)。"""
    marked = _decide(
        _inp(
            [_prop(groups=("price",))],
            layers=_layers(
                groups=(("price", ReliabilityClass.UNUSABLE), ("b", ReliabilityClass.RELIABLE))
            ),
        )
    )
    never_proposed = _decide(_inp())
    assert marked.action is never_proposed.action is ExitAction.HOLD
    assert marked.strength == never_proposed.strength


# ---------------------------------------------------------------------------
# (9) 勝者の選択 (M23a): 現行の規則 = 最大の raw レベル -> 最大の origin
# ---------------------------------------------------------------------------

_KINDS = list(TRIGGER_TIE_ORDER)
_STRENGTHS = [Strength.WATCH, Strength.PARTIAL, Strength.FULL]
# 現行のエンジンが成立させうる (種別, 強さ) だけ(上限を超える主張は入力として拒否される)
_VALID_CLAIMS = [
    (kind, strength)
    for kind in _KINDS
    for strength in _STRENGTHS
    if strength <= MAX_STRENGTH_BY_TRIGGER[kind]
]


def _class_for(kind: TriggerKind) -> ExitClass:
    return sorted(ALLOWED_CLASSES_BY_TRIGGER[kind], key=lambda c: c.value)[0]


def _proposal(kind: TriggerKind, strength: Strength) -> CandidateProposal:
    cls = _class_for(kind)
    layer = ExitLayer.L1_THESIS if cls is ExitClass.RISK_EXIT else ExitLayer.L2_EXPECTED_RETURN
    return _prop(kind, cls, strength, layer)


def _reference_winner(
    proposals: list[tuple[TriggerKind, Strength]],
) -> tuple[Strength, Origin]:
    """現行の規則の最小の再現: 最大の raw レベル -> 同レベルなら最大の origin。"""
    level = max(strength for _, strength in proposals)
    top = [kind for kind, strength in proposals if strength == level]
    return level, max(ORIGIN_OF_TRIGGER[kind] for kind in top)


def test_the_winner_is_the_highest_level_then_the_highest_origin_like_today() -> None:
    layers = _fe1_layers()  # FULL が適格
    for count in (1, 2, 3):
        for combo in itertools.combinations(_VALID_CLAIMS, count):
            pairs = list(combo)
            decision = _decide(
                _inp([_proposal(k, s) for k, s in pairs], layers=layers, policy=_policy())
            )
            level, origin = _reference_winner(pairs)
            # 降格 (b) が無い入力で、勝者の強さ・origin は参照実装と一致する
            if level is Strength.WATCH:
                assert decision.action is ExitAction.HOLD
                assert decision.strength is Strength.WATCH
                continue
            assert int(decision.strength) == int(level), pairs
            assert decision.trigger_kind is not None
            assert ORIGIN_OF_TRIGGER[decision.trigger_kind] == origin, pairs


def test_a_same_level_same_origin_tie_is_decided_by_the_fixed_trigger_order() -> None:
    a = _prop(TriggerKind.FAIR_VALUE_STRONG, ExitClass.VALUE_EXIT, Strength.PARTIAL)
    b = _prop(TriggerKind.FAIR_VALUE_PARTIAL_GATE, ExitClass.VALUE_EXIT, Strength.PARTIAL)
    for ordering in itertools.permutations([a, b]):
        decision = _decide(_inp(ordering))
        assert decision.trigger_kind is TriggerKind.FAIR_VALUE_STRONG
        assert decision.supporting_triggers == (TriggerKind.FAIR_VALUE_PARTIAL_GATE,)


def test_the_tie_order_for_every_pair_follows_the_table() -> None:
    for first, second in itertools.combinations(TRIGGER_TIE_ORDER, 2):
        if ORIGIN_OF_TRIGGER[first] != ORIGIN_OF_TRIGGER[second]:
            continue
        a = _proposal(first, Strength.PARTIAL)
        b = _proposal(second, Strength.PARTIAL)
        layers = _layers(thesis=_thesis(ThesisState.INTACT))
        decision = _decide(_inp([b, a], layers=layers))
        assert decision.trigger_kind is first


def test_the_winner_does_not_depend_on_the_order_of_the_proposals() -> None:
    """M17 / M23b: 候補の列の順序を入れ替えても Decision が同じ。"""
    proposals = [
        _proposal(TriggerKind.PARTIAL_CONDITIONS, Strength.PARTIAL),
        _proposal(TriggerKind.FAIR_VALUE_PARTIAL_GATE, Strength.PARTIAL),
        _proposal(TriggerKind.PRICE_UPSIDE_MATRIX, Strength.PARTIAL),
        _proposal(TriggerKind.PROFIT_PROTECTION_STRONG, Strength.WATCH),
    ]
    results = {_decide(_inp(order)) for order in itertools.permutations(proposals)}
    assert len(results) == 1


def test_every_trigger_kind_maps_back_to_its_origin_from_the_decision() -> None:
    """M23c: Decision.trigger_kind から origin へ戻せる(N5 の前提)。"""
    layers = _fe1_layers()
    for kind in TRIGGER_TIE_ORDER:
        decision = _decide(_inp([_proposal(kind, Strength.PARTIAL)], layers=layers))
        assert decision.trigger_kind is kind
        assert ORIGIN_OF_TRIGGER[decision.trigger_kind] is ORIGIN_OF_TRIGGER[kind]


def test_redundant_candidates_never_raise_the_strength() -> None:
    """M1 / M23d: 同じ強さの候補を足しても強さが増えない。種別は supporting に残る。"""
    one = _decide(_inp([_proposal(TriggerKind.PRICE_UPSIDE_MATRIX, Strength.PARTIAL)]))
    many = _decide(
        _inp(
            [
                _proposal(TriggerKind.PRICE_UPSIDE_MATRIX, Strength.PARTIAL),
                _proposal(TriggerKind.PARTIAL_CONDITIONS, Strength.PARTIAL),
                _proposal(TriggerKind.FAIR_VALUE_PARTIAL_GATE, Strength.PARTIAL),
            ]
        )
    )
    assert many.strength == one.strength
    assert many.action is one.action
    assert set(many.supporting_triggers) == {
        TriggerKind.PARTIAL_CONDITIONS,
        TriggerKind.PRICE_UPSIDE_MATRIX,
    }
    assert many.trigger_kind is TriggerKind.FAIR_VALUE_PARTIAL_GATE


# ---------------------------------------------------------------------------
# (10) ★ M23e 選択と降格の順序(HANAKO の反例)★
# ---------------------------------------------------------------------------


def _counterexample(steps: int = 1) -> ArbiterInput:
    """A = raw FULL・origin OTHER_CONDITIONS / B = raw PARTIAL・origin PROFIT_PROTECTION_STRONG。
    勝者への緩和で A は PARTIAL に下がる。"""
    a = _prop(TriggerKind.FULL_MODERATE_CONDITIONS, ExitClass.VALUE_EXIT, Strength.FULL)
    b = _prop(
        TriggerKind.PROFIT_PROTECTION_STRONG,
        ExitClass.PROFIT_PROTECTION,
        Strength.PARTIAL,
        ExitLayer.L3_PRICE_REGIME,
    )
    return _inp([a, b], layers=_fe1_layers(), softening=_soft(steps=steps))


def test_the_winner_is_chosen_before_the_softening() -> None:
    decision = _decide(_counterexample())
    # 現行: raw の最大で A が勝つ。降格で PARTIAL になるが origin は OTHER のまま
    assert decision.trigger_kind is TriggerKind.FULL_MODERATE_CONDITIONS
    assert ORIGIN_OF_TRIGGER[decision.trigger_kind] is Origin.OTHER_CONDITIONS
    assert decision.action is ExitAction.PARTIAL
    assert decision.strength is Strength.PARTIAL


def test_the_softening_applies_only_to_the_winner() -> None:
    decision = _decide(_counterexample())
    losers = [
        s for s in decision.suppressed if s.reason is SuppressionReason.SUPERSEDED_BY_STRONGER
    ]
    assert len(losers) == 1
    # 選ばれなかった候補は降格されず、降格前の強さで trace に残る
    assert losers[0].exit_class is ExitClass.PROFIT_PROTECTION
    assert losers[0].strength is Strength.PARTIAL
    lowered = [s for s in decision.suppressed if s.reason is SuppressionReason.MITIGATION]
    assert [(s.exit_class, s.strength) for s in lowered] == [(ExitClass.VALUE_EXIT, Strength.FULL)]


def test_without_the_softening_the_same_winner_stays_full() -> None:
    decision = _decide(_counterexample(steps=0))
    assert decision.action is ExitAction.FULL
    assert decision.trigger_kind is TriggerKind.FULL_MODERATE_CONDITIONS


# ---------------------------------------------------------------------------
# (11) 勝者にだけ降格 (b): 順序・対象外・下限の保証(M21)
# ---------------------------------------------------------------------------


def _single(
    kind: TriggerKind,
    strength: Strength,
    *,
    soft: SofteningFacts,
    policy: ArbiterPolicy | None = None,
    layers: LayerVerdicts | None = None,
) -> Decision:
    return _decide(
        _inp(
            [_proposal(kind, strength)],
            layers=layers or _fe1_layers(),
            softening=soft,
            policy=policy,
        )
    )


def test_the_floor_keeps_partial_for_the_floor_origins_even_after_the_mitigation() -> None:
    """M21: PX-1(価格 × 上値余地)を含む origin 別の下限を保存する。"""
    for kind in (
        TriggerKind.PRICE_UPSIDE_MATRIX,
        TriggerKind.FAIR_VALUE_PARTIAL_GATE,
        TriggerKind.PROFIT_PROTECTION_STRONG,
    ):
        decision = _single(kind, Strength.PARTIAL, soft=_soft(steps=3))
        assert decision.action is ExitAction.PARTIAL, kind


def test_there_is_no_floor_for_the_other_conditions_origin() -> None:
    decision = _single(TriggerKind.PARTIAL_CONDITIONS, Strength.PARTIAL, soft=_soft(steps=1))
    assert decision.action is ExitAction.HOLD
    assert decision.strength is Strength.WATCH


def test_a_signal_is_never_softened_below_a_watch() -> None:
    """現行: 『緩和でHOLDになっても、最低でもWATCH』。タイミング層のあとも同じ。"""
    for kind in (TriggerKind.PARTIAL_CONDITIONS, TriggerKind.PRICE_UPSIDE_MATRIX):
        decision = _single(kind, Strength.WATCH, soft=_soft(steps=3, uptrend=True))
        assert decision.action is ExitAction.HOLD
        assert decision.strength is Strength.WATCH, kind
    full = _single(
        TriggerKind.FULL_MODERATE_CONDITIONS, Strength.FULL, soft=_soft(steps=9, uptrend=True)
    )
    assert full.strength is Strength.WATCH


def test_the_floor_needs_a_raw_partial_or_higher() -> None:
    decision = _single(TriggerKind.PRICE_UPSIDE_MATRIX, Strength.WATCH, soft=_soft(steps=1))
    assert decision.action is ExitAction.HOLD
    assert decision.strength is Strength.WATCH


def test_the_final_floor_holds_even_after_the_timing_layer() -> None:
    """現行: 最終の床は『緩和 + タイミングの合計でも PARTIAL 未満へ落とさない』
    (タイミング層の後)。"""
    decision = _single(
        TriggerKind.PRICE_UPSIDE_MATRIX, Strength.PARTIAL, soft=_soft(steps=1, uptrend=True)
    )
    assert decision.action is ExitAction.PARTIAL
    full = _single(
        TriggerKind.FAIR_VALUE_STRONG,
        Strength.FULL,
        soft=_soft(steps=1, uptrend=True),
        policy=_policy(timing_downgrade_steps=3),
    )
    assert full.action is ExitAction.PARTIAL


def test_the_timing_layer_can_lower_an_origin_without_a_floor() -> None:
    decision = _single(TriggerKind.PARTIAL_CONDITIONS, Strength.PARTIAL, soft=_soft(uptrend=True))
    assert decision.action is ExitAction.HOLD
    assert decision.strength is Strength.WATCH


def test_a_hard_overvalued_position_is_not_lowered_by_the_timing_layer() -> None:
    decision = _single(
        TriggerKind.PARTIAL_CONDITIONS,
        Strength.PARTIAL,
        soft=_soft(uptrend=True, hard=True),
    )
    assert decision.action is ExitAction.PARTIAL


def test_the_timing_layer_lowers_by_the_policy_steps_only_in_an_uptrend() -> None:
    steps2 = _policy(timing_downgrade_steps=2)
    full = _single(
        TriggerKind.FULL_MODERATE_CONDITIONS,
        Strength.FULL,
        soft=_soft(uptrend=True),
        policy=steps2,
    )
    assert full.strength is Strength.WATCH
    no_trend = _single(
        TriggerKind.FULL_MODERATE_CONDITIONS, Strength.FULL, soft=_soft(), policy=steps2
    )
    assert no_trend.action is ExitAction.FULL
    zero = _single(
        TriggerKind.FULL_MODERATE_CONDITIONS,
        Strength.FULL,
        soft=_soft(uptrend=True),
        policy=_policy(timing_downgrade_steps=0),
    )
    assert zero.action is ExitAction.FULL


def test_the_arbiter_adds_no_downgrade_of_its_own_for_the_earnings_window() -> None:
    """決算直前は降格として持たない(現行は E2 の入力側の gate = ceiling の利用可否)。
    Arbiter が二重に降格しない(現行の売却比率・action を変えない)。"""
    near = dataclasses.replace(
        _fe1_layers(),
        context=PortfolioContext(
            concentrated=False, trading_unit_feasible=True, earnings_window_near=True
        ),
    )
    away = _fe1_layers()
    for kind in (TriggerKind.FAIR_VALUE_STRONG, TriggerKind.FULL_MODERATE_CONDITIONS):
        a = _single(kind, Strength.FULL, soft=_soft(), layers=near)
        c = _single(kind, Strength.FULL, soft=_soft(), layers=away)
        assert (a.action, a.strength, a.trigger_kind) == (c.action, c.strength, c.trigger_kind)
    assert SuppressionReason.EARNINGS_WINDOW not in {
        s.reason
        for s in _single(
            TriggerKind.FAIR_VALUE_STRONG, Strength.FULL, soft=_soft(), layers=near
        ).suppressed
    }


def test_the_critical_origin_is_never_softened() -> None:
    soft = _soft(steps=3, uptrend=True)
    layers = dataclasses.replace(
        _fe1_layers(),
        context=PortfolioContext(
            concentrated=False, trading_unit_feasible=True, earnings_window_near=True
        ),
    )
    decision = _single(TriggerKind.FULL_STRONG_CRITICAL, Strength.FULL, soft=soft, layers=layers)
    assert decision.action is ExitAction.FULL
    assert decision.exit_class is ExitClass.RISK_EXIT
    assert not any(s.reason is SuppressionReason.MITIGATION for s in decision.suppressed)


def test_a_critical_full_still_needs_an_eligible_fe() -> None:
    no_fe = _single(
        TriggerKind.FULL_STRONG_CRITICAL,
        Strength.FULL,
        soft=_soft(),
        layers=_layers(),
    )
    assert no_fe.action is ExitAction.PARTIAL


def test_softening_never_runs_before_the_selection() -> None:
    """順序の固定(変異: 降格を各候補へ先に適用すると、反例で勝者が変わって落ちる)。

    勝者 A は降格で HOLD まで下がるが、選ばれなかった B が繰り上がって PARTIAL にはならない
    (現行も、勝者を決めたあとに降格する)。B は降格されずに trace へ残る。
    """
    decision = _decide(_counterexample(steps=2))
    assert decision.action is ExitAction.HOLD
    assert decision.trigger_kind is None
    superseded = [
        s for s in decision.suppressed if s.reason is SuppressionReason.SUPERSEDED_BY_STRONGER
    ]
    assert [(s.exit_class, s.strength) for s in superseded] == [
        (ExitClass.PROFIT_PROTECTION, Strength.PARTIAL)
    ]


# ---------------------------------------------------------------------------
# (12) regime は単調で FULL を含まない(M13)
# ---------------------------------------------------------------------------


def _regime_proposal(
    state: RegimeState, kind: TriggerKind, policy: ArbiterPolicy | None = None
) -> CandidateProposal | None:
    """adapter の役割の最小の再現: regime の強さを claimed_strength にして、現行の経路に対応する
    TriggerKind で候補を載せる。"""
    strength = regime_claimed_strength(state, policy or _policy())
    if strength < Strength.WATCH:
        return None
    return _prop(kind, ExitClass.PROFIT_PROTECTION, strength, ExitLayer.L3_PRICE_REGIME)


def test_the_arbiter_does_not_create_a_candidate_from_the_regime_by_itself() -> None:
    """M-1: TriggerKind(= origin)は『現行のどの経路で成立したか』という現行の事実で、
    adapter が付ける。
    Arbiter が regime の状態から候補を作ると、現行と違う origin(= 売却比率)になりうる。"""
    for state in RegimeState:
        decision = _decide(_inp(layers=_layers(regime=_regime(state))))
        assert decision.action is ExitAction.HOLD
        assert decision.strength is Strength.NONE
        assert decision.trigger_kind is None


def test_the_regime_strength_function_is_the_policy_mapping() -> None:
    policy = _policy()
    assert {s: regime_claimed_strength(s, policy) for s in RegimeState} == _REGIME


def test_a_heavier_regime_never_claims_a_weaker_strength() -> None:
    previous = Strength.NONE
    for state in (
        RegimeState.HEALTHY,
        RegimeState.PEAK_WARNING,
        RegimeState.DOWNTREND_CONFIRMED,
        RegimeState.BREAKDOWN,
    ):
        strength = regime_claimed_strength(state, _policy())
        assert strength >= previous, state
        previous = strength
        assert strength < Strength.FULL


@pytest.mark.parametrize("state", [RegimeState.DOWNTREND_CONFIRMED, RegimeState.BREAKDOWN])
def test_the_regime_alone_never_gives_full_even_with_an_eligible_fe_elsewhere(
    state: RegimeState,
) -> None:
    layers = dataclasses.replace(_fe1_layers(), regime=_regime(state))
    proposal = _regime_proposal(state, TriggerKind.PROFIT_PROTECTION_STRONG)
    assert proposal is not None
    decision = _decide(_inp([proposal], layers=layers))
    assert decision.action is ExitAction.PARTIAL
    assert decision.exit_class is ExitClass.PROFIT_PROTECTION


def test_a_regime_candidate_filed_as_other_conditions_keeps_the_light_origin() -> None:
    """現行の PX-4 / PX-5(利益保全 candidate + トレンド等)は OTHER_CONDITIONS の経路。
    adapter がその TriggerKind で載せる限り、origin は OTHER_CONDITIONS のまま
    (売却比率が変わらない)。"""
    proposal = _regime_proposal(RegimeState.BREAKDOWN, TriggerKind.PARTIAL_CONDITIONS)
    assert proposal is not None
    decision = _decide(_inp([proposal], layers=_layers(regime=_regime(RegimeState.BREAKDOWN))))
    assert decision.trigger_kind is TriggerKind.PARTIAL_CONDITIONS
    assert ORIGIN_OF_TRIGGER[decision.trigger_kind] is Origin.OTHER_CONDITIONS


def test_a_profit_protection_strong_candidate_beats_a_regime_candidate_of_the_same_strength() -> (
    None
):
    """M-1 ③: 同じ強さの『利益保全 strong』候補と regime 候補(OTHER_CONDITIONS)が並んだとき、
    現行と同じく origin の優先順位が高いほうが勝つ。売却比率に効く origin も現行と一致する。"""
    strong = _prop(
        TriggerKind.PROFIT_PROTECTION_STRONG,
        ExitClass.PROFIT_PROTECTION,
        Strength.PARTIAL,
        ExitLayer.L3_PRICE_REGIME,
    )
    regime = _regime_proposal(RegimeState.DOWNTREND_CONFIRMED, TriggerKind.PARTIAL_CONDITIONS)
    assert regime is not None
    layers = _layers(regime=_regime(RegimeState.DOWNTREND_CONFIRMED))
    for order in itertools.permutations([strong, regime]):
        decision = _decide(_inp(order, layers=layers))
        assert decision.trigger_kind is TriggerKind.PROFIT_PROTECTION_STRONG
        assert ORIGIN_OF_TRIGGER[decision.trigger_kind] is Origin.PROFIT_PROTECTION_STRONG
        assert decision.supporting_triggers == (TriggerKind.PARTIAL_CONDITIONS,)
    # regime 候補だけのときは、現行の OTHER_CONDITIONS の経路のまま
    alone = _decide(_inp([regime], layers=layers))
    assert ORIGIN_OF_TRIGGER[alone.trigger_kind] is Origin.OTHER_CONDITIONS  # type: ignore[index]


def test_a_healthy_or_undetermined_regime_claims_nothing() -> None:
    assert regime_claimed_strength(RegimeState.HEALTHY, _policy()) is Strength.NONE
    assert _regime_proposal(RegimeState.HEALTHY, TriggerKind.PARTIAL_CONDITIONS) is None
    undetermined = _prop(
        TriggerKind.PARTIAL_CONDITIONS,
        ExitClass.PROFIT_PROTECTION,
        Strength.PARTIAL,
        ExitLayer.L3_PRICE_REGIME,
    )
    decision = _decide(_inp([undetermined], layers=_layers(regime=_regime(None))))
    assert decision.action is ExitAction.HOLD


def test_the_watch_regime_is_a_watch_not_an_action() -> None:
    proposal = _regime_proposal(RegimeState.PEAK_WARNING, TriggerKind.PARTIAL_CONDITIONS)
    assert proposal is not None
    decision = _decide(_inp([proposal], layers=_layers(regime=_regime(RegimeState.PEAK_WARNING))))
    assert decision.action is ExitAction.HOLD
    assert decision.strength is Strength.WATCH
    assert decision.trigger_kind is None


# ---------------------------------------------------------------------------
# (13) 根拠の重複排除(M12)・順序非依存(M17)
# ---------------------------------------------------------------------------


def test_the_same_event_in_two_layers_counts_once() -> None:
    cut_l1 = _ev(RootFactor.RETURN_POLICY, "cut", event="RETURN_POLICY:dividend", primary=True)
    cut_l2 = _ev(
        RootFactor.RETURN_POLICY,
        "income",
        event="RETURN_POLICY:dividend",
        layer=ExitLayer.L2_EXPECTED_RETURN,
    )
    layers = _layers(
        thesis=_thesis(ThesisState.WEAKENING, (cut_l1,)),
        er=_er(exhaustion=True, evidence=(cut_l2,)),
    )
    merged = collect_evidence(layers)
    assert len(merged) == 1
    assert merged[0].primary_source_confirmed is True  # 確からしさの強いほうが残る


def test_the_same_fact_through_fe1_and_fe2_does_not_raise_the_strength() -> None:
    shared = _ev(RootFactor.EARNINGS, "e", event="EARNINGS:guidance")
    fe1 = _layers(
        thesis=_thesis(ThesisState.WEAKENING, (shared, _ev(RootFactor.CASHFLOW, "c"))),
        er=_er(exhaustion=True, low=True, evidence=(_ev(RootFactor.VALUATION_LEVEL, "v"), shared)),
    )
    both = _decide(_inp([_full_claim()], layers=fe1))
    only_fe1 = _decide(_inp([_full_claim()], layers=_fe1_layers()))
    assert both.action is only_fe1.action is ExitAction.FULL
    assert both.strength == only_fe1.strength


def test_dedupe_key_prefers_the_event_id_and_keeps_the_namespaces_apart() -> None:
    assert dedupe_key(_ev(RootFactor.EARNINGS, "a", event="E")) == ("event", "E")
    assert dedupe_key(_ev(RootFactor.EARNINGS, "a")) == ("fact", "EARNINGS:a")
    clash_event = _ev(RootFactor.EARNINGS, "a", event="EARNINGS:b")
    clash_fact = _ev(RootFactor.EARNINGS, "b")
    assert len(dedupe_evidence((clash_event, clash_fact))) == 2


def test_dedupe_evidence_is_order_independent_and_picks_the_stronger() -> None:
    suspected = _ev(RootFactor.EARNINGS, "x", status=EvidenceStatus.SUSPECTED, source="a")
    triggered = _ev(RootFactor.EARNINGS, "x", status=EvidenceStatus.TRIGGERED, source="b")
    confirmed = _ev(RootFactor.EARNINGS, "x", primary=True, source="c")
    for order in itertools.permutations([suspected, triggered, confirmed]):
        assert dedupe_evidence(order) == (confirmed,)
    tie_a = _ev(RootFactor.EARNINGS, "x", source="a")
    tie_b = _ev(RootFactor.EARNINGS, "x", source="b")
    for order in itertools.permutations([tie_a, tie_b]):
        assert dedupe_evidence(order) == (tie_a,)


def test_a_layer_marker_does_not_change_the_existing_helpers() -> None:
    """Evidence.layer の既定値 None は、既存の dedupe_by_fact_key・distinct_roots の挙動を
    変えない。"""
    plain = (_ev(RootFactor.EARNINGS, "a"), _ev(RootFactor.CASHFLOW, "b"))
    marked = tuple(dataclasses.replace(e, layer=ExitLayer.L1_THESIS) for e in plain)
    assert distinct_roots(plain) == distinct_roots(marked)
    assert [e.fact_key for e in dedupe_by_fact_key(plain)] == [
        e.fact_key for e in dedupe_by_fact_key(marked)
    ]
    assert _ev(RootFactor.EARNINGS, "a").layer is None


def test_the_decision_does_not_depend_on_the_order_of_the_evidence() -> None:
    forward = (_ev(RootFactor.EARNINGS, "a"), _ev(RootFactor.CASHFLOW, "b"))
    for order in itertools.permutations(forward):
        layers = _layers(thesis=_thesis(ThesisState.WEAKENING, order))
        decision = _decide(_inp([_full_claim()], layers=layers))
        assert decision == _decide(_inp([_full_claim()], layers=_fe1_layers()))


# ---------------------------------------------------------------------------
# (14) 追加した Decision の field と不変条件(P-4・P-5)
# ---------------------------------------------------------------------------


def test_the_new_decision_fields_have_defaults_so_existing_constructions_still_work() -> None:
    hold = Decision(ExitAction.HOLD, ExitClass.NONE, Strength.NONE)
    assert hold.review_class is None
    assert hold.undetermined_layers == ()
    assert hold.trigger_kind is None
    assert hold.supporting_triggers == ()


def test_a_review_class_needs_a_review_flag() -> None:
    with pytest.raises(ContractViolationError):
        Decision(ExitAction.HOLD, ExitClass.NONE, Strength.NONE, review_class=ExitClass.RISK_EXIT)
    ok = Decision(
        ExitAction.HOLD,
        ExitClass.NONE,
        Strength.NONE,
        review_flag=ReviewFlag.MANUAL_REVIEW,
        review_class=ExitClass.RISK_EXIT,
    )
    assert ok.review_class is ExitClass.RISK_EXIT


def test_hold_optimal_excludes_undetermined_layers() -> None:
    with pytest.raises(ContractViolationError):
        Decision(
            ExitAction.HOLD,
            ExitClass.NONE,
            Strength.NONE,
            hold_optimal=True,
            undetermined_layers=(ExitLayer.L2_EXPECTED_RETURN,),
        )


def test_a_hold_has_no_winning_trigger() -> None:
    with pytest.raises(ContractViolationError):
        Decision(
            ExitAction.HOLD,
            ExitClass.NONE,
            Strength.NONE,
            trigger_kind=TriggerKind.PARTIAL_CONDITIONS,
        )


def test_the_supporting_triggers_need_a_winner_and_exclude_it() -> None:
    kwargs: dict[str, object] = {
        "action": ExitAction.PARTIAL,
        "exit_class": ExitClass.VALUE_EXIT,
        "strength": Strength.PARTIAL,
        "primary_layer": ExitLayer.L2_EXPECTED_RETURN,
    }
    with pytest.raises(ContractViolationError):
        Decision(**kwargs, supporting_triggers=(TriggerKind.PARTIAL_CONDITIONS,))  # type: ignore[arg-type]
    with pytest.raises(ContractViolationError):
        Decision(
            **kwargs,  # type: ignore[arg-type]
            trigger_kind=TriggerKind.PARTIAL_CONDITIONS,
            supporting_triggers=(TriggerKind.PARTIAL_CONDITIONS,),
        )


def test_the_new_verdict_evidence_fields_default_to_empty() -> None:
    assert _er().evidence == ()
    assert _rotation().evidence == ()


def test_the_arbiter_always_sets_the_winning_trigger_for_a_sale() -> None:
    for kind in TRIGGER_TIE_ORDER:
        decision = _decide(_inp([_proposal(kind, Strength.PARTIAL)], layers=_fe1_layers()))
        if decision.action is not ExitAction.HOLD:
            assert decision.trigger_kind is kind


# ---------------------------------------------------------------------------
# (15) 純粋性・配線なし・fact_key を解析しない(M18・M20)
# ---------------------------------------------------------------------------


def _tree() -> ast.Module:
    return ast.parse(_SRC.read_text(encoding="utf-8"))


def test_the_arbiter_is_deterministic() -> None:
    inp = _counterexample()
    assert arbitrate(inp) == arbitrate(inp)


def test_the_arbiter_never_reads_the_fact_key_of_an_evidence() -> None:
    """M20: 票は強さを持たない。fact_key を解析しない(読む attribute にも出てこない)。"""
    attributes = {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    assert "fact_key" not in attributes
    assert "source" not in attributes


def test_the_module_has_no_case_specific_code_and_no_literal_thresholds() -> None:
    source = _SRC.read_text(encoding="utf-8")
    assert "9536" not in source
    floats = [
        n.lineno
        for n in ast.walk(_tree())
        if isinstance(n, ast.Constant) and isinstance(n.value, float)
    ]
    assert floats == []
    integers = {
        n.value
        for n in ast.walk(_tree())
        if isinstance(n, ast.Constant)
        and isinstance(n.value, int)
        and not isinstance(n.value, bool)
    }
    # 0 / 1 のほか、Origin の優先順位(1〜5)だけ。降格の段数・root の数は policy の引数
    assert integers <= {0, 1, 2, 3, 4, 5}


def test_the_arbiter_does_not_reference_the_current_engines() -> None:
    imported = {n.module for n in ast.walk(_tree()) if isinstance(n, ast.ImportFrom) and n.module}
    assert all(
        m.startswith(
            (
                "jstock_advisor.domain.exit_architecture",
                "dataclasses",
                "enum",
                "collections",
                "__future__",
            )
        )
        for m in imported
    )


# ---------------------------------------------------------------------------
# TriggerKind ごとの強さの上限(現行に無い全株売却を作らない)
# ---------------------------------------------------------------------------

# 現行の profit_taking.py の候補生成から、各経路が到達できる最大のレベルを独立に書き下した表
# (実装の定数の写しではなく、期待値として別に持つ)
_CURRENT_ENGINE_MAXIMUM = {
    TriggerKind.PARTIAL_CONDITIONS: Strength.PARTIAL,
    TriggerKind.FAIR_VALUE_PARTIAL_GATE: Strength.PARTIAL,
    TriggerKind.PROFIT_PROTECTION_STRONG: Strength.PARTIAL,
    TriggerKind.FULL_MODERATE_CONDITIONS: Strength.FULL,
    TriggerKind.PRICE_UPSIDE_MATRIX: Strength.FULL,
    TriggerKind.FAIR_VALUE_STRONG: Strength.FULL,
    TriggerKind.FULL_STRONG_CRITICAL: Strength.FULL,
}
_PARTIAL_ONLY = [k for k, m in _CURRENT_ENGINE_MAXIMUM.items() if m is Strength.PARTIAL]


def test_every_trigger_kind_has_a_maximum_strength() -> None:
    assert set(MAX_STRENGTH_BY_TRIGGER) == set(ORIGIN_OF_TRIGGER) == set(TRIGGER_TIE_ORDER)


def test_the_maximum_matches_what_the_current_engine_can_reach() -> None:
    assert MAX_STRENGTH_BY_TRIGGER == _CURRENT_ENGINE_MAXIMUM


@pytest.mark.parametrize("kind", list(_CURRENT_ENGINE_MAXIMUM), ids=lambda k: k.value)
def test_a_claim_above_the_maximum_is_rejected_and_up_to_it_is_accepted(kind: TriggerKind) -> None:
    maximum = _CURRENT_ENGINE_MAXIMUM[kind]
    for strength in _STRENGTHS:
        if strength <= maximum:
            _proposal(kind, strength)
        else:
            with pytest.raises(ContractViolationError):
                _proposal(kind, strength)


@pytest.mark.parametrize("kind", _PARTIAL_ONLY, ids=lambda k: k.value)
def test_an_eligible_fe_cannot_turn_a_partial_only_path_into_a_full(kind: TriggerKind) -> None:
    """FE-1 が適格でも、現行が PARTIAL までしか成立させない経路は FULL を主張できず、
    PARTIAL の主張は PARTIAL のまま(全株売却を増やさない)。"""
    with pytest.raises(ContractViolationError):
        _proposal(kind, Strength.FULL)
    decision = _single(kind, Strength.PARTIAL, soft=_soft(), layers=_fe1_layers())
    assert decision.action is ExitAction.PARTIAL


# ---------------------------------------------------------------------------
# 緩和要因とタイミング層は trace で区別できる
# ---------------------------------------------------------------------------


def _softening_reasons(decision: Decision) -> list[SuppressionReason]:
    return [
        s.reason
        for s in decision.suppressed
        if s.reason in (SuppressionReason.MITIGATION, SuppressionReason.TIMING_LAYER)
    ]


def test_mitigation_and_the_timing_layer_are_distinguishable_in_the_trace() -> None:
    kind = TriggerKind.FULL_MODERATE_CONDITIONS
    both = _single(kind, Strength.FULL, soft=_soft(steps=1, uptrend=True))
    assert sorted(r.value for r in _softening_reasons(both)) == ["MITIGATION", "TIMING_LAYER"]
    only_mitigation = _single(kind, Strength.FULL, soft=_soft(steps=1))
    assert _softening_reasons(only_mitigation) == [SuppressionReason.MITIGATION]
    only_timing = _single(kind, Strength.FULL, soft=_soft(uptrend=True))
    assert _softening_reasons(only_timing) == [SuppressionReason.TIMING_LAYER]
    neither = _single(kind, Strength.FULL, soft=_soft())
    assert _softening_reasons(neither) == []


# ---------------------------------------------------------------------------
# 出力の『並び』の決定性(集合の比較では固定できない性質。#894 の SHOULD-1 の型の確認で追加)
# ---------------------------------------------------------------------------


def test_the_supporting_triggers_follow_the_fixed_tie_order_for_any_input_order() -> None:
    """supporting_triggers は表示・trace に出る tuple。入力の並びが違っても、固定の順序
    (TRIGGER_TIE_ORDER)で同じ並びになる。集合の比較では並びを固定できない。"""
    kinds = [
        TriggerKind.PRICE_UPSIDE_MATRIX,
        TriggerKind.PARTIAL_CONDITIONS,
        TriggerKind.FAIR_VALUE_PARTIAL_GATE,
        TriggerKind.PROFIT_PROTECTION_STRONG,
    ]
    expected_all = tuple(k for k in TRIGGER_TIE_ORDER if k in kinds)
    for ordering in itertools.permutations(kinds):
        decision = _decide(_inp([_proposal(k, Strength.PARTIAL) for k in ordering]))
        assert decision.trigger_kind is expected_all[0]
        assert decision.supporting_triggers == expected_all[1:], ordering


def test_dedupe_evidence_returns_a_canonical_order_across_different_facts() -> None:
    """異なる事実が複数あるとき、出力の並びは統合キーの順で、入力の並びに依存しない(同じ事実の
    統合だけでなく、結果の tuple の並びを固定する)。"""
    items = [
        _ev(RootFactor.CASHFLOW, "b"),
        _ev(RootFactor.EARNINGS, "a", event="E2"),
        _ev(RootFactor.EARNINGS, "c"),
        _ev(RootFactor.EARNINGS, "z", event="E1"),
        _ev(RootFactor.BALANCE_SHEET, "d"),
    ]
    expected = tuple(sorted(items, key=dedupe_key))
    for ordering in itertools.permutations(items):
        assert dedupe_evidence(ordering) == expected, [dedupe_key(i) for i in ordering]
    # 並びは入力順そのものではない(入力順を保つ実装では、この期待と一致しない入力がある)
    assert tuple(items) != expected


def test_collect_evidence_returns_a_canonical_order_for_any_layer_order_of_the_same_facts() -> None:
    a, b_, c = (
        _ev(RootFactor.EARNINGS, "a"),
        _ev(RootFactor.CASHFLOW, "b"),
        _ev(RootFactor.BALANCE_SHEET, "c"),
    )
    reference = None
    for thesis_items in itertools.permutations([a, b_]):
        layers = _layers(
            thesis=_thesis(ThesisState.WEAKENING, tuple(thesis_items)),
            er=_er(exhaustion=True, evidence=(c,)),
        )
        merged = collect_evidence(layers)
        reference = reference or merged
        assert merged == reference
    assert reference is not None
    assert reference == tuple(sorted(reference, key=dedupe_key))


def test_the_suppressed_trace_is_in_the_documented_order_for_any_input_order() -> None:
    """suppressed は(class の固定の順 -> 強さの降順 -> 理由)で並ぶ。入力の並びに依存しない。"""
    proposals = [
        _proposal(TriggerKind.FULL_MODERATE_CONDITIONS, Strength.FULL),
        _proposal(TriggerKind.PROFIT_PROTECTION_STRONG, Strength.PARTIAL),
        _proposal(TriggerKind.PRICE_UPSIDE_MATRIX, Strength.PARTIAL),
        _proposal(TriggerKind.PARTIAL_CONDITIONS, Strength.WATCH),
    ]
    reference = None
    for ordering in itertools.permutations(proposals):
        decision = _decide(_inp(list(ordering), layers=_fe1_layers(), softening=_soft(steps=1)))
        keys = [
            (_CLASS_ORDER.index(s.exit_class), -int(s.strength), s.reason.value)
            for s in decision.suppressed
        ]
        assert keys == sorted(keys), ordering
        assert len(decision.suppressed) >= 3
        reference = reference or decision.suppressed
        assert decision.suppressed == reference
