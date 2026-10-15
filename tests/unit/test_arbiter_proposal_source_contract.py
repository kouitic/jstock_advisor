"""`CandidateProposal.source`(`ProposalSource`)の契約テスト(Issue #878 PR-3b-0。dormant)。

設計: #878 rev6(…6195907794)・サブちゃんの再判定(…6195777307)。★ PR-3b-0 = field + docstring のみ ★

  S1  語彙は 2 値(LAYER / LEGACY_ENGINE)で、買い側の `CandidateSource` とは別の enum(名前を避けた)
  S2  ★ 既定は LAYER ★・既存の構築点(src)は source を渡さない = 『挙動不変』の根拠をテストに置く
  S3  ★ field は比較・hash に含まれる ★(出所が違う候補を同じと見なさない。`compare=False` にしない)
  S4  ★ `build_candidates` / `arbitrate` は source を見ない ★(値の定義だけ)。既存の判定結果は不変。
      とくに ★ LEGACY_ENGINE でも層が未確定なら落ちる ★(『層の確定性ゲートを通さない』は本 PR に
      含めない = 配線する PR で USER の 1 行確認を受ける。ここで『通る』ことを固定しない)
  S5  docstring の訂正(『決算直前』はエンジン本体に無く、サービス層の置き換え)
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from jstock_advisor.domain.entities import enums as entities_enums
from jstock_advisor.domain.exit_architecture import arbiter
from jstock_advisor.domain.exit_architecture.arbiter import (
    CandidateProposal,
    ProposalSource,
    build_candidates,
)
from jstock_advisor.domain.exit_architecture.decision import SuppressionReason
from jstock_advisor.domain.exit_architecture.vocabulary import (
    ExitClass,
    ExitLayer,
    Strength,
    TriggerKind,
)
from tests.unit.test_arbiter_contract import (
    _decide,
    _er,
    _inp,
    _layers,
    _prop,
    _regime,
    _rotation,
    _thesis,
)

_REPO = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO / "src" / "jstock_advisor"
_ARBITER = _PACKAGE / "domain" / "exit_architecture" / "arbiter.py"


# --- S1 語彙 -----------------------------------------------------------------------------------


def test_the_vocabulary_is_exactly_two_values() -> None:
    assert {m.name: m.value for m in ProposalSource} == {
        "LAYER": "LAYER",
        "LEGACY_ENGINE": "LEGACY_ENGINE",
    }


def test_the_name_does_not_collide_with_the_buy_side_enum() -> None:
    """買い側の `CandidateSource`(WATCHLIST / HOLDING / BOTH)とは別の enum(同名を避けた)。"""
    buy_side = entities_enums.CandidateSource
    assert ProposalSource is not buy_side
    assert {m.value for m in buy_side}.isdisjoint({m.value for m in ProposalSource})
    assert ProposalSource.__module__ == "jstock_advisor.domain.exit_architecture.arbiter"
    assert not hasattr(arbiter, "CandidateSource")  # arbiter に同名を作っていない
    assert "ProposalSource" in arbiter.__all__
    assert "CandidateSource" not in arbiter.__all__


# --- S2 既定値 ---------------------------------------------------------------------------------


def test_the_default_is_layer() -> None:
    assert _prop().source is ProposalSource.LAYER
    fields = {f.name: f for f in dataclasses.fields(CandidateProposal)}
    assert fields["source"].default is ProposalSource.LAYER
    assert list(fields) == [
        "trigger_kind",
        "exit_class",
        "claimed_strength",
        "layer",
        "evidence",
        "input_groups",
        "source",  # 既存の 6 field の後ろ(既存の位置引数の構築は変わらない)
    ]


def test_no_construction_in_src_passes_a_source() -> None:
    """既存の構築点(src)は source を渡さない = 全部が既定の LAYER(『挙動不変』の根拠)。"""
    offenders: list[str] = []
    seen_constructions = 0
    for path in sorted(_PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and _is_proposal_call(node)):
                continue
            seen_constructions += 1
            if len(node.args) > 6 or any(k.arg in {"source", None} for k in node.keywords):
                offenders.append(path.relative_to(_PACKAGE).as_posix())
    assert offenders == []
    assert seen_constructions >= 0  # src に構築点が無くても通る(adapter は PR-3b-1 以降)


def _is_proposal_call(node: ast.Call) -> bool:
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return name == "CandidateProposal"


def test_the_construction_check_is_not_vacuous() -> None:
    """検査が空振りでない: 位置引数 7 個・source= の構築は offender として数える。"""
    bad_positional = ast.parse("CandidateProposal(a, b, c, d, e, f, g)").body[0]
    bad_keyword = ast.parse("CandidateProposal(a, b, c, d, source=s)").body[0]
    for stmt in (bad_positional, bad_keyword):
        assert isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
        call = stmt.value
        assert _is_proposal_call(call)
        assert len(call.args) > 6 or any(k.arg in {"source", None} for k in call.keywords)


# --- S3 比較・hash -----------------------------------------------------------------------------


def test_the_source_takes_part_in_equality_and_hash() -> None:
    layer = _prop()
    legacy = dataclasses.replace(layer, source=ProposalSource.LEGACY_ENGINE)

    assert layer != legacy  # 出所が違う候補を『同じ』と見なさない
    assert len({layer, legacy}) == 2
    assert layer == dataclasses.replace(legacy, source=ProposalSource.LAYER)
    field = {f.name: f for f in dataclasses.fields(CandidateProposal)}["source"]
    assert (
        field.compare is True and field.repr is True
    )  # 観測用の除外(compare=False / repr=False)にしない
    assert field.hash is None  # compare に従う


# --- S4 判定は source を見ない -----------------------------------------------------------------


def _both(**kwargs: object) -> tuple[CandidateProposal, CandidateProposal]:
    layer = _prop(**kwargs)  # type: ignore[arg-type]
    return layer, dataclasses.replace(layer, source=ProposalSource.LEGACY_ENGINE)


@pytest.mark.parametrize(
    "strength", [Strength.WATCH, Strength.PARTIAL, Strength.FULL], ids=lambda s: s.name
)
def test_the_decision_is_identical_for_both_sources(strength: Strength) -> None:
    layer, legacy = _both(strength=strength, trigger=TriggerKind.PRICE_UPSIDE_MATRIX)

    decisions = [_decide(_inp([p])) for p in (layer, legacy)]

    assert decisions[0] == decisions[1]


def test_build_candidates_is_identical_for_both_sources() -> None:
    layer, legacy = _both()

    results = []
    for proposal in (layer, legacy):
        candidates, trace = build_candidates(_inp([proposal]), ())
        results.append(
            (
                [(c.strength, c.origin, c.proposal.trigger_kind) for c in candidates],
                list(trace),
            )
        )

    assert results[0] == results[1]
    assert results[0][0]  # 候補が実際に作られている(空振りでない)


@pytest.mark.parametrize(
    ("layer", "layers"),
    [
        (ExitLayer.L1_THESIS, _layers(thesis=_thesis(None))),
        (ExitLayer.L2_EXPECTED_RETURN, _layers(er=_er(exhaustion=None))),
        (ExitLayer.L3_PRICE_REGIME, _layers(regime=_regime(None))),
        (ExitLayer.L4_OPPORTUNITY_COST, _layers(rotation=_rotation(None))),
    ],
)
def test_an_undetermined_layer_still_drops_a_legacy_engine_candidate(
    layer: ExitLayer, layers: object
) -> None:
    """★ (ii) を本 PR に含めない証拠 ★: LEGACY_ENGINE でも、層が未確定なら従来どおり落ちる。

    『LEGACY_ENGINE は層の確定性ゲートを通さない』は売却判断の意味論そのもの = 配線する PR で
    USER の 1 行確認を受ける。ここで『通る』ことを固定しない。
    """
    legacy = dataclasses.replace(
        _prop(layer=layer, cls=ExitClass.VALUE_EXIT), source=ProposalSource.LEGACY_ENGINE
    )
    layer_source = dataclasses.replace(legacy, source=ProposalSource.LAYER)

    legacy_result = build_candidates(_inp([legacy], layers=layers), ())  # type: ignore[arg-type]
    layer_result = build_candidates(_inp([layer_source], layers=layers), ())  # type: ignore[arg-type]

    assert legacy_result[0] == () and layer_result[0] == ()  # どちらも候補が作られない
    assert SuppressionReason.UNDETERMINED_INPUT in {s.reason for s in legacy_result[1]}
    assert [s.reason for s in legacy_result[1]] == [s.reason for s in layer_result[1]]


def test_the_arbiter_module_never_reads_the_source_attribute() -> None:
    attributes = {
        n.attr
        for n in ast.walk(ast.parse(_ARBITER.read_text("utf-8")))
        if isinstance(n, ast.Attribute)
    }
    assert "source" not in attributes


# --- S5 docstring ------------------------------------------------------------------------------


def test_the_earnings_imminent_sentences_are_corrected() -> None:
    """『決算直前の降格』はエンジン本体に無く、サービス層の既存の置き換え(2 か所とも訂正)。"""
    text = _ARBITER.read_text(encoding="utf-8")

    assert "E2 の入力側の gate" not in text
    assert "ceiling の利用可否" not in text
    assert text.count("サービス層") >= 2
    assert text.count("決算直前は降格として") == 2  # 2 か所とも『降格として持たない』の表現は残る


def test_the_docstring_states_the_field_is_not_yet_read() -> None:
    doc = ProposalSource.__doc__ or ""
    assert "値を定義するだけ" in doc
    assert "build_candidates" in doc
    assert "CandidateSource" in doc  # 同名を避けた理由
    assert "USER の 1 行確認" in doc  # (ii) は配線する PR で
