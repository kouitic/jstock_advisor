"""適正価格の複数手法集計(2026-07 BUYパイプライン再設計、および第2次修正。
要求仕様9節〜11節)。

単一の「最終適正価格」を断定的に扱わず、手法間のバラつきを踏まえて
購入判断基準価格(valuation_anchor)を保守的に算出する。既存の
`fair_value_usability.py::build_fair_value_range()`(SELL側でも使用)を
ラップし、min/max/median/mean/dispersion_ratio等の統計値を追加する。

--- 第2次修正で追加(要求仕様10節) ---
DCFの上方乖離フィルタ(`apply_dcf_divergence_filter`)は「高すぎる」方向のみを
検出する片方向フィルターで、単年度キャッシュフローの歪み等で異常に「低い」
DCF値(実データ: 3355で115円、6505で38円)を検出できず、そのまま
valuation_anchor・バラつき判定・通知の適正価格レンジに使われていた。
`apply_outlier_filters()`はDCFに限らずどの方式にも適用する下方(および
上方の保険的な)外れ値フィルタで、除外理由を`ValuationExclusionReason`として
構造化して記録する。`build_valuation_summary()`は、このフィルタ適用後の
「決定用」集合から`valuation_min/max`(=valuation_anchor算出等に実際に使う値、
通知にも表示する)を算出し、フィルタ適用前の「全手法参考値」は
`decision_valuation_min/max`とは別に`raw_valuation_min/max`として保持しない
判断とした(entities側は`decision_valuation_min/max`という名前で新設済みのため、
そちらに決定用の値を格納し、既存の`valuation_min/max`はフィルタ適用前の
全手法参考値として維持する — 通知層は`decision_valuation_min/max`を、
監査ログは両方を参照する)。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from jstock_advisor.config.models import FairValueUsability, ValuationDispersionThresholds
from jstock_advisor.domain.entities.enums import BuyPriceReliability, ConfidenceLevel
from jstock_advisor.domain.entities.valuation import (
    FairValueMethodResult,
    FairValueRange,
    ValuationExclusionReason,
)
from jstock_advisor.domain.valuation.fair_value_usability import build_fair_value_range

DispersionBand = Literal["LOW", "MEDIUM", "HIGH"]

# DCFが他方式の中央値をこの倍率以上上回る場合、適正価格を押し上げる方向にのみ
# 作用していると判断し、集計から除外する(要求仕様10節)。
_DCF_UPWARD_DIVERGENCE_RATIO = Decimal("1.3")

# 下方外れ値フィルタの閾値(要求仕様10節)。
_EXTREME_LOW_VS_CURRENT_PRICE_RATIO = Decimal("0.10")
_EXTREME_LOW_VS_MEDIAN_RATIO = Decimal("0.40")
_EXTREME_HIGH_VS_MEDIAN_RATIO = Decimal("2.50")
_BELOW_52_WEEK_LOW_RATIO = Decimal("0.50")

# --- BUYパイプライン第3次修正(2026-07)で追加 ---
# 有効な方式が2件しかない場合、外れ値検知は「相手が唯一の比較対象」になるため
# 双方を機械的に外れ値とみなし合い、有効な方式が0件になる危険がある。
# 3件以上ある場合のみ外れ値検知を実行する。
_MIN_METHODS_FOR_OUTLIER_DETECTION = 3
# 外れ値除外後、比較・購入判断に使える方式がこの件数未満になった場合、
# 除外そのものが信頼できないと判断し、除外前の結果へフォールバックする。
_MIN_REMAINING_METHODS_AFTER_FILTER = 2
_TOO_FEW_METHODS_AFTER_OUTLIER_FILTER = "TOO_FEW_METHODS_AFTER_OUTLIER_FILTER"


def _detect_outlier(
    value: Decimal,
    other_values: list[Decimal],
    current_price: Decimal | None,
    low_52_week: Decimal | None,
) -> ValuationExclusionReason | None:
    if current_price is not None and current_price > 0:
        threshold = current_price * _EXTREME_LOW_VS_CURRENT_PRICE_RATIO
        if value < threshold:
            return ValuationExclusionReason(
                code="EXTREME_LOW_RELATIVE_TO_CURRENT_PRICE",
                message=(
                    f"算出値({value}円)が現在値({current_price}円)の"
                    f"{float(_EXTREME_LOW_VS_CURRENT_PRICE_RATIO) * 100:.0f}%未満であり、"
                    "算出誤差または前提の異常による外れ値の可能性が高いため除外"
                ),
                actual_value=value,
                reference_value=threshold,
            )

    if other_values:
        median_others = statistics.median(other_values)
        if median_others > 0:
            low_threshold = median_others * _EXTREME_LOW_VS_MEDIAN_RATIO
            if value < low_threshold:
                return ValuationExclusionReason(
                    code="EXTREME_LOW_RELATIVE_TO_MEDIAN",
                    message=(
                        f"算出値({value}円)が他方式の中央値({round(median_others, 0)}円)の"
                        f"{float(_EXTREME_LOW_VS_MEDIAN_RATIO) * 100:.0f}%未満であり、"
                        "算出誤差または前提の異常による外れ値の可能性が高いため除外"
                    ),
                    actual_value=value,
                    reference_value=low_threshold,
                )
            high_threshold = median_others * _EXTREME_HIGH_VS_MEDIAN_RATIO
            if value > high_threshold:
                return ValuationExclusionReason(
                    code="EXTREME_HIGH_RELATIVE_TO_MEDIAN",
                    message=(
                        f"算出値({value}円)が他方式の中央値({round(median_others, 0)}円)の"
                        f"{float(_EXTREME_HIGH_VS_MEDIAN_RATIO) * 100:.0f}%を超えており、"
                        "算出誤差または前提の異常による外れ値の可能性が高いため除外"
                    ),
                    actual_value=value,
                    reference_value=high_threshold,
                )

    if low_52_week is not None and low_52_week > 0:
        threshold = low_52_week * _BELOW_52_WEEK_LOW_RATIO
        if value < threshold:
            return ValuationExclusionReason(
                code="BELOW_52_WEEK_LOW",
                message=(
                    f"算出値({value}円)が直近52週安値({low_52_week}円)の"
                    f"{float(_BELOW_52_WEEK_LOW_RATIO) * 100:.0f}%未満であり、"
                    "算出誤差または前提の異常による外れ値の可能性が高いため除外"
                ),
                actual_value=value,
                reference_value=threshold,
            )
    return None


@dataclass(frozen=True)
class OutlierFilterResult:
    """apply_outlier_filters()の戻り値(BUYパイプライン第3次修正2026-07で追加)。

    reliability/blocking_reasonは、外れ値除外そのものが信頼できない場合
    (除外後に残る方式が1件以下になる場合)にLOW/理由コードを持つ。呼び出し側
    (buy_signal_service.py)はこれを買付価格信頼性ゲート(determine_buy_price_
    reliability)へそのまま伝え、無理に少数の値だけで購入判断を確定させない。
    """

    results: list[FairValueMethodResult]
    excluded_count: int = 0
    remaining_count: int = 0
    reliability: BuyPriceReliability = BuyPriceReliability.OK
    blocking_reason: str | None = None


def apply_outlier_filters(
    method_results: list[FairValueMethodResult],
    current_price: Decimal | None = None,
    low_52_week: Decimal | None = None,
) -> OutlierFilterResult:
    """下方(および保険的に上方)の外れ値を集計から除外する(要求仕様10節)。

    `apply_dcf_divergence_filter`がDCF専用・上方乖離専用なのに対し、こちらは
    方式を問わず、現在値・他方式中央値・52週安値との比較で機械的に検出できる
    外れ値を除外する。除外理由は`ValuationExclusionReason`として構造化し、
    `exclusion_reason`(既存の文字列フィールド)にも同じ内容を人が読める形で残す。

    --- BUYパイプライン第3次修正(2026-07)で修正 ---
    有効な方式が3件未満(=2件以下)の場合は外れ値検知そのものを行わない
    (2件では「相手が唯一の比較対象」になり、双方が互いを外れ値とみなし合って
    有効な方式が0件になりうるため)。methods_used_count<=2の低信頼シグナルは
    既存のbuy_price_reliability.py側のTOO_FEW_VALUATION_METHODSゲートに委ねる。
    また、3件以上で外れ値検知を行った結果、残る方式が1件以下になった場合
    (例: 3方式が互いを外れ値とみなし合い全滅する)、その除外結果は採用せず
    除外前の結果へフォールバックし、明示的な低信頼シグナルを返す。
    """
    applicable = [r for r in method_results if r.applicable and r.fair_value is not None]
    if len(applicable) < _MIN_METHODS_FOR_OUTLIER_DETECTION:
        return OutlierFilterResult(
            results=method_results, excluded_count=0, remaining_count=len(applicable)
        )

    filtered: list[FairValueMethodResult] = []
    excluded_count = 0
    for r in method_results:
        if not r.applicable or r.fair_value is None:
            filtered.append(r)
            continue
        other_values = [o.fair_value for o in applicable if o.method != r.method and o.fair_value]
        exclusion = _detect_outlier(r.fair_value, other_values, current_price, low_52_week)
        if exclusion is None:
            filtered.append(r)
            continue
        excluded_count += 1
        filtered.append(
            r.model_copy(
                update={
                    "applicable": False,
                    "fair_value": None,
                    "exclusion_reason": exclusion.message,
                    "exclusion_detail": exclusion,
                }
            )
        )

    remaining_count = len(applicable) - excluded_count
    if remaining_count < _MIN_REMAINING_METHODS_AFTER_FILTER:
        return OutlierFilterResult(
            results=method_results,
            excluded_count=0,
            remaining_count=len(applicable),
            reliability=BuyPriceReliability.LOW,
            blocking_reason=_TOO_FEW_METHODS_AFTER_OUTLIER_FILTER,
        )

    return OutlierFilterResult(
        results=filtered, excluded_count=excluded_count, remaining_count=remaining_count
    )


def build_valuation_summary(
    method_results: list[FairValueMethodResult],
    aggregation_method: str,
    method_weights: dict[str, float] | None,
    usability_config: FairValueUsability,
    current_price: Decimal | None = None,
    low_52_week: Decimal | None = None,
) -> FairValueRange:
    """既存のbuild_fair_value_range()を呼び、統計値(min/max/median/mean/
    dispersion_ratio/methods_used_count)を追加する。applicable=Falseの方式は
    fair_value=Noneに書き換えたうえで渡し、集計対象から確実に除外する。

    valuation_min/valuation_maxは下方外れ値フィルタ適用「前」の全採用方式ベース
    (監査・参考用)、decision_valuation_min/decision_valuation_max・
    valuation_dispersion_ratio・methods_used_count・methods_usedは下方外れ値
    フィルタ適用「後」(実際の購入判断・valuation_anchor算出に使う値)。
    """
    normalized_results = [
        r if r.applicable else r.model_copy(update={"fair_value": None}) for r in method_results
    ]
    all_values = [r.fair_value for r in normalized_results if r.fair_value is not None]

    outlier_filter_result = apply_outlier_filters(normalized_results, current_price, low_52_week)
    base_range = build_fair_value_range(
        outlier_filter_result.results, aggregation_method, method_weights, usability_config
    )

    used_values = [r.fair_value for r in base_range.methods_used if r.fair_value is not None]
    if not used_values:
        return base_range.model_copy(
            update={
                "valuation_min": min(all_values) if all_values else None,
                "valuation_max": max(all_values) if all_values else None,
                "outlier_filter_blocking_reason": outlier_filter_result.blocking_reason,
            }
        )

    decision_min = min(used_values)
    decision_max = max(used_values)
    valuation_median = statistics.median(used_values)
    valuation_mean = sum(used_values, Decimal("0")) / len(used_values)
    dispersion_ratio = float(decision_max / decision_min) if decision_min > 0 else None

    return base_range.model_copy(
        update={
            "valuation_min": min(all_values) if all_values else decision_min,
            "valuation_max": max(all_values) if all_values else decision_max,
            "valuation_median": valuation_median,
            "valuation_mean": valuation_mean,
            "valuation_dispersion_ratio": dispersion_ratio,
            "methods_used_count": len(used_values),
            "decision_valuation_min": decision_min,
            "decision_valuation_max": decision_max,
            "outlier_filter_blocking_reason": outlier_filter_result.blocking_reason,
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
        message = (
            "簡易DCFが他方式の中央値を30%超上回っており、適正価格を"
            "押し上げる方向にのみ作用するため除外"
        )
        return dcf_result.model_copy(
            update={
                "applicable": False,
                "exclusion_reason": message,
                "exclusion_detail": ValuationExclusionReason(
                    code="DCF_UPWARD_DIVERGENCE",
                    message=message,
                    actual_value=dcf_result.fair_value,
                    reference_value=median_others * _DCF_UPWARD_DIVERGENCE_RATIO,
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
