"""RAER の型・状態・比較可能性の契約テスト(Issue #602 PR-B1)。

## この PR の範囲(USER 承認: #122 issuecomment-6102822046 の『1. #602 PR-B1』)
  RAER の型・状態・比較可能性の契約のみ: 5 軸の適用・未適用状態の記録 / 欠測とリスクなしの区別 /
  comparison_key による比較可否の固定 / モデルバージョンの保持。
  ★ 未決の式・重み・閾値は実装しない。value は UNDETERMINED のまま(PR-B2 の数値計算は承認対象外)。

## 固定するもの
  (0) 先行(characterization): 本件が依存する既存の契約は変更しない
      (ExpectedReturnResult の field と composite の常時 UNDETERMINED / ConfidenceLevel の値 /
      本件の型名が raer.py の外に定義されていないこと)
  (1) 5 軸の適用・未適用の記録: 固定順で必ず 5 つ / 欠落した軸も UNAVAILABLE として残す /
      適用と不適用の両方を記録する(全 32 通りの部分集合)
  (2) 欠測とリスクなしの区別: risk_flags_available = False は『不明』で、空集合と区別する
  (3) comparison_key: model_version + 昇順の適用軸。順序に依らず、集合が違えば違う
  (4) RaerResult の不変条件: state と value・comparison_key・軸の記録の整合
  (5) 比較可否: key が違う・UNAVAILABLE は比較できない
  (6) ★ value は UNDETERMINED のまま ★: どの軸の組み合わせでも値を作らない(式は未決)
  (7) モデル版: 暫定の印を含む literal
  (8) 構造: 時計・式(乗除)・available_cash・取得単価を持たない / 依存方向 / dormant

時間意味論: evaluation_date は呼び出し側が渡す記録用の値で、時計・営業日・timezone を読まない
(TIME_SEMANTICS_IMPACT = NO)。
"""

from __future__ import annotations

import ast
import itertools
from collections.abc import Collection
from dataclasses import fields, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.price_freshness import PriceFreshnessVerdict
from jstock_advisor.domain.valuation.expected_return import (
    ExpectedReturnResult,
    FairValueInput,
    IncomeInput,
    ReasonCode,
    compute_expected_return,
)
from jstock_advisor.domain.valuation.raer import (
    AXIS_ORDER,
    MODEL_VERSION,
    PROVISIONAL_MARK,
    AxisAdjustment,
    AxisCode,
    AxisObservation,
    AxisReasonCode,
    AxisStatus,
    RaerNotComparableError,
    RaerReasonCode,
    RaerResult,
    RaerState,
    RiskFlag,
    are_comparable,
    evaluate_raer,
    make_comparison_key,
    require_comparable,
    to_raer_result,
)
from jstock_advisor.domain.valuation.yield_calc import BenefitProgramState

_SRC = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"
_MODULE_PATH = _SRC / "domain" / "valuation" / "raer.py"

# --- (0) 先行: 本件が依存する既存の契約は変更しない ---


def test_expected_return_result_has_exactly_the_fields_raer_records() -> None:
    """RAER は ExpectedReturnResult をそのまま保持する(再計算しない)。field の変更は本件にも効く"""
    assert [f.name for f in fields(ExpectedReturnResult)] == [
        "model_version",
        "evaluation_date",
        "upside",
        "income",
        "revision",
        "composite",
        "annualized",
        "notes",
        "fair_value_confidence",
    ]


def test_composite_is_always_undetermined_so_raer_has_no_value_to_build_on() -> None:
    """B1 の『value は UNDETERMINED のまま』の前提: composite は常に UNDETERMINED(式が未決)。"""
    for dividend, benefit_state in (
        (2.0, BenefitProgramState.NO_PROGRAM),
        (None, BenefitProgramState.NO_PROGRAM),
    ):
        result = compute_expected_return(
            evaluation_date=date(2026, 10, 12),
            current_price=Decimal("100"),
            price_freshness=PriceFreshnessVerdict.NORMAL,
            fair_value=FairValueInput(neutral=Decimal("120"), usable_for_trading_judgment=True),
            income=IncomeInput(dividend, None, benefit_state),
        )
        assert result.composite.value is None
        assert result.composite.reasons == (ReasonCode.COMPOSITION_RULE_NOT_DECIDED,)


def test_confidence_level_values_are_pinned() -> None:
    """RaerResult.confidence の型。値が増減したら本件も見直す。"""
    assert [m.name for m in ConfidenceLevel] == ["HIGH", "MEDIUM", "LOW"]


_RAER_TYPE_NAMES = {
    "AxisCode",
    "AxisStatus",
    "AxisAdjustment",
    "AxisReasonCode",
    "RaerReasonCode",
    "RaerState",
    "RaerResult",
    "RaerEvaluation",
    "RiskFlag",
    "RaerNotComparableError",
}


def test_the_raer_type_names_are_defined_only_in_raer_py() -> None:
    """同名の型が他の module に無い(#912 の SkipReason の衝突の再発防止。registry は名前で引く)。"""
    elsewhere = []
    for path in sorted(_SRC.rglob("*.py")):
        if path == _MODULE_PATH:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef) and node.name in _RAER_TYPE_NAMES:
                elsewhere.append(f"{path.relative_to(_SRC).as_posix()}:{node.name}")
    assert elsewhere == []


def test_compute_expected_return_supports_the_inputs_used_in_this_module() -> None:
    """(上の固定が空振りでないこと)ExpectedReturnResult を実際に作れる。"""
    result = compute_expected_return(
        evaluation_date=date(2026, 10, 12),
        current_price=Decimal("100"),
        price_freshness=PriceFreshnessVerdict.NORMAL,
        fair_value=FairValueInput(neutral=Decimal("120"), usable_for_trading_judgment=True),
        income=IncomeInput(2.0, None, BenefitProgramState.NO_PROGRAM),
    )
    assert result.upside.value == pytest.approx(20.0)


# --- 共通の組み立て ---

_AXES = list(AxisCode)
_ALL_SUBSETS = [
    frozenset(c) for r in range(len(_AXES) + 1) for c in itertools.combinations(_AXES, r)
]
_DATE = date(2026, 10, 12)


def _expected_return(*, upside_known: bool = True) -> ExpectedReturnResult:
    return compute_expected_return(
        evaluation_date=_DATE,
        current_price=Decimal("100") if upside_known else None,
        price_freshness=PriceFreshnessVerdict.NORMAL,
        fair_value=FairValueInput(neutral=Decimal("120"), usable_for_trading_judgment=True),
        income=IncomeInput(2.0, None, BenefitProgramState.NO_PROGRAM),
    )


def _observations(applied: frozenset[AxisCode]) -> dict[AxisCode, AxisObservation]:
    return {
        axis: (
            AxisObservation(AxisStatus.APPLIED)
            if axis in applied
            else AxisObservation(AxisStatus.UNAVAILABLE, (AxisReasonCode.INPUT_MISSING,))
        )
        for axis in _AXES
    }


def _evaluate(
    applied: frozenset[AxisCode] = frozenset(_AXES),
    risk_flags: Collection[RiskFlag] | None = frozenset(),
    *,
    axes: dict[AxisCode, AxisObservation] | None = None,
    expected_return: ExpectedReturnResult | None = None,
):  # type: ignore[no-untyped-def]
    return evaluate_raer(
        evaluation_date=_DATE,
        expected_return=expected_return or _expected_return(),
        axes=axes if axes is not None else _observations(applied),
        risk_flags=risk_flags,
    )


_NINE = frozenset(_AXES) - {AxisCode.DATA_QUALITY}
_FULL_KEY = "raer-structure-v1-provisional-axes-y|" + ",".join(sorted(a.value for a in _NINE))


def _result(**overrides: object) -> RaerResult:
    """不変条件を満たす PROVISIONAL(軸 4 だけ適用できない)を土台に、一部を上書きして作る。"""
    values: dict[str, object] = {
        "state": RaerState.PROVISIONAL,
        "value": Decimal("5.5"),
        "confidence": ConfidenceLevel.MEDIUM,
        "risk_flags": frozenset({"cyclical_industry"}),
        "risk_flags_available": True,
        "model_version": MODEL_VERSION,
        "comparison_key": _FULL_KEY,
        "axes_applied": frozenset(a.value for a in _NINE),
        "axes_not_applied": frozenset({AxisCode.DATA_QUALITY.value}),
        "reason_codes": ("DATA_QUALITY:AXIS_NOT_IMPLEMENTED",),
    }
    values.update(overrides)
    return RaerResult(**values)  # type: ignore[arg-type]


# --- (1) 5 軸の適用・未適用の記録 ---


def test_the_five_axes_and_their_fixed_order_are_pinned() -> None:
    assert [a.value for a in AXIS_ORDER] == [
        "FAIR_VALUE_CONFIDENCE",
        "COMPANY_QUALITY",
        "BUSINESS_RISK",
        "DATA_QUALITY",
        "EARNINGS_TREND",
    ]


def test_the_evaluation_always_has_the_five_axes_in_the_fixed_order() -> None:
    shuffled = dict(reversed(list(_observations(frozenset(_AXES)).items())))

    evaluation = _evaluate(axes=shuffled)

    assert tuple(a.axis for a in evaluation.axes) == AXIS_ORDER  # 渡し順に依らない


@pytest.mark.parametrize("missing", _AXES)
def test_an_axis_that_was_not_provided_is_kept_as_unavailable(missing: AxisCode) -> None:
    axes = _observations(frozenset(_AXES))
    del axes[missing]

    evaluation = _evaluate(axes=axes)

    [dropped] = [a for a in evaluation.axes if a.axis is missing]
    assert dropped.status is AxisStatus.UNAVAILABLE
    assert dropped.reasons == (AxisReasonCode.AXIS_NOT_PROVIDED,)
    assert missing in evaluation.axes_not_applied and missing not in evaluation.axes_applied
    assert len(evaluation.axes) == 5  # 黙って捨てない


@pytest.mark.parametrize(
    "applied", _ALL_SUBSETS, ids=lambda s: f"{len(s)}axes-{sorted(a.name[:3] for a in s)}"
)
def test_both_applied_and_not_applied_axes_are_recorded_for_every_subset(
    applied: frozenset[AxisCode],
) -> None:
    flags = frozenset() if AxisCode.BUSINESS_RISK in applied else None
    evaluation = _evaluate(applied, risk_flags=flags)
    result = to_raer_result(evaluation)

    expected_applied = tuple(sorted(a.value for a in applied))
    expected_not_applied = tuple(sorted(a.value for a in set(_AXES) - applied))
    assert tuple(sorted(a.value for a in evaluation.axes_applied)) == expected_applied
    assert tuple(sorted(a.value for a in evaluation.axes_not_applied)) == expected_not_applied
    # 射影した境界にも、片方だけでなく両方を残す(昇順の tuple で比べる = 集合どうしの空振りを避ける)
    assert tuple(sorted(result.axes_applied)) == expected_applied
    assert tuple(sorted(result.axes_not_applied)) == expected_not_applied


def test_the_applied_and_not_applied_records_are_not_interchangeable() -> None:
    one = _evaluate(frozenset({AxisCode.FAIR_VALUE_CONFIDENCE}), risk_flags=None)
    result = to_raer_result(one)

    assert result.axes_applied == {"FAIR_VALUE_CONFIDENCE"}
    assert result.axes_not_applied == {
        "COMPANY_QUALITY",
        "BUSINESS_RISK",
        "DATA_QUALITY",
        "EARNINGS_TREND",
    }


def test_axis_adjustment_invariants() -> None:
    AxisAdjustment(AxisCode.COMPANY_QUALITY, AxisStatus.APPLIED)
    AxisAdjustment(
        AxisCode.COMPANY_QUALITY, AxisStatus.NOT_APPLIED, (AxisReasonCode.NOT_APPLICABLE_TO_STOCK,)
    )
    with pytest.raises(ValueError, match="理由を持たない"):
        AxisAdjustment(
            AxisCode.COMPANY_QUALITY, AxisStatus.APPLIED, (AxisReasonCode.INPUT_MISSING,)
        )
    with pytest.raises(ValueError, match="理由が要る"):
        AxisAdjustment(AxisCode.COMPANY_QUALITY, AxisStatus.UNAVAILABLE)
    with pytest.raises(ValueError, match="理由が要る"):
        AxisAdjustment(AxisCode.COMPANY_QUALITY, AxisStatus.NOT_APPLIED)
    with pytest.raises(ValueError, match="重複"):
        AxisAdjustment(
            AxisCode.COMPANY_QUALITY,
            AxisStatus.UNAVAILABLE,
            (AxisReasonCode.INPUT_MISSING, AxisReasonCode.INPUT_MISSING),
        )


def test_an_unknown_axis_is_rejected() -> None:
    axes: dict = _observations(frozenset(_AXES))  # type: ignore[type-arg]
    axes["MADE_UP_AXIS"] = AxisObservation(AxisStatus.APPLIED)

    with pytest.raises(ValueError, match="未知の軸"):
        _evaluate(axes=axes)


def test_the_evaluation_date_must_match_the_expected_return() -> None:
    with pytest.raises(ValueError, match="評価日"):
        evaluate_raer(
            evaluation_date=date(2026, 10, 13),
            expected_return=_expected_return(),
            axes=_observations(frozenset(_AXES)),
            risk_flags=frozenset(),
        )


def test_the_evaluation_rejects_inconsistent_risk_records() -> None:
    """直接組み立てても、risk の記録と軸 3 の状態の食い違いは作れない(欠測を空集合にしない)。"""
    observed = _evaluate(risk_flags={RiskFlag.CYCLICAL_INDUSTRY})
    unknown = _evaluate(risk_flags=None)

    # 軸 3 が適用されていないのに『観測した』と主張する
    with pytest.raises(ValueError, match="軸 3 が適用されたときだけ"):
        replace(unknown, risk_flags_available=True)
    # 軸 3 が適用されているのに『不明』と主張する
    with pytest.raises(ValueError, match="軸 3 が適用されたときだけ"):
        replace(observed, risk_flags_available=False)
    # 観測が無いのに語を持つ
    with pytest.raises(ValueError, match="不明を空集合にしない"):
        replace(unknown, risk_flags=frozenset({RiskFlag.CYCLICAL_INDUSTRY}))


def test_the_evaluation_rejects_missing_or_duplicate_reasons() -> None:
    evaluation = _evaluate()

    with pytest.raises(ValueError, match="合成式が未決"):
        replace(evaluation, reasons=())
    with pytest.raises(ValueError, match="重複"):
        replace(
            evaluation,
            reasons=(
                RaerReasonCode.COMPOSITION_RULE_NOT_DECIDED,
                RaerReasonCode.COMPOSITION_RULE_NOT_DECIDED,
            ),
        )


def test_the_evaluation_rejects_a_tampered_axis_list() -> None:
    evaluation = _evaluate()
    with pytest.raises(ValueError, match="固定の順序"):
        replace(evaluation, axes=tuple(reversed(evaluation.axes)))
    with pytest.raises(ValueError, match="固定の順序"):
        replace(evaluation, axes=evaluation.axes[:4])


# --- (2) 欠測とリスクなしの区別 ---


def test_the_risk_flag_vocabulary_is_exactly_the_six_words() -> None:
    assert sorted(f.value for f in RiskFlag) == [
        "cyclical_industry",
        "earnings_within_3_business_days",
        "earnings_within_7_business_days",
        "major_customer_dependency",
        "small_cap_or_low_liquidity",
        "temporary_earnings_boost_risk",
    ]


def test_observed_flags_are_kept_and_marked_available() -> None:
    flags = {RiskFlag.CYCLICAL_INDUSTRY, RiskFlag.SMALL_CAP_OR_LOW_LIQUIDITY}

    result = to_raer_result(_evaluate(risk_flags=flags))

    assert result.risk_flags_available is True
    assert tuple(sorted(result.risk_flags)) == ("cyclical_industry", "small_cap_or_low_liquidity")


def test_an_observed_empty_set_means_checked_and_no_risk() -> None:
    result = to_raer_result(_evaluate(risk_flags=frozenset()))

    assert result.risk_flags == frozenset()
    assert result.risk_flags_available is True  # 『確認したがリスクなし』


def test_a_missing_risk_observation_is_unknown_not_no_risk() -> None:
    """軸 3 が適用とされても観測(risk_flags)が無ければ『リスクなし』にせず UNAVAILABLE にする。"""
    unknown = _evaluate(risk_flags=None)
    observed_none = _evaluate(risk_flags=frozenset())

    assert unknown.risk_flags_available is False
    assert unknown.risk_flags == frozenset()
    [risk_axis] = [a for a in unknown.axes if a.axis is AxisCode.BUSINESS_RISK]
    assert risk_axis.status is AxisStatus.UNAVAILABLE
    assert risk_axis.reasons == (AxisReasonCode.RISK_FLAGS_NOT_OBSERVED,)
    # 空集合の『リスクなし』とは、利用側から区別できる
    assert (unknown.risk_flags, unknown.risk_flags_available) != (
        observed_none.risk_flags,
        observed_none.risk_flags_available,
    )
    assert AxisCode.BUSINESS_RISK in observed_none.axes_applied
    assert AxisCode.BUSINESS_RISK not in unknown.axes_applied


@pytest.mark.parametrize("status", [AxisStatus.NOT_APPLIED, AxisStatus.UNAVAILABLE])
def test_flags_without_an_applied_risk_axis_are_rejected(status: AxisStatus) -> None:
    axes = _observations(frozenset(_AXES))
    axes[AxisCode.BUSINESS_RISK] = AxisObservation(status, (AxisReasonCode.INPUT_MISSING,))

    with pytest.raises(ValueError, match="risk_flags が渡された"):
        _evaluate(axes=axes, risk_flags={RiskFlag.CYCLICAL_INDUSTRY})
    # 観測の無い軸に空集合が渡されても『リスクなし』にならない
    result = to_raer_result(_evaluate(axes=axes, risk_flags=frozenset()))
    assert result.risk_flags_available is False


def test_a_risk_axis_that_was_not_provided_is_unknown() -> None:
    axes = _observations(frozenset(_AXES))
    del axes[AxisCode.BUSINESS_RISK]

    result = to_raer_result(_evaluate(axes=axes, risk_flags=frozenset()))

    assert result.risk_flags_available is False and result.risk_flags == frozenset()


def test_only_the_vocabulary_is_accepted_as_a_risk_flag() -> None:
    with pytest.raises(ValueError, match="語彙"):
        _evaluate(risk_flags={"made_up_flag"})  # type: ignore[arg-type]


def test_a_result_cannot_claim_flags_it_did_not_observe() -> None:
    with pytest.raises(ValueError, match="不明を空集合にしない"):
        _result(
            risk_flags=frozenset({"cyclical_industry"}),
            risk_flags_available=False,
            axes_applied=frozenset(a.value for a in _AXES if a is not AxisCode.BUSINESS_RISK),
            axes_not_applied=frozenset({AxisCode.BUSINESS_RISK.value}),
            comparison_key=make_comparison_key(
                MODEL_VERSION, [a for a in _AXES if a is not AxisCode.BUSINESS_RISK]
            ),
        )


def test_risk_flags_available_must_agree_with_the_applied_risk_axis() -> None:
    with pytest.raises(ValueError, match="軸 3"):
        _result(risk_flags_available=False, risk_flags=frozenset())  # 軸 3 は適用済み
    unapplied = frozenset(a.value for a in _AXES if a is not AxisCode.BUSINESS_RISK)
    with pytest.raises(ValueError, match="軸 3"):
        _result(
            risk_flags_available=True,  # 軸 3 は未適用
            axes_applied=unapplied,
            axes_not_applied=frozenset({AxisCode.BUSINESS_RISK.value}),
            comparison_key=make_comparison_key(MODEL_VERSION, unapplied),
        )


def test_a_flag_outside_the_vocabulary_is_rejected_in_a_result() -> None:
    with pytest.raises(ValueError, match="語彙外"):
        _result(risk_flags=frozenset({"made_up_flag"}))


# --- (3) comparison_key ---


def test_comparison_key_has_a_pinned_literal_form() -> None:
    key = make_comparison_key(
        "raer-structure-v1-provisional-axes-y",
        [AxisCode.COMPANY_QUALITY, AxisCode.FAIR_VALUE_CONFIDENCE],
    )

    assert key == "raer-structure-v1-provisional-axes-y|COMPANY_QUALITY,FAIR_VALUE_CONFIDENCE"


def test_comparison_key_does_not_depend_on_order_duplicates_or_type() -> None:
    expected = make_comparison_key(
        MODEL_VERSION, [AxisCode.COMPANY_QUALITY, AxisCode.EARNINGS_TREND]
    )
    for variant in (
        [AxisCode.EARNINGS_TREND, AxisCode.COMPANY_QUALITY],
        [AxisCode.COMPANY_QUALITY, AxisCode.EARNINGS_TREND, AxisCode.COMPANY_QUALITY],
        ["EARNINGS_TREND", "COMPANY_QUALITY"],
        {AxisCode.COMPANY_QUALITY, AxisCode.EARNINGS_TREND},
    ):
        assert make_comparison_key(MODEL_VERSION, variant) == expected


def test_every_distinct_set_of_applied_axes_has_a_distinct_key() -> None:
    nonempty = [s for s in _ALL_SUBSETS if s]

    keys = {make_comparison_key(MODEL_VERSION, s) for s in nonempty}

    assert len(keys) == len(nonempty) == 31  # 適用軸の集合が違えば違う値


def test_comparison_key_changes_with_the_model_version() -> None:
    axes = [AxisCode.COMPANY_QUALITY]
    assert make_comparison_key("model-a", axes) != make_comparison_key("model-b", axes)


def test_comparison_key_rejects_no_axes_and_unknown_axes() -> None:
    with pytest.raises(ValueError, match="適用軸が無い"):
        make_comparison_key(MODEL_VERSION, [])
    with pytest.raises(ValueError, match="未知の軸"):
        make_comparison_key(MODEL_VERSION, ["MADE_UP_AXIS"])


def test_the_evaluation_key_comes_from_the_model_version_and_the_applied_axes_only() -> None:
    a = _evaluate(frozenset({AxisCode.COMPANY_QUALITY, AxisCode.EARNINGS_TREND}), risk_flags=None)
    b = _evaluate(frozenset({AxisCode.EARNINGS_TREND, AxisCode.COMPANY_QUALITY}), risk_flags=None)

    assert (
        a.comparison_key
        == b.comparison_key
        == ("raer-structure-v1-provisional-axes-y|COMPANY_QUALITY,EARNINGS_TREND")
    )
    assert _evaluate(frozenset(), risk_flags=None).comparison_key is None  # 適用軸が無い


# --- (4) RaerResult の不変条件 ---


def test_a_well_formed_provisional_and_available_result_can_be_built() -> None:
    provisional = _result()
    available = _result(
        state=RaerState.AVAILABLE,
        axes_applied=frozenset(a.value for a in _AXES),
        axes_not_applied=frozenset(),
        comparison_key=make_comparison_key(MODEL_VERSION, _AXES),
        reason_codes=(),
    )
    unavailable = _result(
        state=RaerState.UNAVAILABLE,
        value=None,
        confidence=None,
        comparison_key=None,
        reason_codes=("COMPOSITION_RULE_NOT_DECIDED",),
    )

    assert (provisional.state, available.state, unavailable.state) == (
        RaerState.PROVISIONAL,
        RaerState.AVAILABLE,
        RaerState.UNAVAILABLE,
    )


_INVALID = {
    "unavailable-with-value": (
        {"state": RaerState.UNAVAILABLE, "comparison_key": None},
        "value も",
    ),
    "unavailable-with-key": (
        {"state": RaerState.UNAVAILABLE, "value": None, "confidence": None},
        "value も",
    ),
    "unavailable-with-confidence": (
        {"state": RaerState.UNAVAILABLE, "value": None, "comparison_key": None},
        "confidence",
    ),
    "unavailable-without-reason": (
        {
            "state": RaerState.UNAVAILABLE,
            "value": None,
            "confidence": None,
            "comparison_key": None,
            "reason_codes": (),
        },
        "理由が要る",
    ),
    "provisional-without-value": ({"value": None}, "有限の value"),
    "provisional-with-nan": ({"value": Decimal("NaN")}, "有限の value"),
    "provisional-with-infinity": ({"value": Decimal("Infinity")}, "有限の value"),
    "provisional-with-wrong-key": ({"comparison_key": "model|COMPANY_QUALITY"}, "comparison_key"),
    "provisional-without-key": ({"comparison_key": None}, "comparison_key"),
    "provisional-with-everything-applied": (
        {
            "axes_applied": frozenset(a.value for a in _AXES),
            "axes_not_applied": frozenset(),
            "comparison_key": make_comparison_key(MODEL_VERSION, _AXES),
        },
        "適用できない軸があるとき",
    ),
    "provisional-without-the-mark": (
        {
            "model_version": "raer-structure-v1",
            "comparison_key": make_comparison_key("raer-structure-v1", _NINE),
        },
        "暫定の印",
    ),
    "provisional-without-reasons": ({"reason_codes": ()}, "理由が要る"),
    "available-with-not-applied-axis": ({"state": RaerState.AVAILABLE}, "5 軸すべて"),
    "overlapping-axes": (
        {
            "axes_not_applied": frozenset(
                {AxisCode.COMPANY_QUALITY.value, AxisCode.DATA_QUALITY.value}
            )
        },
        "重なっている",
    ),
    "missing-axis": ({"axes_not_applied": frozenset()}, "5 軸すべて"),
    "unknown-axis": (
        {"axes_not_applied": frozenset({"MADE_UP_AXIS"})},
        "5 軸すべて",
    ),
    "duplicate-reasons": ({"reason_codes": ("A", "A")}, "重複"),
    "no-applied-axes": (
        {
            "axes_applied": frozenset(),
            "axes_not_applied": frozenset(a.value for a in _AXES),
            "risk_flags": frozenset(),
            "risk_flags_available": False,
            "comparison_key": None,
        },
        "適用した軸",
    ),
}


@pytest.mark.parametrize(("overrides", "message"), list(_INVALID.values()), ids=list(_INVALID))
def test_an_inconsistent_result_cannot_be_built(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _result(**overrides)


# --- (5) 比較可否 ---


def test_two_results_with_the_same_key_are_comparable() -> None:
    a, b = _result(value=Decimal("5.5")), _result(value=Decimal("-1.25"))

    assert are_comparable(a, b) and are_comparable(b, a)
    require_comparable(a, b)  # 例外にならない


def test_results_with_different_applied_axes_are_not_comparable() -> None:
    other_axes = frozenset(
        a.value for a in _AXES if a not in {AxisCode.DATA_QUALITY, AxisCode.EARNINGS_TREND}
    )
    other = _result(
        axes_applied=other_axes,
        axes_not_applied=frozenset({AxisCode.DATA_QUALITY.value, AxisCode.EARNINGS_TREND.value}),
        comparison_key=make_comparison_key(MODEL_VERSION, other_axes),
    )

    assert not are_comparable(_result(), other) and not are_comparable(other, _result())
    with pytest.raises(RaerNotComparableError):
        require_comparable(_result(), other)


def test_results_with_different_model_versions_are_not_comparable() -> None:
    version = "raer-structure-v2" + PROVISIONAL_MARK
    other = _result(model_version=version, comparison_key=make_comparison_key(version, _NINE))

    assert not are_comparable(_result(), other)
    with pytest.raises(RaerNotComparableError):
        require_comparable(_result(), other)


def test_an_unavailable_result_is_never_comparable_even_with_itself() -> None:
    unavailable = _result(
        state=RaerState.UNAVAILABLE,
        value=None,
        confidence=None,
        comparison_key=None,
        reason_codes=("COMPOSITION_RULE_NOT_DECIDED",),
    )

    assert not are_comparable(unavailable, unavailable)
    assert not are_comparable(unavailable, _result()) and not are_comparable(_result(), unavailable)
    with pytest.raises(RaerNotComparableError):
        require_comparable(unavailable, _result())


def test_the_refusal_message_does_not_carry_values() -> None:
    other = _result(
        value=Decimal("9876.54321"),
        model_version="x" + PROVISIONAL_MARK,
        comparison_key=make_comparison_key("x" + PROVISIONAL_MARK, _NINE),
    )
    with pytest.raises(RaerNotComparableError) as info:
        require_comparable(_result(value=Decimal("1234.5678")), other)
    assert "1234" not in str(info.value) and "9876" not in str(info.value)


# --- (6) value は UNDETERMINED のまま(B1 は値を作らない) ---


@pytest.mark.parametrize("applied", _ALL_SUBSETS, ids=lambda s: f"{len(s)}axes")
def test_evaluation_never_produces_a_value_for_any_set_of_applied_axes(
    applied: frozenset[AxisCode],
) -> None:
    flags = frozenset() if AxisCode.BUSINESS_RISK in applied else None
    evaluation = _evaluate(applied, risk_flags=flags)
    result = to_raer_result(evaluation)

    assert evaluation.state is RaerState.UNAVAILABLE
    assert result.state is RaerState.UNAVAILABLE
    assert result.value is None and result.comparison_key is None and result.confidence is None
    assert RaerReasonCode.COMPOSITION_RULE_NOT_DECIDED.value in result.reason_codes


def test_all_axes_applied_still_yields_no_value() -> None:
    """全軸が適用できても値は作らない(式が未決。AVAILABLE を勝手に出さない)。"""
    result = to_raer_result(_evaluate(frozenset(_AXES)))

    assert result.state is RaerState.UNAVAILABLE and result.value is None


def test_the_reasons_of_a_projection_are_deterministic_and_axis_qualified() -> None:
    applied = frozenset({AxisCode.FAIR_VALUE_CONFIDENCE, AxisCode.BUSINESS_RISK})

    result = to_raer_result(_evaluate(applied, risk_flags=frozenset()))

    assert result.reason_codes == (
        "COMPOSITION_RULE_NOT_DECIDED",
        "COMPANY_QUALITY:INPUT_MISSING",
        "DATA_QUALITY:INPUT_MISSING",
        "EARNINGS_TREND:INPUT_MISSING",
    )


def test_an_undetermined_upside_is_recorded_as_a_reason() -> None:
    result = to_raer_result(_evaluate(expected_return=_expected_return(upside_known=False)))

    assert result.reason_codes[:2] == (
        "COMPOSITION_RULE_NOT_DECIDED",
        "EXPECTED_RETURN_UPSIDE_UNDETERMINED",
    )


def test_the_evaluation_keeps_the_expected_return_as_given() -> None:
    expected_return = _expected_return()

    evaluation = _evaluate(expected_return=expected_return)

    assert evaluation.expected_return is expected_return  # 再計算しない


def test_enum_surfaces_are_pinned() -> None:
    assert [s.value for s in RaerState] == ["AVAILABLE", "PROVISIONAL", "UNAVAILABLE"]
    assert [s.value for s in AxisStatus] == ["APPLIED", "NOT_APPLIED", "UNAVAILABLE"]
    # PR-B2(#602)で、組合せの値を作るために追加した語(意図した変更。B1 の語は 1 つも変えていない)
    assert sorted(r.value for r in AxisReasonCode) == [
        "AXIS_NOT_IMPLEMENTED",
        "AXIS_NOT_PROVIDED",
        "COVERAGE_BELOW_MINIMUM",
        "INPUT_MISSING",
        "NOT_APPLICABLE_TO_STOCK",
        "RISK_FLAGS_NOT_OBSERVED",
    ]
    assert sorted(r.value for r in RaerReasonCode) == [
        "AXES_BELOW_MINIMUM",
        "BASE_UNDETERMINED",
        "COMPOSITION_RULE_NOT_DECIDED",
        "EXPECTED_RETURN_INCOME_UNDETERMINED",
        "EXPECTED_RETURN_UPSIDE_UNDETERMINED",
        "REQUIRED_AXIS_MISSING",
    ]


# --- (7) モデル版 ---


def test_the_model_version_is_a_pinned_literal_that_marks_the_provisional_axes() -> None:
    """暫定計算であることをモデル版で識別する。定数との比較は恒真なので literal で固定する。"""
    assert MODEL_VERSION == "raer-structure-v1-provisional-axes-y"
    assert PROVISIONAL_MARK == "-provisional-axes-y"
    assert _evaluate().model_version == "raer-structure-v1-provisional-axes-y"
    assert to_raer_result(_evaluate()).model_version == "raer-structure-v1-provisional-axes-y"


# --- (8) 構造 ---


def _module_tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def test_module_reads_no_clock_and_has_no_arithmetic_formula() -> None:
    tree = _module_tree()
    called = {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert not called & {"now", "today", "utcnow", "time", "monotonic", "perf_counter", "sleep"}
    # 式を実装しない: 乗除・べき乗の演算が 1 つも無い(未決の合成式・単位・重みを持ち込まない)
    operators = {type(n.op) for n in ast.walk(tree) if isinstance(n, ast.BinOp | ast.AugAssign)}
    assert not operators & {ast.Mult, ast.Div, ast.FloorDiv, ast.Pow, ast.MatMult, ast.Mod}
    # 値を作る手段を持たない: float への変換・丸め・平均の呼出が無い。PR-B2(#602)で、composition の
    # 恒等式(土台 - Σ寄与度 = value)の検査のために Decimal の生成・sum を使う(加減算だけ。
    # 乗除・べき乗の演算は上で引き続き禁止 = 式は raer_composition.py にあり、本 module に無い)
    assert not called & {"float", "round", "quantize", "mean"}


def test_module_has_no_forbidden_names() -> None:
    tree = _module_tree()
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {
        n.target.id
        for n in ast.walk(tree)
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
    }
    names |= {
        a.arg for n in ast.walk(tree) if isinstance(n, ast.arguments) for a in n.args + n.kwonlyargs
    }
    lowered = " ".join(sorted(n.lower() for n in names))
    for forbidden in (
        "available_cash",
        "acquisition",
        "average_price",
        "cost_basis",
        "notification",
    ):
        assert forbidden not in lowered, forbidden


def test_module_depends_only_on_the_expected_return_types_and_confidence_level() -> None:
    imported: list[str] = []
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
        elif isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
    forbidden = tuple(
        f"jstock_advisor.{n}"
        for n in ("services", "infrastructure", "lambda_handlers", "providers", "cli", "config")
    )
    for module in imported:
        assert not module.startswith(forbidden), module
        assert "exit_architecture" not in module, module
    own = sorted(m for m in imported if m.startswith("jstock_advisor"))
    assert own == [
        "jstock_advisor.domain.entities.enums",
        "jstock_advisor.domain.valuation.expected_return",
    ]


# --- dormant: import 連鎖の推移閉包(handler・CLI から到達しない) ---


def _module_files(package_root: Path) -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in package_root.rglob("*.py"):
        relative = path.relative_to(package_root.parent).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


def _direct_imports(module: str, path: Path, known: dict[str, Path]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    base = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = base[: len(base) - (node.level - 1)]
                prefix = ".".join(anchor + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            targets = [prefix] + [f"{prefix}.{a.name}" for a in node.names]
        else:
            continue
        for target in targets:
            parts = target.split(".")
            for i in range(1, len(parts) + 1):
                candidate = ".".join(parts[:i])
                if candidate in known:
                    found.add(candidate)
    return found


def _reachable(roots: set[str], known: dict[str, Path]) -> set[str]:
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        stack.extend(_direct_imports(module, known[module], known) - seen)
    return seen


def test_reachability_helper_follows_chains_and_relative_imports(tmp_path: Path) -> None:
    """helper 自体の検証: 間接の連鎖・相対 import・親 package を辿れる(空振りしない)。"""
    root = tmp_path / "pkg"
    (root / "sub").mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "sub" / "__init__.py").write_text("", encoding="utf-8")
    (root / "entry.py").write_text("from .sub import middle\n", encoding="utf-8")
    (root / "sub" / "middle.py").write_text("from . import leaf\n", encoding="utf-8")
    (root / "sub" / "leaf.py").write_text("X = 1\n", encoding="utf-8")
    (root / "orphan.py").write_text("Y = 2\n", encoding="utf-8")

    reached = _reachable({"pkg.entry"}, _module_files(root))

    assert {"pkg.entry", "pkg.sub", "pkg.sub.middle", "pkg.sub.leaf"} <= reached
    assert "pkg.orphan" not in reached


def test_the_new_module_is_reachable_from_handlers_only_through_the_shadow_wiring() -> None:
    """RAER が handler・CLI から到達できる入口は、出荷 SHADOW の Shadow の配線だけ(#603 PR-F2-2b)。

    承認の記録 = #122 issuecomment-6138622792【1】(本番 BUY 候補バッチ内で RAER・配分を算出できる)。
    配線の module を『辿らない』ようにすると、他の経路からは到達しない(= 入口はそこだけ)。
    """
    wiring = "jstock_advisor.lambda_handlers.allocation_shadow_wiring"
    known = {m: p for m, p in _module_files(_SRC).items() if m != wiring}
    assert wiring not in known
    roots = {
        m
        for m in known
        if m.startswith(("jstock_advisor.lambda_handlers.", "jstock_advisor.cli."))
        or m in {"jstock_advisor.lambda_handlers", "jstock_advisor.cli"}
    }
    assert len(roots) > 20

    reached = _reachable(roots, known)

    assert "jstock_advisor.domain.entities.enums" in reached
    assert "jstock_advisor.domain.valuation.raer" in known
    assert "jstock_advisor.domain.valuation.raer" not in reached  # 配線以外の入口は無い
    with_wiring = _reachable(
        roots | {wiring},
        {**known, wiring: _SRC / "lambda_handlers" / "allocation_shadow_wiring.py"},
    )
    assert "jstock_advisor.domain.valuation.raer" in with_wiring  # 配線からは到達する(空振り防止)


# この module を import してよい src の module の明示リスト(いずれも未配線の dormant)。
# #603 の配分の型(PR-A)は RaerResult を候補の入力に持つ。後続の消費側はここへ足す
_ALLOWED_IMPORTERS = {
    "domain/signals/portfolio_allocation_types.py",  # #603 PR-A
    "domain/signals/portfolio_allocation.py",  # #603 PR-C
    "domain/signals/capital_rotation.py",  # #603 PR-D(入替元の RaerResult)
    "domain/valuation/raer_coefficients_config.py",  # #602 PR-E(係数の loader)
    "domain/valuation/raer_composition.py",  # #602 PR-B2(式。本 module のデータ型を使う)
    "domain/signals/allocation_policy_config.py",  # #603 PR-E(RiskFlag の語彙を検証)
    "domain/signals/allocation_inputs.py",  # #603 PR-F1(候補の RaerResult を受ける)
    "domain/signals/allocation_shadow_record.py",  # #603 PR-F2-1(RaerResult を記録へ写す)
    "domain/valuation/raer_inputs.py",  # #602 PR-G1(入力の組み立て。RaerResult を作る)
    "services/allocation_shadow_compute.py",  # #603 PR-F2-2a(RAER を計算して候補へ渡す)
    "lambda_handlers/allocation_shadow_wiring.py",  # #603 PR-F2-2b(係数・assembler を読む)
}


def test_no_other_source_file_mentions_the_module() -> None:
    importers = []
    for path in sorted(_SRC.rglob("*.py")):
        if path == _MODULE_PATH:
            continue
        text = path.read_text(encoding="utf-8")
        if "valuation.raer" in text or "valuation import raer" in text:
            importers.append(path.relative_to(_SRC).as_posix())
    assert set(importers) <= _ALLOWED_IMPORTERS, importers
