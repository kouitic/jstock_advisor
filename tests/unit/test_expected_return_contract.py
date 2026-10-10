"""Expected Return の型と UPSIDE / INCOME の純粋計算の契約テスト(Issue #601 PR-1a)。

## この PR の範囲(USER 承認: #122 issuecomment-6100082651 の 3 節)
  Expected Return の型・UPSIDE / INCOME の純粋計算・入力欠測時の状態と理由コード。
  ★ composite の合成式・年率換算・Fair Value Revision の規則・重み・閾値は確定しない
  (UNDETERMINED で表現する)。既存の ExpectedReturnVerdict への写像は PR-1b(exit_architecture 側)。

## 固定するもの
  (0) 先行(characterization): INCOME が使う総合利回りの真理値表(#55)は変更しない
  (1) UPSIDE = (Fair Value〔中立〕/ 現在価格 - 1) × 100。現在価格基準。境界と符号
  (2) 欠測・不適格 -> 値を作らず理由コード(現在価格・鮮度・Fair Value・使用可否)。理由は累積
  (3) INCOME は #55 の真理値表(A〜H)に従う。None から 0 を推測しない
  (4) composite・annualized・revision は常に UNDETERMINED(全入力の格子で。値を捏造しない)
  (5) 構造: 取得価格・available_cash・clock を引数に持たない / 時計を読まない /
      誰からも import されない(dormant)

時間意味論: evaluation_date は呼び出し側が渡す記録用の値で、時計・営業日・timezone を読まない
(TIME_SEMANTICS_IMPACT = NO)。
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import itertools
import math
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.entities.valuation import FairValueUnusableReasonCode
from jstock_advisor.domain.price_freshness import PriceFreshnessVerdict
from jstock_advisor.domain.valuation import expected_return as er
from jstock_advisor.domain.valuation.expected_return import (
    ComponentResult,
    FairValueInput,
    IncomeInput,
    Note,
    ReasonCode,
    compute_expected_return,
    compute_income,
    compute_upside,
)
from jstock_advisor.domain.valuation.yield_calc import BenefitProgramState, compute_total_yield_pct

# --- (0) 先行: INCOME が使う既存の真理値表は変更しない(#55) -----------------------------------

_NO = BenefitProgramState.NO_PROGRAM
_VALUED = BenefitProgramState.VALUED
_UNVALUABLE = BenefitProgramState.UNVALUABLE

TRUTH_TABLE = [
    # (名前, 配当%, 優待%, 優待の状態, 期待する総合利回り)
    ("A 配当既知 + 制度なし", 2.0, None, _NO, 2.0),
    ("B 配当既知 + 評価可能", 2.0, 1.5, _VALUED, 3.5),
    ("C 配当既知 + 評価不能", 2.0, None, _UNVALUABLE, None),
    ("D 配当不明 + 制度なし", None, None, _NO, None),
    ("E 配当不明 + 評価可能", None, 1.5, _VALUED, None),
    ("F 配当不明 + 評価不能", None, None, _UNVALUABLE, None),
    ("G 配当 0(明示)+ 制度なし", 0.0, None, _NO, 0.0),
    ("H 配当 0(明示)+ 評価可能", 0.0, 1.5, _VALUED, 1.5),
]


@pytest.mark.parametrize(("name", "dividend", "benefit", "state", "expected"), TRUTH_TABLE)
def test_characterization_total_yield_truth_table_is_unchanged(
    name: str, dividend: float | None, benefit: float | None, state: BenefitProgramState, expected
) -> None:
    assert compute_total_yield_pct(dividend, benefit, benefit_state=state) == expected, name


# --- (1) UPSIDE -------------------------------------------------------------------------------

_NORMAL = PriceFreshnessVerdict.NORMAL
_WARNING = PriceFreshnessVerdict.WARNING
_HARD_STOP = PriceFreshnessVerdict.HARD_STOP
_INSUFFICIENT = PriceFreshnessVerdict.DATA_INSUFFICIENT


def fv(
    neutral: Decimal | None = Decimal("150"),
    *,
    usable: bool = True,
    code: FairValueUnusableReasonCode | None = None,
    confidence: ConfidenceLevel | None = None,
) -> FairValueInput:
    return FairValueInput(
        neutral=neutral,
        usable_for_trading_judgment=usable,
        unusable_reason_code=code,
        confidence=confidence,
    )


def upside(
    price: Decimal | None,
    neutral: Decimal | None,
    freshness: PriceFreshnessVerdict = _NORMAL,
    *,
    usable: bool = True,
    code: FairValueUnusableReasonCode | None = None,
) -> ComponentResult:
    return compute_upside(
        current_price=price,
        price_freshness=freshness,
        fair_value=fv(neutral, usable=usable, code=code),
    )


@pytest.mark.parametrize(
    ("price", "neutral", "expected"),
    [
        ("100", "150", 50.0),
        ("100", "100", 0.0),
        ("100", "80", -20.0),
        ("1234.5", "1234.5", 0.0),
        ("0.01", "0.02", 100.0),
        ("3", "4", 100.0 / 3.0),
        ("2500", "3000", 20.0),
    ],
)
def test_upside_is_fair_value_over_current_price_minus_one_in_percent(
    price: str, neutral: str, expected: float
) -> None:
    result = upside(Decimal(price), Decimal(neutral))
    assert result.is_determined
    assert result.value == pytest.approx(expected, rel=1e-12, abs=1e-12)
    assert result.reasons == ()


def test_upside_sign_flips_exactly_at_fair_value_equal_to_price() -> None:
    """境界: Fair Value が現在価格を上回る / 等しい / 下回るで、符号が 正 / 0 / 負 になる。"""
    above = upside(Decimal("100"), Decimal("100.01")).value
    equal = upside(Decimal("100"), Decimal("100")).value
    below = upside(Decimal("100"), Decimal("99.99")).value
    assert above is not None and equal == 0.0 and below is not None
    assert above > 0 > below


def test_upside_is_monotonic_in_fair_value_and_inverse_in_price() -> None:
    values = [upside(Decimal("100"), Decimal(n)).value for n in range(50, 301, 10)]
    assert all(a is not None and b is not None and a < b for a, b in itertools.pairwise(values))
    prices = [upside(Decimal(n), Decimal("200")).value for n in range(50, 301, 10)]
    assert all(a is not None and b is not None and a > b for a, b in itertools.pairwise(prices))


def test_upside_negative_values_are_kept_not_clamped() -> None:
    result = upside(Decimal("100"), Decimal("50"))
    assert result.value == pytest.approx(-50.0)


_BAD_DECIMALS = [None, Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")]


@pytest.mark.parametrize("bad", _BAD_DECIMALS)
def test_unusable_current_price_gives_no_value_and_a_reason(bad: Decimal | None) -> None:
    result = upside(bad, Decimal("150"))
    assert result.value is None
    assert result.reasons == (ReasonCode.CURRENT_PRICE_UNAVAILABLE,)


@pytest.mark.parametrize("bad", _BAD_DECIMALS)
def test_unusable_fair_value_gives_no_value_and_a_reason(bad: Decimal | None) -> None:
    result = upside(Decimal("100"), bad)
    assert result.value is None
    assert result.reasons == (ReasonCode.FAIR_VALUE_UNAVAILABLE,)


@pytest.mark.parametrize("verdict", [_HARD_STOP, _INSUFFICIENT])
def test_stale_price_verdicts_give_no_value(verdict: PriceFreshnessVerdict) -> None:
    result = upside(Decimal("100"), Decimal("150"), verdict)
    assert result.value is None
    assert result.reasons == (ReasonCode.CURRENT_PRICE_STALE,)


@pytest.mark.parametrize("verdict", [_NORMAL, _WARNING])
def test_fresh_or_warning_price_verdicts_keep_the_value(verdict: PriceFreshnessVerdict) -> None:
    assert upside(Decimal("100"), Decimal("150"), verdict).value == pytest.approx(50.0)


def test_fair_value_that_is_not_usable_gives_no_value_and_keeps_the_code() -> None:
    code = FairValueUnusableReasonCode.TOO_FEW_METHODS
    result = upside(Decimal("100"), Decimal("150"), usable=False, code=code)
    assert result.value is None
    assert result.reasons == (ReasonCode.FAIR_VALUE_NOT_USABLE,)
    assert code.value in result.detail


def test_reasons_accumulate_across_inputs_in_a_fixed_order() -> None:
    both_missing = upside(None, None)
    assert both_missing.reasons == (
        ReasonCode.CURRENT_PRICE_UNAVAILABLE,
        ReasonCode.FAIR_VALUE_UNAVAILABLE,
    )
    stale_and_unusable = upside(Decimal("100"), Decimal("150"), _HARD_STOP, usable=False)
    assert stale_and_unusable.reasons == (
        ReasonCode.CURRENT_PRICE_STALE,
        ReasonCode.FAIR_VALUE_NOT_USABLE,
    )


def test_a_missing_input_takes_priority_over_its_other_flags() -> None:
    """価格が無ければ鮮度の理由は足さない / Fair Value が無ければ使用可否の理由は足さない。"""
    assert upside(None, Decimal("150"), _HARD_STOP).reasons == (
        ReasonCode.CURRENT_PRICE_UNAVAILABLE,
    )
    assert upside(Decimal("100"), None, usable=False).reasons == (
        ReasonCode.FAIR_VALUE_UNAVAILABLE,
    )


# --- (3) INCOME(#55 の真理値表)--------------------------------------------------------------


@pytest.mark.parametrize(("name", "dividend", "benefit", "state", "expected"), TRUTH_TABLE)
def test_income_follows_the_total_yield_truth_table(
    name: str,
    dividend: float | None,
    benefit: float | None,
    state: BenefitProgramState,
    expected: float | None,
) -> None:
    result = compute_income(IncomeInput(dividend, benefit, state))
    if expected is None:
        assert result.value is None, name
        assert result.reasons, name
    else:
        assert result.value == pytest.approx(expected), name
        assert result.reasons == (), name


@pytest.mark.parametrize(
    ("name", "dividend", "benefit", "state", "reasons"),
    [
        ("C", 2.0, None, _UNVALUABLE, (ReasonCode.BENEFIT_UNVALUABLE,)),
        ("D", None, None, _NO, (ReasonCode.DIVIDEND_UNKNOWN,)),
        ("E", None, 1.5, _VALUED, (ReasonCode.DIVIDEND_UNKNOWN,)),
        (
            "F",
            None,
            None,
            _UNVALUABLE,
            (ReasonCode.DIVIDEND_UNKNOWN, ReasonCode.BENEFIT_UNVALUABLE),
        ),
        ("B'", 2.0, None, _VALUED, (ReasonCode.BENEFIT_UNVALUABLE,)),
    ],
)
def test_income_reasons_say_which_input_is_missing(
    name: str,
    dividend: float | None,
    benefit: float | None,
    state: BenefitProgramState,
    reasons: tuple[ReasonCode, ...],
) -> None:
    assert compute_income(IncomeInput(dividend, benefit, state)).reasons == reasons, name


def test_an_explicit_zero_dividend_is_a_determined_zero_not_a_missing_value() -> None:
    result = compute_income(IncomeInput(0.0, None, _NO))
    assert result.is_determined and result.value == 0.0


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, -0.5])
def test_non_finite_or_negative_yields_are_treated_as_unknown_not_as_zero(bad: float) -> None:
    assert compute_income(IncomeInput(bad, None, _NO)).reasons == (ReasonCode.DIVIDEND_UNKNOWN,)
    assert compute_income(IncomeInput(2.0, bad, _VALUED)).reasons == (
        ReasonCode.BENEFIT_UNVALUABLE,
    )


# --- (4) 確定しないものは常に UNDETERMINED ----------------------------------------------------

_PRICES: list[Decimal | None] = [None, Decimal("100")]
_FAIR_VALUES = [fv(None), fv(Decimal("150")), fv(Decimal("150"), usable=False)]
_INCOMES = [
    IncomeInput(None, None, _NO),
    IncomeInput(2.0, None, _NO),
    IncomeInput(2.0, 1.5, _VALUED),
    IncomeInput(2.0, None, _UNVALUABLE),
]


def _grid():  # type: ignore[no-untyped-def]
    return itertools.product(_PRICES, list(PriceFreshnessVerdict), _FAIR_VALUES, _INCOMES)


def test_revision_composite_and_annualized_are_always_undetermined() -> None:
    count = 0
    for price, freshness, fair_value, income in _grid():
        count += 1
        result = compute_expected_return(
            evaluation_date=date(2026, 1, 5),
            current_price=price,
            price_freshness=freshness,
            fair_value=fair_value,
            income=income,
        )
        assert result.revision == ComponentResult(reasons=(ReasonCode.REVISION_NOT_IMPLEMENTED,))
        assert result.composite == ComponentResult(
            reasons=(ReasonCode.COMPOSITION_RULE_NOT_DECIDED,)
        )
        assert result.annualized == ComponentResult(reasons=(ReasonCode.HORIZON_NOT_ESTIMATED,))
    assert count == len(_PRICES) * len(list(PriceFreshnessVerdict)) * 3 * 4


def test_composite_is_undetermined_even_when_upside_and_income_are_both_determined() -> None:
    result = compute_expected_return(
        evaluation_date=date(2026, 1, 5),
        current_price=Decimal("100"),
        price_freshness=_NORMAL,
        fair_value=fv(Decimal("150")),
        income=IncomeInput(2.0, 1.5, _VALUED),
    )
    assert result.upside.is_determined and result.income.is_determined
    assert result.composite.value is None
    assert result.model_version == er.MODEL_VERSION


def test_the_result_does_not_depend_on_the_evaluation_date_value() -> None:
    a = compute_expected_return(
        evaluation_date=date(2026, 1, 5),
        current_price=Decimal("100"),
        price_freshness=_NORMAL,
        fair_value=fv(Decimal("150")),
        income=IncomeInput(2.0, None, _NO),
    )
    b = compute_expected_return(
        evaluation_date=date(2030, 12, 31),
        current_price=Decimal("100"),
        price_freshness=_NORMAL,
        fair_value=fv(Decimal("150")),
        income=IncomeInput(2.0, None, _NO),
    )
    assert (a.upside, a.income, a.notes) == (b.upside, b.income, b.notes)
    assert (a.evaluation_date, b.evaluation_date) == (date(2026, 1, 5), date(2030, 12, 31))


def test_notes_record_the_limits_of_the_values() -> None:
    base = compute_expected_return(
        evaluation_date=date(2026, 1, 5),
        current_price=Decimal("100"),
        price_freshness=_NORMAL,
        fair_value=fv(Decimal("150")),
        income=IncomeInput(2.0, 1.5, _VALUED),
    )
    assert base.notes == ()
    warned = compute_expected_return(
        evaluation_date=date(2026, 1, 5),
        current_price=Decimal("100"),
        price_freshness=_WARNING,
        fair_value=fv(Decimal("150")),
        income=IncomeInput(2.0, None, _NO),
    )
    assert warned.notes == (
        Note.PRICE_FRESHNESS_WARNING,
        Note.BENEFIT_NO_PROGRAM_OR_UNREGISTERED,
    )
    assert warned.upside.is_determined  # 注記は値の有無を変えない


def test_unavailable_reasons_lists_the_primary_components_without_duplicates() -> None:
    result = compute_expected_return(
        evaluation_date=date(2026, 1, 5),
        current_price=None,
        price_freshness=_HARD_STOP,
        fair_value=fv(None),
        income=IncomeInput(None, None, _UNVALUABLE),
    )
    assert result.unavailable_reasons == (
        ReasonCode.CURRENT_PRICE_UNAVAILABLE,
        ReasonCode.FAIR_VALUE_UNAVAILABLE,
        ReasonCode.DIVIDEND_UNKNOWN,
        ReasonCode.BENEFIT_UNVALUABLE,
    )


def test_component_result_requires_exactly_one_of_value_or_reasons() -> None:
    with pytest.raises(ValueError):
        ComponentResult()
    with pytest.raises(ValueError):
        ComponentResult(value=1.0, reasons=(ReasonCode.DIVIDEND_UNKNOWN,))
    with pytest.raises(ValueError):
        ComponentResult(value=math.nan)
    with pytest.raises(ValueError):
        ComponentResult(reasons=(ReasonCode.DIVIDEND_UNKNOWN, ReasonCode.DIVIDEND_UNKNOWN))
    assert ComponentResult(value=0.0).is_determined  # 0.0 は確定した値(欠測ではない)


# --- (5) 構造 --------------------------------------------------------------------------------

_MODULE_PATH = Path(inspect.getsourcefile(er) or "")
_SRC = _MODULE_PATH.parents[3]
_FORBIDDEN_NAME_PARTS = (
    "purchase",
    "acquisition",
    "cost",
    "average",
    "cash",
    "clock",
    "now",
    "gain",
    "profit",
)


def test_no_acquisition_price_cash_or_clock_in_any_signature_or_field() -> None:
    names: list[str] = []
    for fn in (compute_expected_return, compute_upside, compute_income):
        names += list(inspect.signature(fn).parameters)
    for cls in (FairValueInput, IncomeInput, ComponentResult, er.ExpectedReturnResult):
        names += [f.name for f in dataclasses.fields(cls)]
    for name in names:
        assert not any(part in name.lower() for part in _FORBIDDEN_NAME_PARTS), name


def test_the_module_reads_no_clock_and_imports_no_services_or_exit_architecture() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    called = {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert not (called & {"now", "today", "utcnow", "time", "monotonic", "perf_counter"})
    imported = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    imported += [
        a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names
    ]
    forbidden = tuple(
        f"jstock_advisor.{n}"
        for n in ("services", "infrastructure", "lambda_handlers", "providers", "cli", "config")
    )
    for module in imported:
        assert not module.startswith(forbidden), module
        assert "exit_architecture" not in module, module


def test_only_the_allowed_modules_import_expected_return() -> None:
    """dormant: 現行のエンジンのどこからも import されない。例外は PR-1b の写像の module だけ。"""
    allowed = {"domain/exit_architecture/expected_return_adapter.py"}
    importers = set()
    for path in sorted((_SRC / "jstock_advisor").rglob("*.py")):
        if path == _MODULE_PATH:
            continue
        text = path.read_text(encoding="utf-8")
        if "valuation.expected_return" in text or "valuation import expected_return" in text:
            importers.add(path.relative_to(_SRC / "jstock_advisor").as_posix())
    assert importers <= allowed, importers


def test_a_usable_fair_value_with_low_confidence_keeps_the_value_and_records_the_confidence() -> (
    None
):
    """USER 決定 D-601-5: usable なら LOW でも値と信頼度を記録する。信頼度では値を変えない。"""
    results = {
        level: compute_expected_return(
            evaluation_date=date(2026, 1, 5),
            current_price=Decimal("100"),
            price_freshness=_NORMAL,
            fair_value=fv(Decimal("150"), confidence=level),
            income=IncomeInput(2.0, None, _NO),
        )
        for level in ConfidenceLevel
    }
    for result in results.values():
        assert result.upside.value == pytest.approx(50.0)  # 信頼度で UPSIDE を調整しない
    for level, result in results.items():
        assert result.fair_value_confidence is level
    unknown = compute_expected_return(
        evaluation_date=date(2026, 1, 5),
        current_price=Decimal("100"),
        price_freshness=_NORMAL,
        fair_value=fv(Decimal("150")),
        income=IncomeInput(2.0, None, _NO),
    )
    assert unknown.fair_value_confidence is None


def test_a_fair_value_that_is_not_usable_is_unavailable_even_when_the_confidence_is_high() -> None:
    result = upside(Decimal("100"), Decimal("150"), usable=False)
    assert result.value is None
    assert result.reasons == (ReasonCode.FAIR_VALUE_NOT_USABLE,)
