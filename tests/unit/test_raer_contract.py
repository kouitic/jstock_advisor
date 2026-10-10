"""RAER の型・状態・比較可能性の契約テスト(Issue #602 PR-B1)。

## この PR の範囲(USER 承認: #122 issuecomment-6102822046 の『1. #602 PR-B1』)
  RAER の型・状態・比較可能性の契約のみ: 5 軸の適用・未適用状態の記録 / 欠測とリスクなしの区別 /
  comparison_key による比較可否の固定 / モデルバージョンの保持。
  ★ 未決の式・重み・閾値は実装しない。value は UNDETERMINED のまま(PR-B2 の数値計算は承認対象外)。

## 固定するもの
  (0) 先行(characterization): 本件が依存する既存の契約は変更しない
      (ExpectedReturnResult の field と composite の常時 UNDETERMINED / ConfidenceLevel の値 /
      本件の型名が raer.py の外に定義されていないこと)
"""

from __future__ import annotations

import ast
from dataclasses import fields
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
