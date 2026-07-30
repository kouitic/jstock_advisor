"""適正価格の複数手法集計(2026-07 BUYパイプライン再設計。要求仕様9節〜11節)。

単一の「最終適正価格」を断定的に扱わず、手法間のバラつきを踏まえて
購入判断基準価格(valuation_anchor)を保守的に算出する。既存の
`fair_value_usability.py::build_fair_value_range()`(SELL側でも使用)を
ラップし、min/max/median/mean/dispersion_ratio等の統計値を追加する。
"""

from __future__ import annotations

import statistics
from decimal import Decimal
from typing import Literal

from jstock_advisor.config.models import FairValueUsability, ValuationDispersionThresholds
from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.entities.valuation import FairValueMethodResult, FairValueRange
from jstock_advisor.domain.valuation.fair_value_usability import build_fair_value_range

DispersionBand = Literal["LOW", "MEDIUM", "HIGH"]

# DCFが他方式の中央値をこの倍率以上上回る場合、適正価格を押し上げる方向にのみ
# 作用していると判断し、集計から除外する(要求仕様10節)。
_DCF_UPWARD_DIVERGENCE_RATIO = Decimal("1.3")


def build_valuation_summary(
    method_results: list[FairValueMethodResult],
    aggregation_method: str,
    method_weights: dict[str, float] | None,
    usability_config: FairValueUsability,
) -> FairValueRange:
    """既存のbuild_fair_value_range()を呼び、統計値(min/max/median/mean/
    dispersion_ratio/methods_used_count)を追加する。applicable=Falseの方式は
    fair_value=Noneに書き換えたうえで渡し、集計対象から確実に除外する。
    """
    normalized_results = [
        r if r.applicable else r.model_copy(update={"fair_value": None}) for r in method_results
    ]
    base_range = build_fair_value_range(
        normalized_results, aggregation_method, method_weights, usability_config
    )

    used_values = [r.fair_value for r in base_range.methods_used if r.fair_value is not None]
    if not used_values:
        return base_range

    valuation_min = min(used_values)
    valuation_max = max(used_values)
    valuation_median = statistics.median(used_values)
    valuation_mean = sum(used_values, Decimal("0")) / len(used_values)
    dispersion_ratio = (
        float(valuation_max / valuation_min) if valuation_min > 0 else None
    )

    return base_range.model_copy(
        update={
            "valuation_min": valuation_min,
            "valuation_max": valuation_max,
            "valuation_median": valuation_median,
            "valuation_mean": valuation_mean,
            "valuation_dispersion_ratio": dispersion_ratio,
            "methods_used_count": len(used_values),
        }
    )


def determine_dispersion_band(
    dispersion_ratio: float | None, config: ValuationDispersionThresholds
) -> DispersionBand | None:
    """要求仕様9節: 1.30以下=小、1.30超1.60以下=中、1.60超=大。

    2.00超は自動購入判定禁止(呼び出し側でBuyAction判定時に別途参照する)。
    """
    if dispersion_ratio is None:
        return None
    if dispersion_ratio <= config.low_max:
        return "LOW"
    if dispersion_ratio <= config.medium_max:
        return "MEDIUM"
    return "HIGH"


def apply_dcf_divergence_filter(
    dcf_result: FairValueMethodResult, other_applicable_results: list[FairValueMethodResult]
) -> FairValueMethodResult:
    """DCFが他方式の中央値を大きく上回る場合、適正価格を押し上げるためだけに
    使われることを防ぐため、集計から除外する(要求仕様10節)。
    """
    if dcf_result.fair_value is None or not dcf_result.applicable:
        return dcf_result
    other_values = [
        r.fair_value
        for r in other_applicable_results
        if r.applicable and r.fair_value is not None and r.method != "dcf"
    ]
    if not other_values:
        return dcf_result
    median_others = statistics.median(other_values)
    if median_others <= 0:
        return dcf_result
    if dcf_result.fair_value > median_others * _DCF_UPWARD_DIVERGENCE_RATIO:
        return dcf_result.model_copy(
            update={
                "applicable": False,
                "exclusion_reason": (
                    "簡易DCFが他方式の中央値を30%超上回っており、適正価格を"
                    "押し上げる方向にのみ作用するため除外"
                ),
            }
        )
    return dcf_result


def _weighted_median(values_with_weights: list[tuple[Decimal, float]]) -> Decimal | None:
    positive = [(v, w) for v, w in values_with_weights if w > 0]
    if not positive:
        return None
    ordered = sorted(positive, key=lambda item: item[0])
    total_weight = sum(w for _, w in ordered)
    if total_weight <= 0:
        return None
    cumulative = 0.0
    half = total_weight / 2
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= half:
            return value
    return ordered[-1][0]


def _trimmed_mean(values: list[Decimal], trim_fraction: float = 0.1) -> Decimal:
    ordered = sorted(values)
    n = len(ordered)
    trim_count = int(n * trim_fraction)
    trimmed = ordered[trim_count : n - trim_count] if n - 2 * trim_count > 0 else ordered
    return sum(trimmed, Decimal("0")) / len(trimmed)


def _percentile(values: list[Decimal], pct: float) -> Decimal:
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return ordered[0]
    rank = pct / 100 * (n - 1)
    lower_index = int(rank)
    upper_index = min(lower_index + 1, n - 1)
    fraction = Decimal(str(rank - lower_index))
    return ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction


def compute_valuation_anchor(
    fair_value_range: FairValueRange,
    valuation_confidence: ConfidenceLevel,
    dispersion_band: DispersionBand | None,
    method_weights: dict[str, float] | None = None,
) -> Decimal | None:
    """購入判断基準価格(要求仕様11節)。適正価格は「買ってよい上限価格」ではなく
    企業価値の中心値として扱い、実際の買付価格には別途安全余裕率を適用する。

    - 信頼度HIGHかつばらつき小: weighted_median
    - 信頼度MEDIUMまたはばらつき中: min(weighted_median, trimmed_mean)
    - ばらつき大: percentile_40
    - 信頼度LOW: None(自動買付価格を生成しない)
    """
    if valuation_confidence == ConfidenceLevel.LOW:
        return None

    values = [r.fair_value for r in fair_value_range.methods_used if r.fair_value is not None]
    if not values:
        return None

    weights = method_weights or {}
    values_with_weights = [
        (r.fair_value, weights.get(r.method, 1.0))
        for r in fair_value_range.methods_used
        if r.fair_value is not None
    ]
    weighted_median = _weighted_median(values_with_weights)
    if weighted_median is None:
        return None

    if dispersion_band == "HIGH":
        return _percentile(values, 40)
    if valuation_confidence == ConfidenceLevel.MEDIUM or dispersion_band == "MEDIUM":
        trimmed_mean = _trimmed_mean(values)
        return min(weighted_median, trimmed_mean)
    # 信頼度HIGHかつばらつき小(またはばらつき不明で信頼度HIGH)
    return weighted_median
