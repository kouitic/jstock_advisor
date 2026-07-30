"""適正価格の信頼度(2026-07 BUYパイプライン再設計。要求仕様9節・12節・13節)。

`confidence_scoring.py`の共通エンジンとは別に、購入判断基準価格・安全余裕率
バケットを決めるための固有ルールをここに実装する(手法間バラつき率1.60/2.00・
業種別モデル未適用・簡易DCF依存・平準化EPS信頼度)。Recommendation全体の
信頼度は引き続き共通エンジンを使い、両者の保守的な方(min)を最終的に採用する。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jstock_advisor.domain.entities.enums import ConfidenceLevel

_MIN_METHODS_FOR_MEDIUM_OR_HIGH = 2


@dataclass(frozen=True)
class ValuationConfidenceResult:
    level: ConfidenceLevel
    reasons_not_high: list[str] = field(default_factory=list)


def determine_valuation_confidence(
    *,
    methods_used_count: int,
    dispersion_ratio: float | None,
    dispersion_medium_max: float,
    dispersion_auto_buy_block: float,
    industry_model_applied: bool,
    uses_simplified_dcf: bool,
    normalized_eps_confidence: ConfidenceLevel | None,
) -> ValuationConfidenceResult:
    if methods_used_count == 0:
        return ValuationConfidenceResult(
            ConfidenceLevel.LOW, ["有効な適正価格算出手法がありません"]
        )

    reasons_not_high: list[str] = []
    if dispersion_ratio is not None and dispersion_ratio > dispersion_medium_max:
        reasons_not_high.append(
            f"適正価格手法間のばらつきが{dispersion_medium_max}倍を超えています"
        )
    if not industry_model_applied:
        reasons_not_high.append("業種別適正価格モデル未適用")
    if uses_simplified_dcf:
        reasons_not_high.append("簡易DCF(固定割引率・固定成長率の前提)を使用")
    if normalized_eps_confidence is not None and normalized_eps_confidence != ConfidenceLevel.HIGH:
        reasons_not_high.append("平準化EPSの信頼度が十分でない")

    if methods_used_count < _MIN_METHODS_FOR_MEDIUM_OR_HIGH:
        return ValuationConfidenceResult(
            ConfidenceLevel.LOW,
            [*reasons_not_high, "有効な適正価格算出手法が2件未満です"],
        )
    if dispersion_ratio is not None and dispersion_ratio > dispersion_auto_buy_block:
        return ValuationConfidenceResult(
            ConfidenceLevel.LOW,
            [
                *reasons_not_high,
                f"適正価格手法間のばらつきが{dispersion_auto_buy_block}倍を超えており"
                "自動購入判定を禁止します",
            ],
        )

    if reasons_not_high:
        return ValuationConfidenceResult(ConfidenceLevel.MEDIUM, reasons_not_high)
    return ValuationConfidenceResult(ConfidenceLevel.HIGH, [])
