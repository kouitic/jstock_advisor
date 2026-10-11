"""ExpectedReturnResult -> ExpectedReturnVerdict の写像の契約テスト(Issue #601 PR-1b)。

## この PR の範囲(USER 承認: #122 issuecomment-6102822046 の『2. #601 PR-1b』)
  ExpectedReturnResult(`domain/valuation/expected_return.py`。#601 PR-1a)から、exit_architecture の
  ExpectedReturnVerdict への写像と、必要な C0 契約テストの更新。D2 の同時利用は、#903 と変更
  ファイルが重ならない条件の例外承認(記録 #601 …6102827464)。
  ★ 未決の式・重み・閾値は推測で実装しない(composite・年率換算・RAER は verdict に写さない)。

## 固定するもの
  (0) 先行(characterization): 写像が依存する既存の契約は変更しない
      (ExpectedReturnVerdict の検証・構成要素の 4 値・UndeterminedReason の値・ReasonCode の値)
"""

from __future__ import annotations

import pytest

from jstock_advisor.domain.exit_architecture.determination import Determination
from jstock_advisor.domain.exit_architecture.verdicts import (
    ComponentValue,
    ExpectedReturnComponent,
    ExpectedReturnVerdict,
)
from jstock_advisor.domain.exit_architecture.vocabulary import ReliabilityClass, UndeterminedReason
from jstock_advisor.domain.valuation.expected_return import ReasonCode

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
