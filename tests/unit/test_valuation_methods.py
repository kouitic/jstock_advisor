from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import ConfidenceLevel
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
    filtered = apply_outlier_filters(results, current_price=Decimal("1675"))
    dcf_result = next(r for r in filtered if r.method == "dcf")
    assert dcf_result.applicable is False
    assert dcf_result.fair_value is None
    assert dcf_result.exclusion_detail is not None
    assert dcf_result.exclusion_detail.code == "EXTREME_LOW_RELATIVE_TO_CURRENT_PRICE"
    others = [r for r in filtered if r.method != "dcf"]
    assert all(r.applicable for r in others)


def test_apply_outlier_filters_keeps_close_values() -> None:
    results = [
        _result("target_yield", "100"),
        _result("per", "105"),
        _result("pbr", "95"),
    ]
    filtered = apply_outlier_filters(results, current_price=Decimal("100"))
    assert all(r.applicable for r in filtered)


def test_apply_outlier_filters_noop_when_fewer_than_2_applicable() -> None:
    results = [_result("target_yield", "10")]
    filtered = apply_outlier_filters(results, current_price=Decimal("1000"))
    assert filtered[0].applicable is True


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
    anchor = compute_valuation_anchor(summary, ConfidenceLevel.LOW, "LOW")
    assert anchor is None


def test_valuation_anchor_high_confidence_low_dispersion_uses_weighted_median() -> None:
    results = [_result("target_yield", "100"), _result("per", "110"), _result("pbr", "120")]
    summary = build_valuation_summary(results, "median", None, _USABILITY_CONFIG)
    anchor = compute_valuation_anchor(summary, ConfidenceLevel.HIGH, "LOW")
    assert anchor == Decimal("110")


def test_valuation_anchor_medium_confidence_uses_min_of_weighted_median_and_trimmed_mean() -> None:
    results = [_result("target_yield", "100"), _result("per", "110"), _result("pbr", "150")]
    summary = build_valuation_summary(results, "median", None, _USABILITY_CONFIG)
    anchor = compute_valuation_anchor(summary, ConfidenceLevel.MEDIUM, "LOW")
    weighted_median = Decimal("110")
    assert anchor <= weighted_median


def test_valuation_anchor_high_dispersion_uses_percentile_40() -> None:
    results = [_result("target_yield", "100"), _result("per", "200")]
    summary = build_valuation_summary(results, "median", None, _USABILITY_CONFIG)
    anchor = compute_valuation_anchor(summary, ConfidenceLevel.MEDIUM, "HIGH")
    assert Decimal("100") <= anchor <= Decimal("200")
