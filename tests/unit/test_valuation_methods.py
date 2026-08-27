from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import BuyPriceReliability, ConfidenceLevel
from jstock_advisor.domain.entities.valuation import FairValueMethodResult
from jstock_advisor.domain.valuation.valuation_methods import (
    apply_dcf_divergence_filter,
    apply_outlier_filters,
    build_valuation_summary,
    compute_valuation_anchor,
    determine_dispersion_band,
)

_CONFIG = load_config()
_DISPERSION_CONFIG = _CONFIG.buy_decision.valuation_dispersion
_USABILITY_CONFIG = _CONFIG.valuation.fair_value_usability


def _result(method: str, fair_value: str | None, confidence=ConfidenceLevel.HIGH, **kwargs):
    return FairValueMethodResult(
        method=method,
        fair_value=Decimal(fair_value) if fair_value is not None else None,
        confidence=confidence,
        **kwargs,
    )


def test_dispersion_band_low_at_or_below_1_30() -> None:
    assert determine_dispersion_band(1.30, _DISPERSION_CONFIG) == "LOW"
    assert determine_dispersion_band(1.0, _DISPERSION_CONFIG) == "LOW"


def test_dispersion_band_medium_between_1_30_and_1_60() -> None:
    assert determine_dispersion_band(1.45, _DISPERSION_CONFIG) == "MEDIUM"
    assert determine_dispersion_band(1.60, _DISPERSION_CONFIG) == "MEDIUM"


def test_dispersion_band_high_above_1_60() -> None:
    assert determine_dispersion_band(1.61, _DISPERSION_CONFIG) == "HIGH"
    assert determine_dispersion_band(2.5, _DISPERSION_CONFIG) == "HIGH"


def test_dispersion_band_none_when_ratio_none() -> None:
    assert determine_dispersion_band(None, _DISPERSION_CONFIG) is None


def test_build_valuation_summary_computes_statistics() -> None:
    results = [
        _result("target_yield", "100"),
        _result("per", "120"),
        _result("pbr", "110"),
    ]
    summary = build_valuation_summary(results, "median", None, _USABILITY_CONFIG)
    assert summary.valuation_min == Decimal("100")
    assert summary.valuation_max == Decimal("120")
    assert summary.valuation_median == Decimal("110")
    assert summary.methods_used_count == 3
    assert summary.valuation_dispersion_ratio == 1.2


def test_build_valuation_summary_excludes_inapplicable_methods() -> None:
    results = [
        _result("target_yield", "100"),
        _result("per", "500", applicable=False, exclusion_reason="EPSが負数のため除外"),
    ]
    summary = build_valuation_summary(results, "median", None, _USABILITY_CONFIG)
    assert summary.methods_used_count == 1
    assert summary.valuation_max == Decimal("100")


def test_dcf_divergence_filter_excludes_upward_outlier() -> None:
    dcf = _result("dcf", "300", confidence=ConfidenceLevel.MEDIUM)
    others = [_result("target_yield", "100"), _result("per", "110")]
    filtered = apply_dcf_divergence_filter(dcf, others)
    assert filtered.applicable is False
    assert filtered.exclusion_reason is not None


def test_dcf_divergence_filter_keeps_close_dcf() -> None:
    dcf = _result("dcf", "115", confidence=ConfidenceLevel.MEDIUM)
    others = [_result("target_yield", "100"), _result("per", "110")]
    filtered = apply_dcf_divergence_filter(dcf, others)
    assert filtered.applicable is True


def test_apply_outlier_filters_excludes_far_below_median() -> None:
    # クリヤマ3355の実データ: DCF=115.31円が他4方式の中央値(約1073円)の
    # 40%未満のため下方外れ値として除外される。
    results = [
        _result("target_yield", "1525"),
        _result("per", "977.11"),
        _result("pbr", "1170.98"),
        _result("historical_range", "1169"),
        _result("dcf", "115.31", confidence=ConfidenceLevel.MEDIUM),
    ]
    outlier_result = apply_outlier_filters(results, current_price=Decimal("1675"))
    filtered = outlier_result.results
    dcf_result = next(r for r in filtered if r.method == "dcf")
    assert dcf_result.applicable is False
    assert dcf_result.fair_value is None
    assert dcf_result.exclusion_detail is not None
    assert dcf_result.exclusion_detail.code == "EXTREME_LOW_RELATIVE_TO_CURRENT_PRICE"
    others = [r for r in filtered if r.method != "dcf"]
    assert all(r.applicable for r in others)
    assert outlier_result.reliability == BuyPriceReliability.OK
    assert outlier_result.blocking_reason is None


def test_apply_outlier_filters_keeps_close_values() -> None:
    results = [
        _result("target_yield", "100"),
        _result("per", "105"),
        _result("pbr", "95"),
    ]
    outlier_result = apply_outlier_filters(results, current_price=Decimal("100"))
    assert all(r.applicable for r in outlier_result.results)


def test_apply_outlier_filters_noop_when_fewer_than_2_applicable() -> None:
    results = [_result("target_yield", "10")]
    outlier_result = apply_outlier_filters(results, current_price=Decimal("1000"))
    assert outlier_result.results[0].applicable is True


# --- BUYパイプライン第3次修正(2026-07): 最小方式数を3件に引き上げ ----------------


def test_apply_outlier_filters_does_not_mutually_exclude_two_methods() -> None:
    """有効な方式が2件しかない場合、外れ値検知そのものを行わない
    (2件では互いが唯一の比較対象になり、双方を機械的に外れ値とみなし合って
    全滅してしまう不具合を防ぐ)。methods_used_count<=2の低信頼シグナルは
    既存のTOO_FEW_VALUATION_METHODSゲートに委ねる。
    """
    results = [_result("target_yield", "500"), _result("per", "1500")]
    outlier_result = apply_outlier_filters(results)
    assert outlier_result.excluded_count == 0
    assert all(r.applicable for r in outlier_result.results)
    assert outlier_result.remaining_count == 2
    assert outlier_result.reliability == BuyPriceReliability.OK
    assert outlier_result.blocking_reason is None


def test_apply_outlier_filters_three_methods_all_excluded_falls_back_with_low_reliability() -> (
    None
):
    """3方式が互いを極端な外れ値とみなし合い全滅するケース(38円・100円・2900円)。
    中央値(100円)だけを機械的に「正しい」適正価格として採用してはならない
    (=除外を採用せず、除外前の3件へフォールバックする)。この場合、
    methods_used_countだけでは3のままになり低信頼を検出できないため、
    blocking_reasonで明示的にLOWを立てる。
    """
    results = [
        _result("target_yield", "38"),
        _result("per", "100"),
        _result("pbr", "2900"),
    ]
    outlier_result = apply_outlier_filters(results)
    assert outlier_result.blocking_reason == "TOO_FEW_METHODS_AFTER_OUTLIER_FILTER"
    assert outlier_result.reliability == BuyPriceReliability.LOW
    # 除外前へフォールバックしているため、3件とも引き続きapplicable=Trueのまま。
    assert all(r.applicable for r in outlier_result.results)
    assert {r.fair_value for r in outlier_result.results} == {
        Decimal("38"),
        Decimal("100"),
        Decimal("2900"),
    }


def test_apply_outlier_filters_four_methods_excludes_only_high_outlier() -> None:
    """4方式(1000/1050/1100/3000)のうち、3000のみが上方外れ値として除外され、
    残り3件は影響を受けない。"""
    results = [
        _result("target_yield", "1000"),
        _result("per", "1050"),
        _result("pbr", "1100"),
        _result("historical_range", "3000"),
    ]
    outlier_result = apply_outlier_filters(results)
    excluded = [r for r in outlier_result.results if not r.applicable]
    kept = [r for r in outlier_result.results if r.applicable]
    assert {r.method for r in excluded} == {"historical_range"}
    assert {r.fair_value for r in kept} == {Decimal("1000"), Decimal("1050"), Decimal("1100")}
    assert outlier_result.blocking_reason is None
    assert outlier_result.reliability == BuyPriceReliability.OK


def test_apply_outlier_filters_four_methods_excludes_only_low_outlier() -> None:
    """4方式(50/950/1000/1050)のうち、50のみが下方外れ値として除外され、
    残り3件は影響を受けない。"""
    results = [
        _result("target_yield", "50"),
        _result("per", "950"),
        _result("pbr", "1000"),
        _result("historical_range", "1050"),
    ]
    outlier_result = apply_outlier_filters(results)
    excluded = [r for r in outlier_result.results if not r.applicable]
    kept = [r for r in outlier_result.results if r.applicable]
    assert {r.method for r in excluded} == {"target_yield"}
    assert {r.fair_value for r in kept} == {Decimal("950"), Decimal("1000"), Decimal("1050")}
    assert outlier_result.blocking_reason is None
    assert outlier_result.reliability == BuyPriceReliability.OK


def test_build_valuation_summary_propagates_outlier_filter_blocking_reason() -> None:
    """外れ値除外が破綻してフォールバックした場合、build_valuation_summary()の
    戻り値(FairValueRange)にもblocking_reasonが伝播する
    (buy_signal_service.py側でdetermine_buy_price_reliability()へ渡すため)。
    """
    results = [
        _result("target_yield", "38"),
        _result("per", "100"),
        _result("pbr", "2900"),
    ]
    summary = build_valuation_summary(results, "median", None, _USABILITY_CONFIG)
    assert summary.outlier_filter_blocking_reason == "TOO_FEW_METHODS_AFTER_OUTLIER_FILTER"


def test_determine_buy_price_reliability_forces_low_on_outlier_filter_blocking_reason() -> None:
    """outlier_filter_blocking_reasonが設定されている場合、他の懸念件数に
    かかわらず単独でLOWとなることを確認する(明示的な低信頼シグナル)。
    """
    from jstock_advisor.domain.valuation.buy_price_reliability import (
        determine_buy_price_reliability,
    )
    from jstock_advisor.domain.valuation.margin_of_safety import MarginOfSafetyResult

    margin_result = MarginOfSafetyResult(
        entry_margin=Decimal("0.20"),
        standard_margin=Decimal("0.25"),
        strong_margin=Decimal("0.30"),
        entry_margin_before_cap=Decimal("0.20"),
        adjustments=[],
    )
    result = determine_buy_price_reliability(
        margin_result=margin_result,
        maximum_entry_margin=0.30,
        valuation_dispersion_ratio=1.1,
        dispersion_medium_max=1.60,
        methods_used_count=3,
        data_quality_warning=False,
        earnings_date_status=None,
        excluded_outlier_count=0,
        outlier_filter_blocking_reason="TOO_FEW_METHODS_AFTER_OUTLIER_FILTER",
    )
    assert result.reliability == BuyPriceReliability.LOW
    assert "TOO_FEW_METHODS_AFTER_OUTLIER_FILTER" in result.concerns


def test_build_valuation_summary_separates_all_methods_from_decision_range() -> None:
    # 東洋電機6505の実データ: 全手法参考値(valuation_min/max)は外れ値込みの
    # 38.29円〜2900円のまま、decision_valuation_min/maxは外れ値除外後の
    # 926.74円〜1337円になる。
    results = [
        _result("target_yield", "2900"),
        _result("per", "926.74"),
        _result("pbr", "1113.59"),
        _result("historical_range", "1337"),
        _result("dcf", "38.29", confidence=ConfidenceLevel.MEDIUM),
    ]
    summary = build_valuation_summary(
        results, "median", None, _USABILITY_CONFIG, current_price=Decimal("2328")
    )
    assert summary.valuation_min == Decimal("38.29")
    assert summary.valuation_max == Decimal("2900")
    assert summary.decision_valuation_min == Decimal("926.74")
    assert summary.decision_valuation_max == Decimal("1337")
    assert summary.valuation_dispersion_ratio is not None
    assert summary.valuation_dispersion_ratio < 2.0


def test_valuation_anchor_none_when_confidence_low() -> None:
    results = [_result("target_yield", "100")]
    summary = build_valuation_summary(results, "median", None, _USABILITY_CONFIG)
    result = compute_valuation_anchor(summary, ConfidenceLevel.LOW, "LOW")
    assert result.anchor is None
    # レビュー対応(2026-08、NO_VALUATION_ANCHOR表示不備の是正): confidence==LOWに
    # よる打ち切りの理由はdetermine_valuation_confidence()側のValuation
    # ConfidenceResult.blocking_reasonに格納される設計のため、compute_valuation_
    # anchor()自身のblocking_reasonはここでは設定されない(重複防止)。
    assert result.blocking_reason is None


def test_valuation_anchor_high_confidence_low_dispersion_uses_weighted_median() -> None:
    results = [_result("target_yield", "100"), _result("per", "110"), _result("pbr", "120")]
    summary = build_valuation_summary(results, "median", None, _USABILITY_CONFIG)
    result = compute_valuation_anchor(summary, ConfidenceLevel.HIGH, "LOW")
    assert result.anchor == Decimal("110")


def test_valuation_anchor_medium_confidence_uses_min_of_weighted_median_and_trimmed_mean() -> None:
    results = [_result("target_yield", "100"), _result("per", "110"), _result("pbr", "150")]
    summary = build_valuation_summary(results, "median", None, _USABILITY_CONFIG)
    result = compute_valuation_anchor(summary, ConfidenceLevel.MEDIUM, "LOW")
    weighted_median = Decimal("110")
    assert result.anchor is not None
    assert result.anchor <= weighted_median


def test_valuation_anchor_high_dispersion_uses_percentile_40() -> None:
    results = [_result("target_yield", "100"), _result("per", "200")]
    summary = build_valuation_summary(results, "median", None, _USABILITY_CONFIG)
    result = compute_valuation_anchor(summary, ConfidenceLevel.MEDIUM, "HIGH")
    assert result.anchor is not None
    assert Decimal("100") <= result.anchor <= Decimal("200")


def test_valuation_anchor_calculation_failed_when_all_weights_non_positive() -> None:
    """必須テスト6: weighted_medianが算出不能(全採用方式のweightが0以下)な
    理論上のエッジケース。現行configのmethod_weightsは全方式正の値が設定されて
    おり実運用では発生しないため、method_weightsを直接0で注入して単体で
    検証する(BuySignalService経由の結合テストでは到達不能パスのため対象外)。"""
    results = [_result("target_yield", "100"), _result("per", "110"), _result("pbr", "120")]
    summary = build_valuation_summary(results, "median", None, _USABILITY_CONFIG)
    zero_weights = {"target_yield": 0.0, "per": 0.0, "pbr": 0.0}
    result = compute_valuation_anchor(summary, ConfidenceLevel.HIGH, "LOW", zero_weights)
    assert result.anchor is None
    assert result.blocking_reason is not None
    assert result.blocking_reason.code == "VALUATION_ANCHOR_CALCULATION_FAILED"
