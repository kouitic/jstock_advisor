"""割安度スコアのカテゴリ別上限点(2026-07 BUYパイプライン再設計。要求仕様15節)。

PERが過去中央値以下/PBRが過去中央値以下/配当利回りが過去平均以上/現在値が
適正価格以下/52週高値からの下落/直近株価下落は相互に強く関連するため、
単純加点すると同じ価格下落を重複評価する可能性がある。カテゴリへ分け、
カテゴリごとに上限点を設定することで二重加点を防ぐ。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jstock_advisor.config.models import UndervaluationCategoryCaps

if TYPE_CHECKING:
    from jstock_advisor.domain.scoring.score import UndervaluationSignals

# カテゴリと、それに属するUndervaluationSignalsのフィールド名。
_CATEGORY_SIGNAL_FIELDS: dict[str, tuple[str, ...]] = {
    "valuation_multiple": ("per_below_median", "pbr_below_median"),
    "yield": ("dividend_yield_above_historical_average",),
    "fair_value": ("below_fair_value",),
    # 52週高値からの下落・前日比マイナスだけで高い割安点を付けない。
    # price_down_despite_stable_earningsは「業績悪化が理由ではない」ことを
    # 確認できた場合のみTrueになる信号であり、drawdown単独より慎重な評価とする。
    "market_price_action": ("drawdown_from_52w_high", "price_down_despite_stable_earnings"),
}

_CATEGORY_LABELS: dict[str, str] = {
    "valuation_multiple": "PER・PBR倍率",
    "yield": "配当利回り",
    "fair_value": "適正価格対比",
    "market_price_action": "株価下落(財務悪化以外の理由に限る)",
}


def _category_caps(config: UndervaluationCategoryCaps) -> dict[str, float]:
    return {
        "valuation_multiple": config.valuation_multiple,
        "yield": config.yield_,
        "fair_value": config.fair_value,
        "market_price_action": config.market_price_action,
    }


def score_undervaluation_categories(
    signals: UndervaluationSignals, config: UndervaluationCategoryCaps
) -> tuple[float, str]:
    available = signals.available()
    caps = _category_caps(config)
    total_score = 0.0
    formula_parts: list[str] = []

    for category, field_names in _CATEGORY_SIGNAL_FIELDS.items():
        cap = caps[category]
        category_signals = {name: available[name] for name in field_names if name in available}
        if not category_signals:
            continue
        met = sum(1 for v in category_signals.values() if v)
        total = len(category_signals)
        category_score = cap * (met / total)
        total_score += category_score
        formula_parts.append(f"{_CATEGORY_LABELS[category]}:{met}/{total}件×{cap}点")

    if not formula_parts:
        return 0.0, "割安条件を判定するデータがないため0点"
    formula = "割安度(カテゴリ別上限点): " + ", ".join(formula_parts)
    return total_score, formula
