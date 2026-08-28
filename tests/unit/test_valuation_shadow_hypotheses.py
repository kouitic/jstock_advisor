"""valuation shadow仮説レジストリ(Issue #20 Phase C)のテスト。

- PREDEFINED / EXPLORATORY_DATA_DERIVED の分離(探索仮説を事前定義候補へ
  混ぜない)
- 集約式(vh1のshadow定義)が現時点の本番式と同値であること
- grouping縮約の決定性
を固定する。
"""

from decimal import Decimal

from jstock_advisor.analysis.valuation_shadow_hypotheses import (
    ALL_HYPOTHESES,
    EXPLORATORY_HYPOTHESES,
    PREDEFINED_HYPOTHESES,
    SELL_USABILITY_SHADOW_PARAMS,
    VALUATION_HYPOTHESIS_SET_VERSION,
    HypothesisOrigin,
    compute_anchor_candidates,
    percentile_40,
    reduce_population,
    trimmed_mean,
    weighted_median_equal,
)
from jstock_advisor.domain.valuation.valuation_methods import (
    _percentile,
    _trimmed_mean,
    _weighted_median,
)

_STANDARD_METHODS = {"target_yield", "per", "pbr", "historical_range", "dcf"}


def test_version_and_sell_params_are_defined() -> None:
    assert VALUATION_HYPOTHESIS_SET_VERSION == "vh1"
    # SELL usability閾値は判定時点値として未保存のためshadow parameterとして版管理
    assert SELL_USABILITY_SHADOW_PARAMS == {
        "max_method_spread_ratio": 2.0,
        "min_methods_required": 2,
    }


def test_hypothesis_ids_unique_and_origins_separated() -> None:
    ids = [h.hypothesis_id for h in ALL_HYPOTHESES]
    assert len(ids) == len(set(ids))
    assert all(h.origin is HypothesisOrigin.PREDEFINED for h in PREDEFINED_HYPOTHESES)
    assert all(
        h.origin is HypothesisOrigin.EXPLORATORY_DATA_DERIVED for h in EXPLORATORY_HYPOTHESES
    )
    assert set(ALL_HYPOTHESES) == set(PREDEFINED_HYPOTHESES) | set(EXPLORATORY_HYPOTHESES)


def test_exploratory_hypothesis_carries_derivation_note() -> None:
    """data-derived仮説(実測相関ベースC1d相当)はorigin+derivation_noteで
    探索由来であることを明示する(selection bias防止の前提)。"""
    assert EXPLORATORY_HYPOTHESES, "探索仮説カテゴリ自体は存在する"
    for h in EXPLORATORY_HYPOTHESES:
        assert h.derivation_note


def test_c1c_is_canonicalized_into_h_d_per_pbr_pair() -> None:
    """設計候補C1c({per,pbr}|{target_yield}|{dcf}|{historical_range})は
    H_D_PER_PBR_PAIRと概念的に同一のため、vh1では別仮説として二重登録せず
    H_Dへcanonicalizeする(aliasで機械可読に記録)。"""
    c1c_conceptual_definition = {
        frozenset({"per", "pbr"}),
        frozenset({"target_yield"}),
        frozenset({"dcf"}),
        frozenset({"historical_range"}),
    }
    h_d = next(h for h in ALL_HYPOTHESES if h.hypothesis_id == "H_D_PER_PBR_PAIR")
    assert h_d.clusters is not None
    assert set(h_d.clusters) == c1c_conceptual_definition
    assert "C1c" in h_d.aliases
    # 同一分割を持つ仮説が重複登録されていないこと
    partitions = [set(h.clusters) for h in ALL_HYPOTHESES if h.clusters is not None]
    assert len(partitions) == len({frozenset(p) for p in partitions})


def test_clusters_are_valid_partitions_of_standard_methods() -> None:
    for h in ALL_HYPOTHESES:
        if h.clusters is None:
            continue
        seen: set[str] = set()
        for cluster in h.clusters:
            assert cluster <= _STANDARD_METHODS
            assert not (cluster & seen)  # 互いに素
            seen |= cluster
        assert seen == _STANDARD_METHODS  # 5方式の完全分割


def test_formulas_match_current_production_definitions() -> None:
    """vh1の集約式が現時点の本番式(valuation_methods.py)と同値であることを
    固定する(shadow定義は本番コードをimportしない設計のため、同値性は
    このテストで担保する)。"""
    cases = [
        [Decimal("949"), Decimal("1450"), Decimal("1468.5"), Decimal("1499.6")],
        [Decimal("100")],
        [Decimal("100"), Decimal("200")],
        [Decimal("100"), Decimal("200"), Decimal("300"), Decimal("400"), Decimal("500")],
        [Decimal("500"), Decimal("478.9"), Decimal("1450")],
    ]
    for values in cases:
        assert weighted_median_equal(values) == _weighted_median([(v, 0.2) for v in values])
        assert trimmed_mean(values) == _trimmed_mean(values)
        assert percentile_40(values) == _percentile(values, 40)


def test_compute_anchor_candidates_min_wm_tm() -> None:
    values = [Decimal("949"), Decimal("1450"), Decimal("1468.5"), Decimal("1499.6")]
    anchors = compute_anchor_candidates(values)
    assert anchors["weighted_median"] == Decimal("1450")
    assert anchors["trimmed_mean"] == Decimal("1341.775")
    assert anchors["min_wm_tm"] == Decimal("1341.775")
    assert anchors["median"] == Decimal("1459.25")


def test_reduce_population_independent_and_clustered() -> None:
    values = {
        "per": Decimal("1499.6"),
        "pbr": Decimal("1468.5"),
        "target_yield": Decimal("1450"),
        "historical_range": Decimal("949"),
    }
    independent = reduce_population(values, None)
    assert independent == sorted(values.items())

    clusters = (
        frozenset({"per", "pbr", "target_yield"}),
        frozenset({"historical_range"}),
        frozenset({"dcf"}),  # 値なし→skip(捏造しない)
    )
    reduced = reduce_population(values, clusters)
    assert reduced == [
        ("historical_range", Decimal("949")),
        ("pbr+per+target_yield", Decimal("1468.5")),
    ]


def test_reduce_population_keeps_unclustered_method_as_independent() -> None:
    """分割へ属さない方式が母集団に含まれる場合は独立票のまま残す
    (静かに落とさない)。"""
    values = {"per": Decimal("1000"), "unknown_extra": Decimal("500")}
    clusters = (
        frozenset({"per", "pbr"}),
        frozenset({"target_yield"}),
        frozenset({"dcf"}),
        frozenset({"historical_range"}),
    )
    reduced = reduce_population(values, clusters)
    assert ("unknown_extra", Decimal("500")) in reduced
    assert ("per", Decimal("1000")) in reduced
