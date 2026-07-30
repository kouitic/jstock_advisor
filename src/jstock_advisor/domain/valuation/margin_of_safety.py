"""必要安全余裕率(2026-07 BUYパイプライン再設計。要求仕様7節・8節)。

固定95%/90%/85%方式を廃止し、適正価格の信頼度を基準とした安全余裕率に、
リスクに応じた加算を行う。信頼度LOWの場合は自動の買い推奨価格を生成しない。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.config.models import MarginOfSafetyConfig
from jstock_advisor.domain.entities.common import MarginAdjustment
from jstock_advisor.domain.entities.enums import ConfidenceLevel

_ADJUSTMENT_REASON_LABELS: dict[str, str] = {
    "earnings_within_3_business_days": "次回決算まで3営業日以内",
    "earnings_within_7_business_days": "次回決算まで7営業日以内",
    "high_valuation_dispersion": "適正価格手法間のばらつきが大きい",
    "very_high_valuation_dispersion": "適正価格手法間のばらつきが非常に大きい",
    "industry_model_not_applied": "業種別適正価格モデル未適用",
    "cyclical_industry": "市況の影響を受けやすい業種",
    "small_cap_or_low_liquidity": "時価総額が小さい、または流動性が低い",
    "volatile_earnings": "業績のブレが大きい",
    "temporary_earnings_boost_risk": "一時的な利益上振れの可能性",
    "major_customer_dependency": "主要顧客への依存度が高い",
    "data_quality_warning": "データ品質に懸念がある",
}


@dataclass(frozen=True)
class MarginOfSafetyResult:
    entry_margin: Decimal | None
    standard_margin: Decimal | None
    strong_margin: Decimal | None
    adjustments: tuple[MarginAdjustment, ...] = ()
    # 信頼度LOWの場合はFalse(自動の買い推奨価格を生成しない)。
    allowed: bool = True


def compute_margin_of_safety(
    valuation_confidence: ConfidenceLevel,
    adjustment_codes: list[str],
    config: MarginOfSafetyConfig,
) -> MarginOfSafetyResult:
    if valuation_confidence == ConfidenceLevel.LOW:
        return MarginOfSafetyResult(None, None, None, (), allowed=False)

    tier = (
        config.confidence.high
        if valuation_confidence == ConfidenceLevel.HIGH
        else config.confidence.medium
    )

    adjustments: list[MarginAdjustment] = []
    total_adjustment = Decimal("0")
    for code in adjustment_codes:
        amount = getattr(config.adjustments, code, None)
        if amount is None:
            continue
        decimal_amount = Decimal(str(amount))
        total_adjustment += decimal_amount
        adjustments.append(
            MarginAdjustment(
                code=code,
                adjustment=decimal_amount,
                reason=_ADJUSTMENT_REASON_LABELS.get(code, code),
            )
        )

    maximum = Decimal(str(config.maximum))

    def _capped(base: float) -> Decimal:
        return min(Decimal(str(base)) + total_adjustment, maximum)

    return MarginOfSafetyResult(
        entry_margin=_capped(tier.entry),
        standard_margin=_capped(tier.standard),
        strong_margin=_capped(tier.strong),
        adjustments=tuple(adjustments),
        allowed=True,
    )
