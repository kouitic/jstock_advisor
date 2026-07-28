"""適正価格レンジの集約と使用可否判定(要求仕様8節)。

単一の適正価格を絶対値として扱わず、弱気/中立/強気のレンジとして提示する。
以下の場合は適正価格を売買判定に使用できない(usable_for_trading_judgment=False):
- 有効な手法数がmin_methods_required未満
- 手法間の最大値/最小値がmax_method_spread_ratio倍以上乖離
"""

from __future__ import annotations

from jstock_advisor.config.models import FairValueUsability
from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.entities.valuation import FairValueMethodResult, FairValueRange
from jstock_advisor.domain.valuation.fair_value import aggregate_fair_value


def build_fair_value_range(
    method_results: list[FairValueMethodResult],
    aggregation_method: str,
    method_weights: dict[str, float] | None,
    usability_config: FairValueUsability,
) -> FairValueRange:
    used = [r for r in method_results if r.fair_value is not None]
    excluded = [r for r in method_results if r.fair_value is None]

    if not used:
        return FairValueRange(
            bear=None,
            neutral=None,
            bull=None,
            overall_confidence=ConfidenceLevel.LOW,
            methods_used=used,
            methods_excluded=excluded,
            usable_for_trading_judgment=False,
            unusable_reason="有効な適正価格手法が一つもありません",
        )

    values = [r.fair_value for r in used if r.fair_value is not None]
    bear = min(values)
    bull = max(values)
    candidates = {r.method: r.fair_value for r in used}
    neutral = aggregate_fair_value(candidates, aggregation_method, method_weights)

    usable = True
    unusable_reason = None
    if len(used) < usability_config.min_methods_required:
        usable = False
        unusable_reason = (
            f"有効な適正価格手法が{len(used)}件しかなく"
            f"({usability_config.min_methods_required}件必要)、使用できません"
        )
    elif bear > 0 and float(bull / bear) >= usability_config.max_method_spread_ratio:
        usable = False
        unusable_reason = (
            f"手法間の乖離が{usability_config.max_method_spread_ratio}倍以上"
            f"({bear}円〜{bull}円)のため、使用できません"
        )

    confidences = [r.confidence for r in used]
    if not usable or any(c == ConfidenceLevel.LOW for c in confidences):
        overall_confidence = ConfidenceLevel.LOW
    elif len(used) >= 3 and all(c == ConfidenceLevel.HIGH for c in confidences):
        overall_confidence = ConfidenceLevel.HIGH
    else:
        overall_confidence = ConfidenceLevel.MEDIUM

    return FairValueRange(
        bear=bear,
        neutral=neutral,
        bull=bull,
        overall_confidence=overall_confidence,
        methods_used=used,
        methods_excluded=excluded,
        usable_for_trading_judgment=usable,
        unusable_reason=unusable_reason,
    )
