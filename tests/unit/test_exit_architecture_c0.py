"""Exit Architecture の契約(C0。Issue #878 PR-1)の型・語彙・不変条件の契約テスト。

## 何を固定するか

    (1) 語彙の集合: 変更は意図した変更として、このテストの更新と一緒に行う
    (2) 三値(値 / UNDETERMINED): UNDETERMINED を真偽値・値として扱えない(根拠にも否定にも
        ならない。値を捏造しない)
    (3) 根拠: root_factor は必須。同じ root は 1(R-A)。同じ事実は 1 件に統合(R-D)
    (4) 型の不変条件: HOLD と class NONE は同値 / FULL は FULL の独立根拠を持つ。
        valuation 枯渇のみでは FULL にならない(UJ-4)。価格由来・ユーザー目標・データ品質・
        時機・集中の根は FULL の独立根拠に使えない(OP-5・UJ-6・OP-7)
    (5) available_cash が型に存在しない(OP-7。M4)
    (6) **現行のエンジンからどこからも import されない**(配線なし)・I/O を持たない

## 何を固定しないか

Arbiter の判定(強さ・cap・降格・4 ケースの区別・M1〜M3・M6〜M11 の性質)は #878 の PR-2。
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import pkgutil
from enum import IntEnum, StrEnum
from pathlib import Path

import pytest

import jstock_advisor.domain.exit_architecture as pkg
from jstock_advisor.domain.exit_architecture.decision import (
    ALLOWED_ROOTS_BY_KIND,
    FORBIDDEN_FULL_ROOTS,
    ContractViolationError,
    Decision,
    FullEvidence,
    SuppressedCandidate,
)
from jstock_advisor.domain.exit_architecture.determination import (
    Determination,
    UndeterminedError,
)
from jstock_advisor.domain.exit_architecture.evidence import (
    Evidence,
    EvidenceStatus,
    dedupe_by_fact_key,
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

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "jstock_advisor"
_PKG_DIR = _SRC / "domain" / "exit_architecture"


def _modules() -> list[object]:
    return [
        importlib.import_module(f"{pkg.__name__}.{info.name}")
        for info in pkgutil.iter_modules(pkg.__path__)
    ]


# --- (1) 語彙の集合 -----------------------------------------------------------------------

_VOCABULARY: dict[type, set[str]] = {
    ExitAction: {"HOLD", "PARTIAL", "FULL"},
    ExitClass: {"RISK_EXIT", "VALUE_EXIT", "PROFIT_PROTECTION", "CAPITAL_ROTATION", "NONE"},
    Strength: {"NONE", "WATCH", "PARTIAL", "FULL"},
    ReviewFlag: {"NONE", "MANUAL_REVIEW", "URGENT_REVIEW"},
    ThesisState: {"INTACT", "WEAKENING", "BROKEN"},
    RegimeState: {"HEALTHY", "PEAK_WARNING", "DOWNTREND_CONFIRMED", "BREAKDOWN"},
    ReliabilityClass: {"RELIABLE", "DEGRADED", "UNUSABLE"},
    RootFactor: {
        "PRICE_PATH",
        "VALUATION_LEVEL",
        "EARNINGS",
        "CASHFLOW",
        "BALANCE_SHEET",
        "RETURN_POLICY",
        "GOVERNANCE_EVENT",
        "EVENT_RISK",
        "PORTFOLIO",
        "DATA",
        "USER_DIRECTIVE",
        "OPPORTUNITY_COST",
    },
    FullEvidenceKind: {
        "THESIS_DETERIORATION",
        "EXPECTED_RETURN_DETERIORATION",
        "ROTATION_OPPORTUNITY",
    },
    TriggerKind: {
        "PRICE_UPSIDE_MATRIX",
        "FAIR_VALUE_STRONG",
        "FAIR_VALUE_PARTIAL_GATE",
        "PROFIT_PROTECTION_STRONG",
        "PARTIAL_CONDITIONS",
        "FULL_MODERATE_CONDITIONS",
        "FULL_STRONG_CRITICAL",
        "USER_TARGET_PRICE",
        "USER_TARGET_RATE",
    },
    ExitLayer: {
        "L0_DATA_RELIABILITY",
        "L1_THESIS",
        "L2_EXPECTED_RETURN",
        "L3_PRICE_REGIME",
        "L4_OPPORTUNITY_COST",
    },
    UndeterminedReason: {
        "COMPONENT_NOT_IMPLEMENTED",
        "INPUT_MISSING",
        "COVERAGE_INSUFFICIENT",
        "NOT_EVALUATED",
        "RELIABILITY_UNUSABLE",
        "GUARD_NOT_MET",
    },
    SuppressionReason: {
        "RELIABILITY_CAP",
        "PROFIT_PROTECTION_PARTIAL_CAP",
        "NO_FULL_EVIDENCE",
        "EARNINGS_WINDOW",
        "MITIGATION",
        "UNDETERMINED_INPUT",
        "DUPLICATE_EVIDENCE",
        "SUPERSEDED_BY_STRONGER",
    },
    EvidenceStatus: {"TRIGGERED", "SUSPECTED", "NOT_EVALUATED"},
    ExpectedReturnComponent: {"UPSIDE", "INCOME", "REVISION", "RISK_ADJUSTMENT"},
}


@pytest.mark.parametrize("enum_cls", list(_VOCABULARY), ids=lambda c: c.__name__)
def test_vocabulary_members_are_fixed(enum_cls: type) -> None:
    assert {m.name for m in enum_cls} == _VOCABULARY[enum_cls]  # type: ignore[attr-defined]


@pytest.mark.parametrize("enum_cls", [c for c in _VOCABULARY if issubclass(c, StrEnum)])
def test_string_enum_values_equal_their_names(enum_cls: type) -> None:
    # 永続・監査の値として使うとき、名前と値がずれない
    assert all(m.value == m.name for m in enum_cls)  # type: ignore[attr-defined]


def test_strength_is_ordered_none_watch_partial_full() -> None:
    assert issubclass(Strength, IntEnum)
    assert Strength.NONE < Strength.WATCH < Strength.PARTIAL < Strength.FULL


def test_valuation_exhaustion_is_not_a_full_evidence_kind() -> None:
    # UJ-4: valuation 枯渇は L2 の材料だが、FULL の独立根拠の種類ではない
    names = {m.name for m in FullEvidenceKind}
    assert not any("VALUATION" in n or "EXHAUST" in n or "UPSIDE" in n for n in names)
    assert len(names) == 3


# --- (2) 三値 ------------------------------------------------------------------------------


def test_determination_holds_a_value_or_an_undetermined_reason_never_both_or_neither() -> None:
    assert Determination.of(1.5).unwrap() == 1.5
    undetermined = Determination[float].undetermined(UndeterminedReason.COMPONENT_NOT_IMPLEMENTED)
    assert not undetermined.is_determined
    with pytest.raises(ValueError):
        Determination[float]()
    with pytest.raises(ValueError):
        Determination(value=1.0, reason=UndeterminedReason.INPUT_MISSING)
    with pytest.raises(ValueError):
        Determination.of(None)


def test_a_falsy_value_is_still_a_determined_value() -> None:
    for falsy in (0, 0.0, False, ""):
        determined = Determination.of(falsy)
        assert determined.is_determined
        assert determined.unwrap() == falsy


def test_undetermined_cannot_be_unwrapped_or_used_as_a_boolean() -> None:
    undetermined = Determination[bool].undetermined(UndeterminedReason.NOT_EVALUATED, "x")
    with pytest.raises(UndeterminedError):
        undetermined.unwrap()
    with pytest.raises(TypeError):
        bool(undetermined)
    with pytest.raises(TypeError):
        if Determination.of(True):  # 確定した値でも真偽値には変換できない
            pass


def test_determination_is_frozen_and_comparable() -> None:
    a = Determination.of(2.0)
    assert a == Determination.of(2.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.value = 3.0  # type: ignore[misc]


# --- (3) 根拠 ------------------------------------------------------------------------------


def _ev(
    root: RootFactor = RootFactor.EARNINGS,
    fact_key: str = "operating_income_decline",
    *,
    status: EvidenceStatus = EvidenceStatus.TRIGGERED,
    primary: bool = False,
    source: str = "rule",
) -> Evidence:
    return Evidence(
        root_factor=root,
        source=source,
        fact_key=fact_key,
        status=status,
        primary_source_confirmed=primary,
    )


def test_evidence_requires_a_root_factor() -> None:
    with pytest.raises(TypeError):
        Evidence(source="s", fact_key="k")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Evidence(root_factor="EARNINGS", source="s", fact_key="k")  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["source", "fact_key"])
def test_evidence_rejects_blank_source_and_fact_key(field: str) -> None:
    kwargs = {"root_factor": RootFactor.EARNINGS, "source": "s", "fact_key": "k", field: "  "}
    with pytest.raises(ValueError):
        Evidence(**kwargs)  # type: ignore[arg-type]


def test_only_triggered_evidence_counts_as_independent() -> None:
    assert _ev(status=EvidenceStatus.TRIGGERED).counts_as_independent
    assert not _ev(status=EvidenceStatus.SUSPECTED).counts_as_independent
    assert not _ev(status=EvidenceStatus.NOT_EVALUATED).counts_as_independent


def test_distinct_roots_counts_a_root_once_however_many_evidence_it_has() -> None:
    base = [_ev(RootFactor.EARNINGS, "a"), _ev(RootFactor.CASHFLOW, "b")]
    padded = [*base, _ev(RootFactor.EARNINGS, "c"), _ev(RootFactor.EARNINGS, "d")]
    assert (
        distinct_roots(base) == distinct_roots(padded) == {RootFactor.EARNINGS, RootFactor.CASHFLOW}
    )


def test_adding_evidence_with_a_new_root_adds_that_root_monotonically() -> None:
    base = [_ev(RootFactor.EARNINGS, "a")]
    more = [*base, _ev(RootFactor.BALANCE_SHEET, "b")]
    assert distinct_roots(base) < distinct_roots(more)


def test_suspected_and_unevaluated_evidence_do_not_add_a_root() -> None:
    base = [_ev(RootFactor.EARNINGS, "a")]
    noisy = [
        *base,
        _ev(RootFactor.RETURN_POLICY, "b", status=EvidenceStatus.SUSPECTED),
        _ev(RootFactor.CASHFLOW, "c", status=EvidenceStatus.NOT_EVALUATED),
    ]
    assert distinct_roots(noisy) == distinct_roots(base)


def test_dedupe_by_fact_key_keeps_one_per_fact_in_first_seen_order() -> None:
    items = [_ev(fact_key="x"), _ev(fact_key="y"), _ev(fact_key="x"), _ev(fact_key="z")]
    assert [e.fact_key for e in dedupe_by_fact_key(items)] == ["x", "y", "z"]


def test_dedupe_prefers_the_stronger_status_then_primary_source_confirmation() -> None:
    weak = _ev(fact_key="x", status=EvidenceStatus.SUSPECTED, source="e3")
    strong = _ev(fact_key="x", status=EvidenceStatus.TRIGGERED, source="e1")
    assert dedupe_by_fact_key([weak, strong])[0] is strong
    assert dedupe_by_fact_key([strong, weak])[0] is strong
    plain = _ev(fact_key="y", source="e1")
    confirmed = _ev(fact_key="y", primary=True, source="e3")
    assert dedupe_by_fact_key([plain, confirmed])[0] is confirmed


def test_dedupe_is_idempotent() -> None:
    items = [_ev(fact_key="x"), _ev(fact_key="x", primary=True), _ev(fact_key="y")]
    once = dedupe_by_fact_key(items)
    assert dedupe_by_fact_key(once) == once


# --- 各層の verdict -------------------------------------------------------------------------


def _undetermined(reason: UndeterminedReason = UndeterminedReason.COMPONENT_NOT_IMPLEMENTED):
    return Determination.undetermined(reason)


def _components(*, determined: bool) -> tuple[ComponentValue, ...]:
    return tuple(
        ComponentValue(c, Determination.of(0.0) if determined else _undetermined())
        for c in ExpectedReturnComponent
    )


def test_thesis_verdict_hard_gate_and_reasons_go_together_and_mean_broken() -> None:
    broken = Determination.of(ThesisState.BROKEN)
    ThesisVerdict(
        broken, ReliabilityClass.RELIABLE, hard_gate_triggered=True, hard_gate_reasons=("X",)
    )
    with pytest.raises(ValueError):
        ThesisVerdict(broken, ReliabilityClass.RELIABLE, hard_gate_triggered=True)
    with pytest.raises(ValueError):
        ThesisVerdict(broken, ReliabilityClass.RELIABLE, hard_gate_reasons=("X",))
    for not_broken in (Determination.of(ThesisState.INTACT), _undetermined()):
        with pytest.raises(ValueError):
            ThesisVerdict(
                not_broken,
                ReliabilityClass.RELIABLE,
                hard_gate_triggered=True,
                hard_gate_reasons=("X",),
            )


def test_thesis_verdict_can_be_undetermined_without_being_intact() -> None:
    verdict = ThesisVerdict(
        _undetermined(UndeterminedReason.COVERAGE_INSUFFICIENT), ReliabilityClass.DEGRADED
    )
    assert not verdict.thesis_state.is_determined
    assert verdict.thesis_state != Determination.of(ThesisState.INTACT)


def test_expected_return_severely_low_requires_every_component_determined() -> None:
    ExpectedReturnVerdict(
        _components(determined=True),
        Determination.of(True),
        Determination.of(True),
        ReliabilityClass.RELIABLE,
    )
    # 未実装の軸から値を作って FE-2 を確定させない
    with pytest.raises(ValueError):
        ExpectedReturnVerdict(
            _components(determined=False),
            Determination.of(True),
            Determination.of(True),
            ReliabilityClass.RELIABLE,
        )
    partial = (
        *_components(determined=True)[:3],
        ComponentValue(ExpectedReturnComponent.RISK_ADJUSTMENT, _undetermined()),
    )
    with pytest.raises(ValueError):
        ExpectedReturnVerdict(
            partial, _undetermined(), Determination.of(False), ReliabilityClass.RELIABLE
        )


def test_expected_return_can_be_fully_undetermined_while_the_pieces_are_recorded() -> None:
    verdict = ExpectedReturnVerdict(
        _components(determined=False),
        Determination.of(True),  # 枯渇という個別の事実は確定していてよい
        _undetermined(),
        ReliabilityClass.RELIABLE,
    )
    assert verdict.valuation_exhaustion.unwrap() is True
    assert not verdict.severely_low.is_determined


def test_expected_return_rejects_duplicate_components() -> None:
    dup = (
        *_components(determined=False),
        ComponentValue(ExpectedReturnComponent.UPSIDE, _undetermined()),
    )
    with pytest.raises(ValueError):
        ExpectedReturnVerdict(dup, _undetermined(), _undetermined(), ReliabilityClass.RELIABLE)


def test_input_reliability_rejects_a_blank_group() -> None:
    with pytest.raises(ValueError):
        InputReliability(" ", ReliabilityClass.RELIABLE)


def _layer_verdicts(**override: object) -> LayerVerdicts:
    base: dict[str, object] = {
        "reliability": ReliabilityVerdict(),
        "thesis": ThesisVerdict(Determination.of(ThesisState.INTACT), ReliabilityClass.RELIABLE),
        "expected_return": ExpectedReturnVerdict(
            _components(determined=False),
            _undetermined(),
            _undetermined(),
            ReliabilityClass.RELIABLE,
        ),
        "regime": RegimeVerdict(_undetermined(), _undetermined(), _undetermined(), _undetermined()),
        "rotation": RotationVerdict(_undetermined()),
        "context": PortfolioContext(False, True, False),
    }
    base.update(override)
    return LayerVerdicts(**base)  # type: ignore[arg-type]


def test_layer_verdicts_require_every_layer_explicitly() -> None:
    assert _layer_verdicts().rotation.gap_clear.is_determined is False
    for layer in ("reliability", "thesis", "expected_return", "regime", "rotation", "context"):
        kwargs = {
            f.name: getattr(_layer_verdicts(), f.name) for f in dataclasses.fields(LayerVerdicts)
        }
        del kwargs[layer]
        with pytest.raises(TypeError):
            LayerVerdicts(**kwargs)


# --- (4) Decision の不変条件 ----------------------------------------------------------------


def _thesis(state: ThesisState | None = ThesisState.WEAKENING) -> ThesisVerdict:
    """裏づけの ThesisVerdict。state が None なら UNDETERMINED。"""
    determination = (
        Determination.of(state)
        if state is not None
        else _undetermined(UndeterminedReason.COVERAGE_INSUFFICIENT)
    )
    return ThesisVerdict(determination, ReliabilityClass.RELIABLE)


def _er(severely_low: bool | None = True) -> ExpectedReturnVerdict:
    """裏づけの ExpectedReturnVerdict。None なら全て UNDETERMINED(未実装の間の状態)。"""
    if severely_low is None:
        return ExpectedReturnVerdict(
            _components(determined=False),
            _undetermined(),
            _undetermined(),
            ReliabilityClass.RELIABLE,
        )
    return ExpectedReturnVerdict(
        _components(determined=True),
        Determination.of(True),
        Determination.of(severely_low),
        ReliabilityClass.RELIABLE,
    )


def _rotation(gap_clear: bool | None = True) -> RotationVerdict:
    return RotationVerdict(_undetermined() if gap_clear is None else Determination.of(gap_clear))


def _full_evidence() -> FullEvidence:
    return FullEvidence(
        FullEvidenceKind.THESIS_DETERIORATION,
        (_ev(RootFactor.EARNINGS, "a"), _ev(RootFactor.CASHFLOW, "b")),
        thesis=_thesis(),
    )


def _partial() -> Decision:
    return Decision(
        ExitAction.PARTIAL, ExitClass.VALUE_EXIT, Strength.PARTIAL, ExitLayer.L2_EXPECTED_RETURN
    )


def test_valid_decisions_can_be_built() -> None:
    hold = Decision(ExitAction.HOLD, ExitClass.NONE, Strength.NONE, hold_optimal=True)
    assert hold.hold_optimal and hold.independent_roots == frozenset()
    assert _partial().action is ExitAction.PARTIAL
    full = Decision(
        ExitAction.FULL,
        ExitClass.RISK_EXIT,
        Strength.FULL,
        ExitLayer.L1_THESIS,
        full_evidence=(_full_evidence(),),
        suppressed=(
            SuppressedCandidate(
                ExitClass.VALUE_EXIT, Strength.PARTIAL, SuppressionReason.SUPERSEDED_BY_STRONGER
            ),
        ),
        review_flag=ReviewFlag.URGENT_REVIEW,
    )
    assert full.independent_roots == {RootFactor.EARNINGS, RootFactor.CASHFLOW}


@pytest.mark.parametrize(
    "kwargs",
    [
        # HOLD と class NONE は同値
        {"action": ExitAction.HOLD, "exit_class": ExitClass.VALUE_EXIT, "strength": Strength.NONE},
        {
            "action": ExitAction.PARTIAL,
            "exit_class": ExitClass.NONE,
            "strength": Strength.PARTIAL,
            "primary_layer": ExitLayer.L1_THESIS,
        },
        # hold_optimal は HOLD のときだけ
        {
            "action": ExitAction.PARTIAL,
            "exit_class": ExitClass.VALUE_EXIT,
            "strength": Strength.PARTIAL,
            "primary_layer": ExitLayer.L2_EXPECTED_RETURN,
            "hold_optimal": True,
        },
        # action と strength の整合
        {"action": ExitAction.HOLD, "exit_class": ExitClass.NONE, "strength": Strength.PARTIAL},
        {
            "action": ExitAction.PARTIAL,
            "exit_class": ExitClass.VALUE_EXIT,
            "strength": Strength.WATCH,
            "primary_layer": ExitLayer.L2_EXPECTED_RETURN,
        },
        {
            "action": ExitAction.PARTIAL,
            "exit_class": ExitClass.VALUE_EXIT,
            "strength": Strength.FULL,
            "primary_layer": ExitLayer.L2_EXPECTED_RETURN,
        },
        # 売却の action には主たる層が要る
        {
            "action": ExitAction.PARTIAL,
            "exit_class": ExitClass.VALUE_EXIT,
            "strength": Strength.PARTIAL,
        },
    ],
)
def test_inconsistent_decisions_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ContractViolationError):
        Decision(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("exit_class", [c for c in ExitClass if c is not ExitClass.NONE])
def test_full_without_a_full_evidence_is_rejected_for_every_class(exit_class: ExitClass) -> None:
    # valuation 枯渇のみ(FULL の独立根拠なし)では、どの class でも FULL にならない(UJ-4 / OP-5)
    with pytest.raises(ContractViolationError):
        Decision(ExitAction.FULL, exit_class, Strength.FULL, ExitLayer.L2_EXPECTED_RETURN)


@pytest.mark.parametrize("root", sorted(FORBIDDEN_FULL_ROOTS, key=lambda r: r.value))
def test_roots_that_cannot_justify_full_are_rejected_in_a_full_evidence(root: RootFactor) -> None:
    # 裏づけの verdict を揃えた上で拒否される(= 拒否の理由は root)
    with pytest.raises(ContractViolationError):
        FullEvidence(
            FullEvidenceKind.THESIS_DETERIORATION,
            (_ev(RootFactor.EARNINGS, "a"), _ev(root, "b")),
            thesis=_thesis(),
        )
    with pytest.raises(ContractViolationError):
        FullEvidence(
            FullEvidenceKind.EXPECTED_RETURN_DETERIORATION,
            (_ev(RootFactor.VALUATION_LEVEL, "v"), _ev(root, "b")),
            expected_return=_er(True),
        )


def test_forbidden_full_roots_are_exactly_price_user_directive_data_event_and_portfolio() -> None:
    assert {
        RootFactor.PRICE_PATH,
        RootFactor.USER_DIRECTIVE,
        RootFactor.DATA,
        RootFactor.EVENT_RISK,
        RootFactor.PORTFOLIO,
    } == FORBIDDEN_FULL_ROOTS
    # 売る理由になりうる root は禁止に入っていない(VALUE_EXIT を不当に弱めない)
    assert RootFactor.VALUATION_LEVEL not in FORBIDDEN_FULL_ROOTS


def test_full_evidence_needs_supporting_evidence_that_is_independent() -> None:
    with pytest.raises(ContractViolationError):
        FullEvidence(FullEvidenceKind.THESIS_DETERIORATION, (), thesis=_thesis())
    suspected_only = (_ev(RootFactor.RETURN_POLICY, "d", status=EvidenceStatus.SUSPECTED),)
    with pytest.raises(ContractViolationError):
        FullEvidence(FullEvidenceKind.THESIS_DETERIORATION, suspected_only, thesis=_thesis())


def test_full_evidence_may_carry_a_suspected_item_next_to_an_independent_one() -> None:
    mixed = (
        _ev(RootFactor.EARNINGS, "a"),
        _ev(RootFactor.RETURN_POLICY, "d", status=EvidenceStatus.SUSPECTED),
    )
    full = Decision(
        ExitAction.FULL,
        ExitClass.VALUE_EXIT,
        Strength.FULL,
        ExitLayer.L1_THESIS,
        full_evidence=(
            FullEvidence(FullEvidenceKind.THESIS_DETERIORATION, mixed, thesis=_thesis()),
        ),
    )
    # 推定のみの根は独立 root に数えない
    assert full.independent_roots == {RootFactor.EARNINGS}


# --- (4b) FE の kind と root の対応・裏づけ(UJ-4。HANAKO の PR 前 review の M-1) ---------------

_VALUATION_ONLY = [
    (_ev(RootFactor.VALUATION_LEVEL, "v1"),),
    (_ev(RootFactor.VALUATION_LEVEL, "v1"), _ev(RootFactor.VALUATION_LEVEL, "v2")),
]


@pytest.mark.parametrize("kind", list(FullEvidenceKind))
@pytest.mark.parametrize("evidence", _VALUATION_ONLY, ids=["one", "several"])
def test_valuation_exhaustion_alone_cannot_form_a_full_evidence_under_any_kind(
    kind: FullEvidenceKind, evidence: tuple[Evidence, ...]
) -> None:
    # 裏づけの verdict を全て揃えても、VALUATION_LEVEL の根拠だけでは、どの kind でも拒否される
    with pytest.raises(ContractViolationError):
        FullEvidence(
            kind,
            evidence,
            thesis=_thesis(ThesisState.BROKEN),
            expected_return=_er(True),
            rotation=_rotation(True),
        )


def test_relabelling_valuation_exhaustion_as_expected_return_cannot_make_a_full_decision() -> None:
    # review の反例: 枯渇の根拠 1 件に FE-2 のラベルを付けて FULL の Decision を作る
    with pytest.raises(ContractViolationError):
        FullEvidence(
            FullEvidenceKind.EXPECTED_RETURN_DETERIORATION,
            (_ev(RootFactor.VALUATION_LEVEL, "v"),),
            expected_return=_er(True),
        )
    # FullEvidence が作れない以上、それを持つ FULL の Decision も作れない。FULL の入口は他に無い
    with pytest.raises(ContractViolationError):
        Decision(ExitAction.FULL, ExitClass.VALUE_EXIT, Strength.FULL, ExitLayer.L2_EXPECTED_RETURN)


def test_allowed_roots_by_kind_is_a_fixed_vocabulary_correspondence() -> None:
    assert {
        FullEvidenceKind.THESIS_DETERIORATION: {
            RootFactor.EARNINGS,
            RootFactor.CASHFLOW,
            RootFactor.BALANCE_SHEET,
            RootFactor.RETURN_POLICY,
            RootFactor.GOVERNANCE_EVENT,
        },
        FullEvidenceKind.EXPECTED_RETURN_DETERIORATION: {
            RootFactor.VALUATION_LEVEL,
            RootFactor.RETURN_POLICY,
            RootFactor.EARNINGS,
        },
        FullEvidenceKind.ROTATION_OPPORTUNITY: {RootFactor.OPPORTUNITY_COST},
    } == ALLOWED_ROOTS_BY_KIND
    # 許す root は、禁止の root と重ならない。VALUATION_LEVEL は FE-2 だけ(単独では不可)
    for allowed in ALLOWED_ROOTS_BY_KIND.values():
        assert not allowed & FORBIDDEN_FULL_ROOTS
    assert [k for k, v in ALLOWED_ROOTS_BY_KIND.items() if RootFactor.VALUATION_LEVEL in v] == [
        FullEvidenceKind.EXPECTED_RETURN_DETERIORATION
    ]


def _backing_for(kind: FullEvidenceKind) -> dict[str, object]:
    return {
        FullEvidenceKind.THESIS_DETERIORATION: {"thesis": _thesis()},
        FullEvidenceKind.EXPECTED_RETURN_DETERIORATION: {"expected_return": _er(True)},
        FullEvidenceKind.ROTATION_OPPORTUNITY: {"rotation": _rotation(True)},
    }[kind]


@pytest.mark.parametrize(
    ("kind", "root"),
    [
        (kind, root)
        for kind in FullEvidenceKind
        for root in RootFactor
        if root not in ALLOWED_ROOTS_BY_KIND[kind]
    ],
    ids=lambda v: v.value if isinstance(v, StrEnum) else str(v),
)
def test_a_root_outside_the_kinds_correspondence_is_rejected_even_with_full_backing(
    kind: FullEvidenceKind, root: RootFactor
) -> None:
    anchor = sorted(ALLOWED_ROOTS_BY_KIND[kind] - {RootFactor.VALUATION_LEVEL}, key=str)[0]
    with pytest.raises(ContractViolationError):
        FullEvidence(kind, (_ev(anchor, "a"), _ev(root, "b")), **_backing_for(kind))  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", list(FullEvidenceKind))
def test_each_kind_is_buildable_with_its_own_roots_and_backing(kind: FullEvidenceKind) -> None:
    roots = sorted(ALLOWED_ROOTS_BY_KIND[kind] - {RootFactor.VALUATION_LEVEL}, key=str)
    evidence = tuple(_ev(r, f"f{i}") for i, r in enumerate(roots))
    built = FullEvidence(kind, evidence, **_backing_for(kind))  # type: ignore[arg-type]
    assert built.kind is kind


def test_fe2_can_combine_valuation_level_with_another_component_root() -> None:
    built = FullEvidence(
        FullEvidenceKind.EXPECTED_RETURN_DETERIORATION,
        (_ev(RootFactor.VALUATION_LEVEL, "v"), _ev(RootFactor.RETURN_POLICY, "r")),
        expected_return=_er(True),
    )
    assert built.kind is FullEvidenceKind.EXPECTED_RETURN_DETERIORATION


def test_fe2_second_root_must_itself_be_an_independent_evidence() -> None:
    suspected = _ev(RootFactor.RETURN_POLICY, "r", status=EvidenceStatus.SUSPECTED)
    with pytest.raises(ContractViolationError):
        FullEvidence(
            FullEvidenceKind.EXPECTED_RETURN_DETERIORATION,
            (_ev(RootFactor.VALUATION_LEVEL, "v"), suspected),
            expected_return=_er(True),
        )


@pytest.mark.parametrize("severely_low", [False, None], ids=["not-low", "undetermined"])
def test_fe2_needs_a_determined_true_severely_low(severely_low: bool | None) -> None:
    evidence = (_ev(RootFactor.VALUATION_LEVEL, "v"), _ev(RootFactor.RETURN_POLICY, "r"))
    with pytest.raises(ContractViolationError):
        FullEvidence(
            FullEvidenceKind.EXPECTED_RETURN_DETERIORATION,
            evidence,
            expected_return=_er(severely_low),
        )
    with pytest.raises(ContractViolationError):
        FullEvidence(FullEvidenceKind.EXPECTED_RETURN_DETERIORATION, evidence)


@pytest.mark.parametrize("state", [ThesisState.INTACT, None], ids=["intact", "undetermined"])
def test_fe1_needs_a_weakening_or_broken_thesis(state: ThesisState | None) -> None:
    evidence = (_ev(RootFactor.EARNINGS, "a"),)
    with pytest.raises(ContractViolationError):
        FullEvidence(FullEvidenceKind.THESIS_DETERIORATION, evidence, thesis=_thesis(state))
    with pytest.raises(ContractViolationError):
        FullEvidence(FullEvidenceKind.THESIS_DETERIORATION, evidence)


@pytest.mark.parametrize("state", [ThesisState.WEAKENING, ThesisState.BROKEN])
def test_fe1_accepts_weakening_and_broken(state: ThesisState) -> None:
    evidence = (_ev(RootFactor.EARNINGS, "a"),)
    assert FullEvidence(FullEvidenceKind.THESIS_DETERIORATION, evidence, thesis=_thesis(state))


@pytest.mark.parametrize("gap", [False, None], ids=["not-clear", "undetermined"])
def test_fe3_cannot_be_built_until_the_rotation_gap_is_determined_true(gap: bool | None) -> None:
    evidence = (_ev(RootFactor.OPPORTUNITY_COST, "o"),)
    with pytest.raises(ContractViolationError):
        FullEvidence(FullEvidenceKind.ROTATION_OPPORTUNITY, evidence, rotation=_rotation(gap))
    with pytest.raises(ContractViolationError):
        FullEvidence(FullEvidenceKind.ROTATION_OPPORTUNITY, evidence)


def test_an_undetermined_backing_never_makes_a_full_evidence() -> None:
    # 未実装の軸(RAER・資本入替)が UNDETERMINED の間は、FE-2 / FE-3 を作れない(値を作らない)
    for kind, backing in (
        (FullEvidenceKind.EXPECTED_RETURN_DETERIORATION, {"expected_return": _er(None)}),
        (FullEvidenceKind.ROTATION_OPPORTUNITY, {"rotation": _rotation(None)}),
    ):
        roots = sorted(ALLOWED_ROOTS_BY_KIND[kind] - {RootFactor.VALUATION_LEVEL}, key=str)
        evidence = tuple(_ev(r, f"f{i}") for i, r in enumerate(roots))
        with pytest.raises(ContractViolationError):
            FullEvidence(kind, evidence, **backing)  # type: ignore[arg-type]


# --- (5) available_cash が型に存在しない(M4) -------------------------------------------------


def _mentions_cash(name: str) -> bool:
    # CASHFLOW(営業 CF の root)は別物。買付余力・現金残高の類だけを検出する
    return "cash" in name.lower().replace("cashflow", "")


def _package_dataclasses() -> list[type]:
    found: list[type] = []
    for module in _modules():
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if dataclasses.is_dataclass(obj) and obj.__module__ == module.__name__:  # type: ignore[attr-defined]
                found.append(obj)
    return found


def test_no_dataclass_field_in_the_package_carries_cash() -> None:
    classes = _package_dataclasses()
    assert len(classes) >= 10  # 走査が空振りしていない
    offenders = [
        f"{cls.__name__}.{field.name}"
        for cls in classes
        for field in dataclasses.fields(cls)
        if _mentions_cash(field.name) or "capital" in field.name.lower()
    ]
    assert offenders == []


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_package_source_never_mentions_available_cash_outside_comments_and_docstrings() -> None:
    for path in sorted(_PKG_DIR.glob("*.py")):
        tree = ast.parse(_source(path))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {a.arg for n in ast.walk(tree) if isinstance(n, ast.arguments) for a in n.args}
        assert not [n for n in names if _mentions_cash(n)], path.name


# --- (6) 配線なし・I/O なし ------------------------------------------------------------------

_PACKAGE_NAME = "jstock_advisor.domain.exit_architecture"


def _imports(path: Path) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(ast.parse(_source(path))):
        if isinstance(node, ast.Import):
            modules += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            modules.append(f"{prefix}{node.module or ''}")
            modules += [f"{prefix}{node.module or ''}.{a.name}" for a in node.names]
    return modules


def test_no_existing_source_file_imports_the_package() -> None:
    importers = []
    for path in sorted(_SRC.rglob("*.py")):
        if _PKG_DIR in path.parents:
            continue
        if any(m == _PACKAGE_NAME or m.startswith(_PACKAGE_NAME + ".") for m in _imports(path)):
            importers.append(path.relative_to(_SRC).as_posix())
    # 現行のエンジンへ配線しない(配線は後続の承認された PR)
    assert importers == []


_ALLOWED_IMPORT_ROOTS = {
    "__future__",
    "dataclasses",
    "enum",
    "typing",
    "collections",
    "jstock_advisor",
}


def test_the_package_imports_only_pure_standard_library_and_itself() -> None:
    for path in sorted(_PKG_DIR.glob("*.py")):
        for module in _imports(path):
            root = module.split(".")[0]
            assert root in _ALLOWED_IMPORT_ROOTS, f"{path.name}: {module}"
            if root == "jstock_advisor":
                assert module.startswith(_PACKAGE_NAME), f"{path.name}: {module}"


def test_the_package_has_no_io_or_logging_calls() -> None:
    forbidden = {"open", "print", "input", "exec", "eval", "getLogger"}
    for path in sorted(_PKG_DIR.glob("*.py")):
        calls = {
            n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
            for n in ast.walk(ast.parse(_source(path)))
            if isinstance(n, ast.Call)
        }
        assert not (calls & forbidden), path.name
