"""ExpectedReturnResult -> ExpectedReturnVerdict の写像の契約テスト(Issue #601 PR-1b)。

## この PR の範囲(USER 承認: #122 issuecomment-6102822046 の『2. #601 PR-1b』)
  ExpectedReturnResult(`domain/valuation/expected_return.py`。#601 PR-1a)から、exit_architecture の
  ExpectedReturnVerdict への写像と、必要な C0 契約テストの更新。D2 の同時利用は、#903 と変更
  ファイルが重ならない条件の例外承認(記録 #601 …6102827464)。
  ★ 未決の式・重み・閾値は推測で実装しない(composite・年率換算・RAER は verdict に写さない)。

## 固定するもの
  (0) 先行(characterization): 写像が依存する既存の契約は変更しない
      (ExpectedReturnVerdict の検証・構成要素の 4 値・UndeterminedReason の値・ReasonCode の値)
  (1) 値は捏造せず、そのまま写す(丸め・符号の変更・補正なし。0 は値。負は切り上げない)
  (2) 未決は UNDETERMINED のまま: REVISION は素通し / RISK_ADJUSTMENT は常に未実装 /
      valuation_exhaustion・severely_low は決めない / composite・年率換算は写さない
  (3) 理由の対応: 全 ReasonCode について『写す / 写さない』を明示。写せない理由は推測せず拒否
  (4) 注記(Note)を黙って捨てない(優待が寄与 0 の INCOME / 価格が 1 セッション前の UPSIDE)
  (5) reliability(L0)は引数どおり。evidence は作らない
  (6) 構造: 時計・算術・買付余力・取得価格を持たない / 依存方向

時間意味論: evaluation_date は記録のみで、時計・営業日を読まない(TIME_SEMANTICS_IMPACT = NO)。
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.exit_architecture import expected_return_adapter as adapter
from jstock_advisor.domain.exit_architecture.determination import Determination
from jstock_advisor.domain.exit_architecture.expected_return_adapter import (
    to_expected_return_verdict,
)
from jstock_advisor.domain.exit_architecture.verdicts import (
    ComponentValue,
    ExpectedReturnComponent,
    ExpectedReturnVerdict,
)
from jstock_advisor.domain.exit_architecture.vocabulary import ReliabilityClass, UndeterminedReason
from jstock_advisor.domain.price_freshness import PriceFreshnessVerdict
from jstock_advisor.domain.valuation.expected_return import (
    ComponentResult,
    ExpectedReturnResult,
    FairValueInput,
    IncomeInput,
    Note,
    ReasonCode,
    compute_expected_return,
)
from jstock_advisor.domain.valuation.yield_calc import BenefitProgramState

_SRC = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"

# --- (0) 先行: 写像が依存する既存の契約は変更しない ---------------------------------------------


def test_the_expected_return_components_are_exactly_the_four() -> None:
    assert [c.value for c in ExpectedReturnComponent] == [
        "UPSIDE",
        "INCOME",
        "REVISION",
        "RISK_ADJUSTMENT",
    ]


def test_undetermined_reason_values_are_pinned() -> None:
    """写像の理由の対応は、この語彙を前提にする。増減したら対応表も見直す。"""
    assert sorted(r.value for r in UndeterminedReason) == [
        "COMPONENT_NOT_IMPLEMENTED",
        "COVERAGE_INSUFFICIENT",
        "GUARD_NOT_MET",
        "INPUT_MISSING",
        "NOT_EVALUATED",
        "RELIABILITY_UNUSABLE",
    ]


def test_expected_return_reason_codes_are_pinned() -> None:
    """写像は ReasonCode の全メンバーについて『写す / 写さない』を決める。増減したら見直す。"""
    assert {r.value for r in ReasonCode} == {
        "CURRENT_PRICE_UNAVAILABLE",
        "CURRENT_PRICE_STALE",
        "FAIR_VALUE_UNAVAILABLE",
        "FAIR_VALUE_NOT_USABLE",
        "DIVIDEND_UNKNOWN",
        "BENEFIT_UNVALUABLE",
        "REVISION_NOT_IMPLEMENTED",
        "COMPOSITION_RULE_NOT_DECIDED",
        "HORIZON_NOT_ESTIMATED",
    }


def _components(*, determined: bool) -> tuple[ComponentValue, ...]:
    return tuple(
        ComponentValue(
            component,
            Determination.of(1.0)
            if determined
            else Determination.undetermined(UndeterminedReason.INPUT_MISSING),
        )
        for component in ExpectedReturnComponent
    )


def _verdict(components: tuple[ComponentValue, ...], severely_low: Determination[bool]):  # type: ignore[no-untyped-def]
    return ExpectedReturnVerdict(
        components=components,
        valuation_exhaustion=Determination.undetermined(UndeterminedReason.NOT_EVALUATED),
        severely_low=severely_low,
        reliability=ReliabilityClass.RELIABLE,
    )


def test_a_verdict_rejects_duplicate_components() -> None:
    first = _components(determined=True)[0]
    with pytest.raises(ValueError, match="重複"):
        _verdict((first, first), Determination.undetermined(UndeterminedReason.NOT_EVALUATED))


def test_severely_low_cannot_be_determined_unless_every_component_is() -> None:
    with pytest.raises(ValueError, match="全ての構成要素が確定していない"):
        _verdict(_components(determined=False), Determination.of(True))
    # 全構成要素が確定していれば確定できる(= 検証が空振りでない)
    assert _verdict(_components(determined=True), Determination.of(True)).severely_low.is_determined


# --- 共通の組み立て ---

_DATE = date(2026, 10, 12)
_REVISION_NOT_IMPLEMENTED = ComponentResult(reasons=(ReasonCode.REVISION_NOT_IMPLEMENTED,))


def _er(
    *,
    upside: ComponentResult | None = None,
    income: ComponentResult | None = None,
    revision: ComponentResult | None = None,
    composite: ComponentResult | None = None,
    notes: tuple[Note, ...] = (),
) -> ExpectedReturnResult:
    return ExpectedReturnResult(
        model_version="er-components-v1",
        evaluation_date=_DATE,
        upside=upside if upside is not None else ComponentResult(value=20.0),
        income=income if income is not None else ComponentResult(value=2.0),
        revision=revision if revision is not None else _REVISION_NOT_IMPLEMENTED,
        composite=(
            composite
            if composite is not None
            else ComponentResult(reasons=(ReasonCode.COMPOSITION_RULE_NOT_DECIDED,))
        ),
        annualized=ComponentResult(reasons=(ReasonCode.HORIZON_NOT_ESTIMATED,)),
        notes=notes,
    )


def _verdict_of(result: ExpectedReturnResult) -> ExpectedReturnVerdict:
    return to_expected_return_verdict(result, reliability=ReliabilityClass.RELIABLE)


def _by_component(
    verdict: ExpectedReturnVerdict,
) -> dict[ExpectedReturnComponent, Determination[float]]:
    return {c.component: c.value for c in verdict.components}


# --- (1) 値は捏造せず、そのまま写す ---


def test_a_determined_value_is_carried_unchanged() -> None:
    result = compute_expected_return(
        evaluation_date=_DATE,
        current_price=Decimal("100"),
        price_freshness=PriceFreshnessVerdict.NORMAL,
        fair_value=FairValueInput(neutral=Decimal("123.456"), usable_for_trading_judgment=True),
        income=IncomeInput(2.34, None, BenefitProgramState.NO_PROGRAM),
    )

    components = _by_component(_verdict_of(result))

    # 丸め・符号の変更・補正をしない(float としてまったく同じ値)
    assert components[ExpectedReturnComponent.UPSIDE].value == result.upside.value
    assert components[ExpectedReturnComponent.INCOME].value == result.income.value
    assert result.upside.value is not None and result.income.value is not None


def test_zero_is_a_value_not_a_missing_value() -> None:
    zero = ComponentResult(value=0.0)

    components = _by_component(_verdict_of(_er(upside=zero, income=zero)))

    for component in (ExpectedReturnComponent.UPSIDE, ExpectedReturnComponent.INCOME):
        assert components[component].is_determined and components[component].value == 0.0


def test_a_negative_upside_is_kept_not_clipped() -> None:
    negative = ComponentResult(value=-20.0)

    components = _by_component(_verdict_of(_er(upside=negative)))

    assert components[ExpectedReturnComponent.UPSIDE].value == -20.0  # 0 へ切り上げない


def test_upside_and_income_are_not_swapped_when_their_values_differ() -> None:
    components = _by_component(
        _verdict_of(_er(upside=ComponentResult(value=11.0), income=ComponentResult(value=3.0)))
    )

    assert components[ExpectedReturnComponent.UPSIDE].value == 11.0
    assert components[ExpectedReturnComponent.INCOME].value == 3.0


def test_the_four_components_are_always_present_in_the_fixed_order() -> None:
    verdict = _verdict_of(_er())

    assert [c.component for c in verdict.components] == [
        ExpectedReturnComponent.UPSIDE,
        ExpectedReturnComponent.INCOME,
        ExpectedReturnComponent.REVISION,
        ExpectedReturnComponent.RISK_ADJUSTMENT,
    ]


# --- (2) 未決は UNDETERMINED のまま ---


def test_revision_is_passed_through_as_undetermined_today() -> None:
    revision = _by_component(_verdict_of(_er()))[ExpectedReturnComponent.REVISION]

    assert not revision.is_determined
    assert revision.reason is UndeterminedReason.COMPONENT_NOT_IMPLEMENTED


def test_revision_value_is_passed_through_once_it_exists() -> None:
    """PR-2 が値を作るようになった後も、写像は値をそのまま写す(今は構造の確認)。"""
    revision = _by_component(_verdict_of(_er(revision=ComponentResult(value=-3.5))))[
        ExpectedReturnComponent.REVISION
    ]

    assert revision.is_determined and revision.value == -3.5


def test_risk_adjustment_is_always_undetermined_not_implemented() -> None:
    for er in (
        _er(),
        _er(upside=ComponentResult(value=0.0)),
        _er(income=ComponentResult(value=9.0)),
    ):
        risk = _by_component(_verdict_of(er))[ExpectedReturnComponent.RISK_ADJUSTMENT]
        assert not risk.is_determined
        assert risk.reason is UndeterminedReason.COMPONENT_NOT_IMPLEMENTED


def test_valuation_exhaustion_and_severely_low_are_never_decided_here() -> None:
    """Exit 側が全構成要素の確定を条件に決める。写像は決めない(全構成要素が確定でも)。"""
    all_known = _er(revision=ComponentResult(value=1.0))

    for verdict in (_verdict_of(_er()), _verdict_of(all_known)):
        for determination in (verdict.valuation_exhaustion, verdict.severely_low):
            assert not determination.is_determined
            assert determination.reason is UndeterminedReason.NOT_EVALUATED


def test_composite_and_annualized_are_not_mapped_even_if_they_had_a_value() -> None:
    """式が未決。verdict の構成要素ではないので、composite に値があっても写さない。"""
    verdict = _verdict_of(_er(composite=ComponentResult(value=987.5)))

    assert {c.component for c in verdict.components} == set(ExpectedReturnComponent)
    values = [c.value.value for c in verdict.components if c.value.is_determined]
    assert 987.5 not in values
    assert "987.5" not in repr(verdict)


# --- (3) 理由の対応(全 ReasonCode について明示) ---

# 期待値は実装(_REASON_TO_UNDETERMINED)から導出せず、独立に書き下した literal
_EXPECTED_MAPPING = {
    ReasonCode.CURRENT_PRICE_UNAVAILABLE: UndeterminedReason.INPUT_MISSING,
    ReasonCode.FAIR_VALUE_UNAVAILABLE: UndeterminedReason.INPUT_MISSING,
    ReasonCode.DIVIDEND_UNKNOWN: UndeterminedReason.INPUT_MISSING,
    ReasonCode.BENEFIT_UNVALUABLE: UndeterminedReason.INPUT_MISSING,
    ReasonCode.CURRENT_PRICE_STALE: UndeterminedReason.RELIABILITY_UNUSABLE,
    ReasonCode.FAIR_VALUE_NOT_USABLE: UndeterminedReason.RELIABILITY_UNUSABLE,
    ReasonCode.REVISION_NOT_IMPLEMENTED: UndeterminedReason.COMPONENT_NOT_IMPLEMENTED,
}
_NOT_COMPONENT_REASONS = {ReasonCode.COMPOSITION_RULE_NOT_DECIDED, ReasonCode.HORIZON_NOT_ESTIMATED}


def test_every_reason_code_is_decided_to_be_mapped_or_not_mapped() -> None:
    """ReasonCode が増えたら、写す / 写さないをここで決めさせる(黙って漏れない)。"""
    assert set(_EXPECTED_MAPPING) | _NOT_COMPONENT_REASONS == set(ReasonCode)
    assert not set(_EXPECTED_MAPPING) & _NOT_COMPONENT_REASONS
    assert set(adapter._REASON_TO_UNDETERMINED) == set(ReasonCode)


@pytest.mark.parametrize(
    ("code", "expected"), list(_EXPECTED_MAPPING.items()), ids=lambda v: str(v)
)
def test_each_mapped_reason_code_lands_on_its_undetermined_reason(
    code: ReasonCode, expected: UndeterminedReason
) -> None:
    for slot in ("upside", "income", "revision"):
        verdict = _verdict_of(_er(**{slot: ComponentResult(reasons=(code,))}))
        component = ExpectedReturnComponent[slot.upper()]
        determination = _by_component(verdict)[component]
        assert not determination.is_determined
        assert determination.reason is expected, (slot, code)
        assert code.value in determination.detail  # 元の理由を detail に残す


@pytest.mark.parametrize("code", sorted(_NOT_COMPONENT_REASONS, key=str))
def test_a_reason_that_is_not_a_component_reason_is_rejected_not_guessed(code: ReasonCode) -> None:
    with pytest.raises(ValueError, match="写せない理由"):
        _verdict_of(_er(upside=ComponentResult(reasons=(code,))))


def test_with_several_reasons_the_representative_follows_a_fixed_priority() -> None:
    cases = [
        (
            (ReasonCode.CURRENT_PRICE_STALE, ReasonCode.FAIR_VALUE_UNAVAILABLE),
            UndeterminedReason.INPUT_MISSING,
        ),
        (
            (ReasonCode.FAIR_VALUE_NOT_USABLE, ReasonCode.CURRENT_PRICE_UNAVAILABLE),
            UndeterminedReason.INPUT_MISSING,
        ),
        (
            (ReasonCode.REVISION_NOT_IMPLEMENTED, ReasonCode.CURRENT_PRICE_STALE),
            UndeterminedReason.RELIABILITY_UNUSABLE,
        ),
    ]
    for codes, expected in cases:
        determination = _by_component(_verdict_of(_er(upside=ComponentResult(reasons=codes))))[
            ExpectedReturnComponent.UPSIDE
        ]
        assert determination.reason is expected
        for code in codes:  # 全件を detail に残す
            assert code.value in determination.detail


def test_a_mapped_reason_survives_next_to_an_unmapped_one() -> None:
    codes = (ReasonCode.CURRENT_PRICE_UNAVAILABLE, ReasonCode.COMPOSITION_RULE_NOT_DECIDED)

    determination = _by_component(_verdict_of(_er(upside=ComponentResult(reasons=codes))))[
        ExpectedReturnComponent.UPSIDE
    ]

    assert determination.reason is UndeterminedReason.INPUT_MISSING


def test_undetermined_details_carry_codes_never_numbers() -> None:
    """値・金額を detail に入れない(公開面・ログに実値が出ない)。"""
    er = _er(
        upside=ComponentResult(
            reasons=(ReasonCode.FAIR_VALUE_NOT_USABLE,),
            detail="fair_value_unusable_reason_code=SOME_CODE",
        ),
        income=ComponentResult(reasons=(ReasonCode.DIVIDEND_UNKNOWN,)),
    )

    verdict = _verdict_of(er)

    for component in verdict.components:
        assert not any(ch.isdigit() for ch in component.value.detail), component


# --- (4) 注記を黙って捨てない ---


def test_every_note_is_assigned_to_one_component() -> None:
    assert set(adapter._NOTE_COMPONENT) == set(Note)


def test_the_benefit_note_is_kept_on_the_income_value() -> None:
    """D-601-4: 『制度なし(未登録を含む)』で寄与 0 の INCOME は、その事実を区別して扱う。"""
    verdict = _verdict_of(_er(notes=(Note.BENEFIT_NO_PROGRAM_OR_UNREGISTERED,)))
    components = _by_component(verdict)

    assert components[ExpectedReturnComponent.INCOME].value == 2.0  # 値は変えない
    assert components[ExpectedReturnComponent.INCOME].detail == (
        "notes=BENEFIT_NO_PROGRAM_OR_UNREGISTERED"
    )
    assert components[ExpectedReturnComponent.UPSIDE].detail == ""  # 他の構成要素に付けない


def test_the_price_freshness_note_is_kept_on_the_upside_value() -> None:
    components = _by_component(_verdict_of(_er(notes=(Note.PRICE_FRESHNESS_WARNING,))))

    assert components[ExpectedReturnComponent.UPSIDE].detail == "notes=PRICE_FRESHNESS_WARNING"
    assert components[ExpectedReturnComponent.UPSIDE].value == 20.0
    assert components[ExpectedReturnComponent.INCOME].detail == ""


def test_no_notes_means_no_detail_on_determined_values() -> None:
    components = _by_component(_verdict_of(_er()))

    assert components[ExpectedReturnComponent.UPSIDE].detail == ""
    assert components[ExpectedReturnComponent.INCOME].detail == ""


# --- (5) reliability・evidence は adapter が決めない ---


@pytest.mark.parametrize("reliability", list(ReliabilityClass))
def test_reliability_is_exactly_what_the_caller_passed(reliability: ReliabilityClass) -> None:
    for confidence in (None, ConfidenceLevel.HIGH, ConfidenceLevel.LOW):
        result = replace(_er(), fair_value_confidence=confidence)

        verdict = to_expected_return_verdict(result, reliability=reliability)

        assert verdict.reliability is reliability  # confidence や注記から決めない


def test_reliability_is_required_not_defaulted() -> None:
    parameter = inspect.signature(to_expected_return_verdict).parameters["reliability"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_no_evidence_is_created() -> None:
    assert _verdict_of(_er(notes=tuple(Note))).evidence == ()


def test_the_mapping_is_pure_and_repeatable() -> None:
    er = _er(notes=(Note.PRICE_FRESHNESS_WARNING,))

    assert _verdict_of(er) == _verdict_of(er)


# --- (6) 構造 ---

_ADAPTER_PATH = _SRC / "domain" / "exit_architecture" / "expected_return_adapter.py"


def _adapter_tree() -> ast.Module:
    return ast.parse(_ADAPTER_PATH.read_text(encoding="utf-8"))


def test_adapter_reads_no_clock_and_does_no_arithmetic() -> None:
    tree = _adapter_tree()
    called = {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert not called & {"now", "today", "utcnow", "time", "monotonic", "round", "float", "sum"}
    operators = {type(n.op) for n in ast.walk(tree) if isinstance(n, ast.BinOp | ast.AugAssign)}
    assert not operators & {ast.Add, ast.Mult, ast.Div, ast.FloorDiv, ast.Pow, ast.Sub, ast.Mod}


def test_adapter_has_no_cash_cost_basis_or_composite_names() -> None:
    tree = _adapter_tree()
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.arg for n in ast.walk(tree) if isinstance(n, ast.arguments) for a in n.kwonlyargs}
    names |= {a.arg for n in ast.walk(tree) if isinstance(n, ast.arguments) for a in n.args}
    lowered = " ".join(sorted(n.lower() for n in names))
    for forbidden in ("available_cash", "acquisition", "average_price", "cost_basis", "capital"):
        assert forbidden not in lowered, forbidden
    # composite・annualized の値を読まない(式が未決)。注記・理由・構成要素の 3 つだけを読む
    assert "composite" not in lowered and "annualized" not in lowered


def test_adapter_imports_only_the_package_and_the_expected_return_module() -> None:
    imported = []
    for node in ast.walk(_adapter_tree()):
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
        elif isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
    own = sorted(m for m in imported if m.startswith("jstock_advisor"))
    assert own == [
        "jstock_advisor.domain.exit_architecture.determination",
        "jstock_advisor.domain.exit_architecture.verdicts",
        "jstock_advisor.domain.exit_architecture.vocabulary",
        "jstock_advisor.domain.valuation.expected_return",
    ]


def test_the_c0_allowlist_gives_the_adapter_exactly_one_external_module() -> None:
    """C0 の許可は module 名単位で必要な 1 件だけ。包括許可にしない(USER の条件)。"""
    from tests.unit import test_exit_architecture_c0 as c0

    allowed = c0._ALLOWED_EXTERNAL_TYPE_MODULES
    assert allowed["expected_return_adapter.py"] == (
        "jstock_advisor.domain.valuation.expected_return",
    )
    # 既存の許可は変更していない
    assert allowed["thesis_adapter.py"] == (
        "jstock_advisor.domain.entities.enums",
        "jstock_advisor.domain.entities.holding_decision",
    )
    assert sorted(allowed) == ["expected_return_adapter.py", "thesis_adapter.py"]
