"""E2 の adapter(`profit_taking_adapter`)の契約テスト(Issue #878 PR-3b-1。dormant)。

設計: #878 rev4 §2・rev5(SHOULD-2)・rev6。adapter は ExitTrace を import せず Protocol で受ける。

  A  対応表: CandidatePath と TriggerKind の 1:1・票の価格由来 ∪ 非価格 = VoteKind の全値(互いに素)
  B  帰属 layer・class・強さ・source = LEGACY_ENGINE・空の input_groups / evidence
  C  USER_TARGET_* は proposals に出ず pass_through へ
  D  ★ 読む 10 field(ExitTrace の 6 + TraceCandidate の 4)を全て値で検査する ★(総当たり)
  E  SofteningFacts が trace と一致・不正なら None
  F  入力の並びに依存しない(全順列)・例外を送出しない
  G  Protocol であって import ではない(偽の trace でも動く)・AST(import の制限・dormant)
  H  ★ ゲートは変えない ★: adapter の出力を Arbiter へ渡すと、層が未確定の候補は従来どおり落ちる
"""

from __future__ import annotations

import ast
import itertools
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.domain.exit_architecture import profit_taking_adapter as adapter
from jstock_advisor.domain.exit_architecture.arbiter import (
    ALLOWED_CLASSES_BY_TRIGGER,
    MAX_STRENGTH_BY_TRIGGER,
    ProposalSource,
    SofteningFacts,
    build_candidates,
)
from jstock_advisor.domain.exit_architecture.decision import SuppressionReason
from jstock_advisor.domain.exit_architecture.profit_taking_adapter import (
    NON_PRICE_VOTES,
    PRICE_DERIVED_VOTES,
    PassThroughKind,
    PassThroughNotice,
    adapt_profit_taking_trace,
)
from jstock_advisor.domain.exit_architecture.vocabulary import (
    ExitClass,
    ExitLayer,
    Strength,
    TriggerKind,
)
from jstock_advisor.domain.signals.profit_taking import (
    CandidatePath,
    ExitTrace,
    TraceCandidate,
    VoteKind,
)
from tests.unit.test_arbiter_contract import _er, _inp, _layers, _regime, _thesis

_REPO = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO / "src" / "jstock_advisor"
_ADAPTER = _PACKAGE / "domain" / "exit_architecture" / "profit_taking_adapter.py"

_PRICE_VOTE = VoteKind.TREND_WORSENING
_NON_PRICE_VOTE = VoteKind.FAIR_VALUE_WEAK


def _candidate(path: Any, level: Any = 2, origin: Any = "OTHER_CONDITIONS", votes: Any = ()):  # type: ignore[no-untyped-def]
    return SimpleNamespace(path=path, level=level, origin=origin, votes=votes)


def _trace(candidates: Any = (), **overrides: Any):  # type: ignore[no-untyped-def]
    values: dict[str, Any] = {
        "candidates": tuple(candidates),
        "raw_level": 2,
        "origin": "PRICE_POSITION",
        "mitigation_steps": 0,
        "uptrend": False,
        "hard_overvalued": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _real(candidates: tuple[TraceCandidate, ...], **overrides: Any) -> ExitTrace:
    values: dict[str, Any] = {
        "candidates": candidates,
        "raw_level": 2,
        "origin": "PRICE_POSITION",
        "mitigation_steps": 1,
        "uptrend": True,
        "hard_overvalued": False,
    }
    values.update(overrides)
    return ExitTrace(**values)


# =================================================================================================
# A 対応表
# =================================================================================================


def test_every_path_maps_to_a_trigger_kind_or_to_pass_through() -> None:
    paths = {p.value for p in CandidatePath}
    kinds = {k.value for k in TriggerKind}

    assert paths == kinds  # 名前(値)の 1:1。経路を足すと落ちる
    assert {k.value for k in PassThroughKind} == {"USER_TARGET_PRICE", "USER_TARGET_RATE"}
    assert {k.value for k in PassThroughKind} <= paths
    mapped = paths - {k.value for k in PassThroughKind}
    assert mapped == {k.value for k in adapter._TRIGGER_BY_PATH.values()}  # noqa: SLF001
    assert len(mapped) == 7


def test_price_derived_and_non_price_votes_partition_the_vote_vocabulary() -> None:
    votes = {v.value for v in VoteKind}

    assert votes == PRICE_DERIVED_VOTES | NON_PRICE_VOTES
    assert not PRICE_DERIVED_VOTES & NON_PRICE_VOTES
    assert {
        "TREND_WORSENING",
        "STRONG_TREND_WORSENING",
        "PROFIT_PROTECTION_CANDIDATE",
        "LOW_YIELD",
        "VERY_LOW_YIELD",
    } == PRICE_DERIVED_VOTES
    assert len(votes) == 10


# =================================================================================================
# B 写し方
# =================================================================================================

_FIXED = [
    (CandidatePath.PRICE_UPSIDE_MATRIX, ExitLayer.L2_EXPECTED_RETURN, ExitClass.VALUE_EXIT),
    (CandidatePath.FAIR_VALUE_STRONG, ExitLayer.L2_EXPECTED_RETURN, ExitClass.VALUE_EXIT),
    (CandidatePath.FAIR_VALUE_PARTIAL_GATE, ExitLayer.L2_EXPECTED_RETURN, ExitClass.VALUE_EXIT),
    (
        CandidatePath.PROFIT_PROTECTION_STRONG,
        ExitLayer.L3_PRICE_REGIME,
        ExitClass.PROFIT_PROTECTION,
    ),
    (CandidatePath.FULL_STRONG_CRITICAL, ExitLayer.L1_THESIS, ExitClass.RISK_EXIT),
]


@pytest.mark.parametrize(("path", "layer", "exit_class"), _FIXED, ids=lambda v: str(v))
def test_a_fixed_path_gets_its_layer_and_class(
    path: CandidatePath, layer: ExitLayer, exit_class: ExitClass
) -> None:
    kind = TriggerKind(path.value)
    level = int(MAX_STRENGTH_BY_TRIGGER[kind])

    result = adapt_profit_taking_trace(_trace([_candidate(path.value, level)]))

    [proposal] = result.proposals
    assert (proposal.trigger_kind, proposal.layer, proposal.exit_class) == (kind, layer, exit_class)
    assert proposal.claimed_strength == Strength(level)  # trace の level をそのまま主張
    assert proposal.source is ProposalSource.LEGACY_ENGINE
    assert proposal.evidence == () and proposal.input_groups == frozenset()
    assert result.unmapped == () and result.pass_through == ()


@pytest.mark.parametrize(
    "path", [CandidatePath.PARTIAL_CONDITIONS, CandidatePath.FULL_MODERATE_CONDITIONS], ids=str
)
@pytest.mark.parametrize(
    ("votes", "layer", "exit_class"),
    [
        ((_PRICE_VOTE.value,), ExitLayer.L3_PRICE_REGIME, ExitClass.PROFIT_PROTECTION),
        (
            tuple(sorted(PRICE_DERIVED_VOTES)),
            ExitLayer.L3_PRICE_REGIME,
            ExitClass.PROFIT_PROTECTION,
        ),
        ((_NON_PRICE_VOTE.value,), ExitLayer.L2_EXPECTED_RETURN, ExitClass.VALUE_EXIT),
        (
            (_PRICE_VOTE.value, _NON_PRICE_VOTE.value),  # 1 つでも非価格なら L2
            ExitLayer.L2_EXPECTED_RETURN,
            ExitClass.VALUE_EXIT,
        ),
    ],
)
def test_a_count_condition_path_follows_the_origin_of_its_votes(
    path: CandidatePath, votes: tuple[str, ...], layer: ExitLayer, exit_class: ExitClass
) -> None:
    result = adapt_profit_taking_trace(_trace([_candidate(path.value, 2, votes=votes)]))

    [proposal] = result.proposals
    assert (proposal.layer, proposal.exit_class) == (layer, exit_class)
    assert result.unmapped == ()


def test_every_proposal_passes_the_arbiters_own_validation() -> None:
    """全ての (経路, level): 通れば proposal・通らなければ unmapped(例外を出さない)。"""
    accepted = rejected = 0
    for path in CandidatePath:
        if path.value in {k.value for k in PassThroughKind}:
            continue
        kind = TriggerKind(path.value)
        votes = (_PRICE_VOTE.value,)
        for level in (1, 2, 3):
            result = adapt_profit_taking_trace(_trace([_candidate(path.value, level, votes=votes)]))
            if level <= MAX_STRENGTH_BY_TRIGGER[kind]:
                [proposal] = result.proposals
                assert proposal.exit_class in ALLOWED_CLASSES_BY_TRIGGER[kind]
                accepted += 1
            else:
                assert result.proposals == () and result.unmapped == (adapter.CANDIDATE_REJECTED,)
                rejected += 1
    assert accepted > 10 and rejected >= 3  # 両方の経路を通る(空振りでない)


# =================================================================================================
# C ユーザー目標
# =================================================================================================


def test_user_targets_go_to_pass_through_only() -> None:
    trace = _trace(
        [
            _candidate("USER_TARGET_RATE", 2),
            _candidate("USER_TARGET_PRICE", 3),
            _candidate("USER_TARGET_PRICE", 2),  # 重複は 1 件
            _candidate("PRICE_UPSIDE_MATRIX", 2),
        ]
    )

    result = adapt_profit_taking_trace(trace)

    assert [p.trigger_kind for p in result.proposals] == [TriggerKind.PRICE_UPSIDE_MATRIX]
    assert result.pass_through == (
        PassThroughNotice(PassThroughKind.USER_TARGET_PRICE),
        PassThroughNotice(PassThroughKind.USER_TARGET_RATE),
    )  # 種別の固定の順序・重複なし
    assert all(n.kind.value.startswith("USER_TARGET") for n in result.pass_through)
    assert not hasattr(
        PassThroughNotice(PassThroughKind.USER_TARGET_RATE), "text"
    )  # 文面を持たない


# =================================================================================================
# D 値の検査(読む 10 field の総当たり)
# =================================================================================================

_TRACE_BAD = [
    ("candidates", None, adapter.TRACE_CANDIDATES),
    ("candidates", "abc", adapter.TRACE_CANDIDATES),
    ("candidates", 5, adapter.TRACE_CANDIDATES),
    ("raw_level", -1, adapter.TRACE_RAW_LEVEL),
    ("raw_level", 4, adapter.TRACE_RAW_LEVEL),
    ("raw_level", True, adapter.TRACE_RAW_LEVEL),
    ("raw_level", "2", adapter.TRACE_RAW_LEVEL),
    ("raw_level", None, adapter.TRACE_RAW_LEVEL),
    ("origin", "", adapter.TRACE_ORIGIN),
    ("origin", 5, adapter.TRACE_ORIGIN),
    ("origin", None, adapter.TRACE_ORIGIN),
    ("mitigation_steps", -1, adapter.TRACE_MITIGATION_STEPS),
    ("mitigation_steps", True, adapter.TRACE_MITIGATION_STEPS),
    ("mitigation_steps", "1", adapter.TRACE_MITIGATION_STEPS),
    ("mitigation_steps", None, adapter.TRACE_MITIGATION_STEPS),
    ("uptrend", 1, adapter.TRACE_UPTREND),
    ("uptrend", "True", adapter.TRACE_UPTREND),
    ("uptrend", None, adapter.TRACE_UPTREND),
    ("hard_overvalued", 0, adapter.TRACE_HARD_OVERVALUED),
    ("hard_overvalued", "no", adapter.TRACE_HARD_OVERVALUED),
    ("hard_overvalued", None, adapter.TRACE_HARD_OVERVALUED),
]


@pytest.mark.parametrize(("field", "value", "code"), _TRACE_BAD, ids=lambda v: repr(v)[:24])
def test_each_trace_field_is_checked_by_value(field: str, value: Any, code: str) -> None:
    trace = _trace([_candidate("PRICE_UPSIDE_MATRIX", 2)])
    setattr(trace, field, value)

    result = adapt_profit_taking_trace(trace)  # 例外を送出しない

    assert code in result.unmapped
    if field in {"mitigation_steps", "uptrend", "hard_overvalued"}:
        assert result.softening is None  # 作れない = 作らない(fail-closed)
    else:
        assert result.softening == SofteningFacts(0, False, False)


_CANDIDATE_BAD = [
    ("path", "", adapter.CANDIDATE_PATH),
    ("path", 5, adapter.CANDIDATE_PATH),
    ("path", None, adapter.CANDIDATE_PATH),
    ("path", "SOMETHING_NEW", adapter.CANDIDATE_PATH_UNKNOWN),
    ("level", 0, adapter.CANDIDATE_LEVEL),
    ("level", 4, adapter.CANDIDATE_LEVEL),
    ("level", -1, adapter.CANDIDATE_LEVEL),
    ("level", True, adapter.CANDIDATE_LEVEL),
    ("level", "2", adapter.CANDIDATE_LEVEL),
    ("level", 2.0, adapter.CANDIDATE_LEVEL),
    ("level", None, adapter.CANDIDATE_LEVEL),
    ("origin", "", adapter.CANDIDATE_ORIGIN),
    ("origin", 5, adapter.CANDIDATE_ORIGIN),
    ("origin", None, adapter.CANDIDATE_ORIGIN),
    ("votes", "TREND_WORSENING", adapter.CANDIDATE_VOTES),  # 文字列は票の列ではない
    ("votes", None, adapter.CANDIDATE_VOTES),
    ("votes", (1,), adapter.CANDIDATE_VOTES),
    ("votes", ("NOT_A_VOTE",), adapter.CANDIDATE_VOTE_UNKNOWN),
]


@pytest.mark.parametrize(("field", "value", "code"), _CANDIDATE_BAD, ids=lambda v: repr(v)[:24])
def test_each_candidate_field_is_checked_by_value(field: str, value: Any, code: str) -> None:
    good = _candidate("PRICE_UPSIDE_MATRIX", 2)
    bad = _candidate("PRICE_UPSIDE_MATRIX", 2)
    setattr(bad, field, value)

    result = adapt_profit_taking_trace(_trace([good, bad]))

    assert code in result.unmapped
    assert len(result.proposals) == 1  # 壊れた 1 件だけが落ち、他は写る(隔離)


def test_count_condition_paths_need_votes() -> None:
    result = adapt_profit_taking_trace(_trace([_candidate("PARTIAL_CONDITIONS", 2, votes=())]))

    assert result.proposals == ()
    assert result.unmapped == (adapter.CANDIDATE_VOTES_EMPTY,)


@pytest.mark.parametrize("missing", ["candidates", "raw_level", "origin"])
def test_a_trace_without_a_required_field_is_a_shape_error(missing: str) -> None:
    trace = _trace([_candidate("PRICE_UPSIDE_MATRIX", 2)])
    delattr(trace, missing)

    result = adapt_profit_taking_trace(trace)

    assert adapter.TRACE_SHAPE in result.unmapped


@pytest.mark.parametrize("missing", ["path", "level", "origin"])
def test_a_candidate_without_a_required_field_is_a_shape_error(missing: str) -> None:
    candidate = _candidate("PRICE_UPSIDE_MATRIX", 2)
    delattr(candidate, missing)

    result = adapt_profit_taking_trace(_trace([candidate]))

    assert adapter.CANDIDATE_SHAPE in result.unmapped and result.proposals == ()


@pytest.mark.parametrize("junk", [None, 5, "x", object(), [], ()])
def test_anything_that_is_not_a_trace_never_raises(junk: Any) -> None:
    result = adapt_profit_taking_trace(junk)

    assert result.proposals == () and result.pass_through == ()
    assert result.softening is None and result.unmapped


# =================================================================================================
# E SofteningFacts
# =================================================================================================


@pytest.mark.parametrize(
    ("steps", "uptrend", "hard"), list(itertools.product((0, 1, 2), (False, True), (False, True)))
)
def test_the_softening_facts_equal_the_trace(steps: int, uptrend: bool, hard: bool) -> None:
    trace = _real(
        (TraceCandidate(CandidatePath.PRICE_UPSIDE_MATRIX, 2, "PRICE_POSITION"),),
        mitigation_steps=steps,
        uptrend=uptrend,
        hard_overvalued=hard,
    )

    assert adapt_profit_taking_trace(trace).softening == SofteningFacts(steps, uptrend, hard)


# =================================================================================================
# F 順序・本物の ExitTrace
# =================================================================================================


def test_the_output_does_not_depend_on_the_order_of_the_candidates() -> None:
    items = [
        _candidate("PRICE_UPSIDE_MATRIX", 3),
        _candidate("FAIR_VALUE_STRONG", 2),
        _candidate("PARTIAL_CONDITIONS", 2, votes=("LOW_YIELD",)),
        _candidate("USER_TARGET_PRICE", 2),
        _candidate("BROKEN_PATH", 2),
    ]
    outputs = {
        repr(adapt_profit_taking_trace(_trace(list(order))))
        for order in itertools.permutations(items)
    }

    assert len(outputs) == 1  # 全順列(120 通り)で同じ出力


def test_a_real_exit_trace_is_adapted_without_recomputing_anything() -> None:
    trace = _real(
        (
            TraceCandidate(CandidatePath.PRICE_UPSIDE_MATRIX, 3, "PRICE_POSITION"),
            TraceCandidate(
                CandidatePath.PARTIAL_CONDITIONS,
                2,
                "OTHER_CONDITIONS",
                (VoteKind.TREND_WORSENING, VoteKind.LOW_YIELD),
            ),
            TraceCandidate(CandidatePath.USER_TARGET_RATE, 2, "PRICE_POSITION"),
        )
    )

    result = adapt_profit_taking_trace(trace)

    assert [(p.trigger_kind, int(p.claimed_strength), p.layer) for p in result.proposals] == [
        (TriggerKind.PRICE_UPSIDE_MATRIX, 3, ExitLayer.L2_EXPECTED_RETURN),
        (TriggerKind.PARTIAL_CONDITIONS, 2, ExitLayer.L3_PRICE_REGIME),
    ]
    assert result.pass_through == (PassThroughNotice(PassThroughKind.USER_TARGET_RATE),)
    assert result.softening == SofteningFacts(1, True, False)
    assert result.unmapped == ()


# =================================================================================================
# G Protocol であって import ではない・AST
# =================================================================================================


@dataclass(frozen=True)
class _FakeCandidate:
    path: str
    level: int
    origin: str
    votes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _FakeTrace:
    candidates: tuple[_FakeCandidate, ...]
    raw_level: int = 2
    origin: str = "PRICE_POSITION"
    mitigation_steps: int = 0
    uptrend: bool = False
    hard_overvalued: bool = False


def test_a_trace_type_that_does_not_import_profit_taking_works() -> None:
    """Protocol であって import ではない証拠: profit_taking を知らない別の型でも動く。"""
    fake = _FakeTrace((_FakeCandidate("FAIR_VALUE_STRONG", 3, "FAIR_VALUE_STRONG"),))

    result = adapt_profit_taking_trace(fake)

    assert [p.trigger_kind for p in result.proposals] == [TriggerKind.FAIR_VALUE_STRONG]


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_the_adapter_imports_only_the_standard_library_and_its_own_package() -> None:
    imports = _imports(_ADAPTER)

    assert imports
    for name in imports:
        assert name in {
            "__future__",
            "collections.abc",
            "dataclasses",
            "enum",
            "typing",
        } or name.startswith("jstock_advisor.domain.exit_architecture."), name
    forbidden = ("profit_taking", "domain.signals", "services", "infrastructure", "config", "cli")
    assert not [n for n in imports if any(word in n for word in forbidden)]
    assert "jstock_advisor.domain.exit_architecture.arbiter" in imports  # 空振りでない


def test_nothing_in_src_imports_the_adapter_yet() -> None:
    importers = [
        path.relative_to(_PACKAGE).as_posix()
        for path in sorted(_PACKAGE.rglob("*.py"))
        if path != _ADAPTER and "profit_taking_adapter" in path.read_text(encoding="utf-8")
    ]
    assert importers == []  # dormant(配線は後続の PR)


def test_the_adapter_neither_reads_a_clock_nor_has_a_broad_except() -> None:
    tree = ast.parse(_ADAPTER.read_text(encoding="utf-8"))
    calls = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert not calls & {"now", "utcnow", "today", "time", "monotonic"}
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert [ast.unparse(h.type) for h in handlers if h.type] == [
        "ContractViolationError"
    ]  # 広い except なし
    assert not [h for h in handlers if h.type is None]


def test_the_adapter_never_calls_the_arbiters_gate() -> None:
    called = {
        n.func.id
        for n in ast.walk(ast.parse(_ADAPTER.read_text(encoding="utf-8")))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert not called & {"build_candidates", "arbitrate", "select_winner", "apply_softening"}


# =================================================================================================
# H ゲートは変えない
# =================================================================================================


@pytest.mark.parametrize(
    ("path", "layers"),
    [
        ("FAIR_VALUE_STRONG", _layers(er=_er(exhaustion=None))),  # L2 が未確定
        ("PROFIT_PROTECTION_STRONG", _layers(regime=_regime(None))),  # L3 が未確定
        ("FULL_STRONG_CRITICAL", _layers(thesis=_thesis(None))),  # L1 が未確定
    ],
)
def test_the_arbiters_layer_gate_still_applies_to_legacy_engine_candidates(
    path: str, layers: Any
) -> None:
    """★ (ii) を含めない証拠 ★: adapter は source を付けるだけ。層が未確定なら従来どおり落ちる。

    『LEGACY_ENGINE は層の確定性ゲートを通さない』は USER の 1 行確認の後に、配線する PR で入れる。
    """
    kind = TriggerKind(path)
    result = adapt_profit_taking_trace(_trace([_candidate(path, 2)]))
    [proposal] = result.proposals
    assert proposal.source is ProposalSource.LEGACY_ENGINE

    candidates, trace = build_candidates(_inp([proposal], layers=layers), ())

    assert candidates == ()
    assert SuppressionReason.UNDETERMINED_INPUT in {s.reason for s in trace}
    assert kind is proposal.trigger_kind
