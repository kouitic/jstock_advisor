"""割安度スコアのカテゴリ別上限点(2026-07 BUYパイプライン再設計。要求仕様15節)。

PERが過去中央値以下/PBRが過去中央値以下/配当利回りが過去平均以上/現在値が
適正価格以下/52週高値からの下落/直近株価下落は相互に強く関連するため、
単純加点すると同じ価格下落を重複評価する可能性がある。カテゴリへ分け、
カテゴリごとに上限点を設定することで二重加点を防ぐ。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from jstock_advisor.config.models import UndervaluationCategoryCaps
from jstock_advisor.domain.entities.enums import EvidenceCoverageStatus

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


@dataclass(frozen=True)
class UndervaluationCategoryDetail:
    """割安度スコアのカテゴリ1件分の判定時点明細(Issue #22 Phase 3.5観測性強化)。

    従来は(合計点, formula文字列)しか外部へ出ておらず、カテゴリごとの
    得点・満点・判定可否がRecommendationへ残らなかった(available 0件の
    カテゴリはformulaにすら現れない)。将来のスコア責務分離のshadow検証で
    「現在値・現在configからの再構築」を不要にするため、判定時点の明細を
    構造化して返す。scoreの計算自体はscore_undervaluation_categories()と
    完全に同一(同関数が本明細から合計点を導出する。二重実装しない)。

    stateはEvidenceCoverageStatusの3値のみを使う。NOT_APPLICABLEは
    「判定時点の事実だけから明確に評価対象外と断定できる」場合にのみ使う
    方針だが、割安度カテゴリには現時点でそのような判定基準が存在しないため
    ここでは発生しない(EVALUATED/NOT_EVALUATEDのみ)。
    """

    category: str
    cap: float
    score: float
    signals_met: int
    signals_available: int
    signals_defined: int
    state: EvidenceCoverageStatus
    # 定義済み全シグナルの生の値(判定不能=None を含む。available()と違い
    # Noneを落とさない。「どのシグナルが判定不能だったか」を残すため)。
    signal_results: dict[str, bool | None]


def build_undervaluation_category_details(
    signals: UndervaluationSignals, config: UndervaluationCategoryCaps
) -> list[UndervaluationCategoryDetail]:
    """4カテゴリすべての判定時点明細を返す(available 0件のカテゴリも含む)。"""
    available = signals.available()
    caps = _category_caps(config)
    details: list[UndervaluationCategoryDetail] = []
    for category, field_names in _CATEGORY_SIGNAL_FIELDS.items():
        cap = caps[category]
        signal_results: dict[str, bool | None] = {
            name: getattr(signals, name) for name in field_names
        }
        category_signals = {name: available[name] for name in field_names if name in available}
        met = sum(1 for v in category_signals.values() if v)
        n_available = len(category_signals)
        category_score = cap * (met / n_available) if n_available else 0.0
        state = (
            EvidenceCoverageStatus.EVALUATED
            if n_available
            else EvidenceCoverageStatus.NOT_EVALUATED
        )
        details.append(
            UndervaluationCategoryDetail(
                category=category,
                cap=cap,
                score=category_score,
                signals_met=met,
                signals_available=n_available,
                signals_defined=len(field_names),
                state=state,
                signal_results=signal_results,
            )
        )
    return details


def score_undervaluation_categories(
    signals: UndervaluationSignals, config: UndervaluationCategoryCaps
) -> tuple[float, str]:
    details = build_undervaluation_category_details(signals, config)
    total_score = 0.0
    formula_parts: list[str] = []

    for detail in details:
        if detail.signals_available == 0:
            continue
        total_score += detail.score
        formula_parts.append(
            f"{_CATEGORY_LABELS[detail.category]}:"
            f"{detail.signals_met}/{detail.signals_available}件×{detail.cap}点"
        )

    if not formula_parts:
        return 0.0, "割安条件を判定するデータがないため0点"
    formula = "割安度(カテゴリ別上限点): " + ", ".join(formula_parts)
    return total_score, formula
