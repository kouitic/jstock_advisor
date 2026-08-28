"""valuation taxonomy(Issue #20 Phase B1)のテスト。

taxonomyは「依存関係の事実」のみを表現し、クラスタ仮説・独立票数の概念を
一切含まないことを固定する。
"""

import pytest

from jstock_advisor.domain.valuation import valuation_taxonomy as taxonomy_module
from jstock_advisor.domain.valuation.valuation_taxonomy import (
    METHOD_DEPENDENCY_TAGS,
    METHOD_PRINCIPLES,
    VALUATION_TAXONOMY_VERSION,
    ValuationDependencyTag,
    ValuationPrinciple,
    dependency_tags_for_method,
    principle_for_method,
)

_STANDARD_METHODS = {"target_yield", "per", "pbr", "historical_range", "dcf"}


def test_all_five_standard_methods_are_covered() -> None:
    assert set(METHOD_PRINCIPLES) == _STANDARD_METHODS
    assert set(METHOD_DEPENDENCY_TAGS) == _STANDARD_METHODS


def test_principle_mapping_is_one_to_one() -> None:
    """5方式↔5原理の1対1(原理enumがクラスタを表現しないことの構造的保証)。"""
    principles = list(METHOD_PRINCIPLES.values())
    assert len(principles) == len(set(principles)) == 5
    assert set(principles) == set(ValuationPrinciple)


def test_expected_principle_assignments() -> None:
    assert principle_for_method("per") is ValuationPrinciple.EARNINGS_MULTIPLE
    assert principle_for_method("pbr") is ValuationPrinciple.ASSET_MULTIPLE
    assert principle_for_method("target_yield") is ValuationPrinciple.SHAREHOLDER_RETURN
    assert principle_for_method("dcf") is ValuationPrinciple.INTRINSIC_CASHFLOW
    assert principle_for_method("historical_range") is ValuationPrinciple.MARKET_HISTORY


def test_dependency_tags_are_nonempty_and_expected() -> None:
    for method in _STANDARD_METHODS:
        assert dependency_tags_for_method(method)
    assert dependency_tags_for_method("per") == {
        ValuationDependencyTag.EARNINGS,
        ValuationDependencyTag.MARKET_MULTIPLE_HISTORY,
    }
    assert dependency_tags_for_method("pbr") == {
        ValuationDependencyTag.BOOK_VALUE,
        ValuationDependencyTag.MARKET_MULTIPLE_HISTORY,
    }
    assert dependency_tags_for_method("target_yield") == {ValuationDependencyTag.DIVIDEND}
    assert dependency_tags_for_method("dcf") == {ValuationDependencyTag.CASHFLOW}
    assert dependency_tags_for_method("historical_range") == {
        ValuationDependencyTag.MARKET_PRICE_HISTORY
    }


def test_unknown_method_raises_explicit_error() -> None:
    with pytest.raises(ValueError, match="未登録の方式"):
        principle_for_method("unknown_method")
    with pytest.raises(ValueError, match="未登録の方式"):
        dependency_tags_for_method("unknown_method")


def test_taxonomy_version_is_defined() -> None:
    assert VALUATION_TAXONOMY_VERSION == "vt1"


def test_taxonomy_module_contains_no_cluster_or_grouping_definitions() -> None:
    """クラスタ仮説・独立票数はPhase C分析側の概念であり、taxonomyモジュールに
    存在しないこと(将来の混入を防ぐガード)。"""
    public_names = [n for n in dir(taxonomy_module) if not n.startswith("_")]
    joined = " ".join(n.lower() for n in public_names)
    for forbidden in ("cluster", "group_count", "independent_evidence", "hypothesis"):
        assert forbidden not in joined
