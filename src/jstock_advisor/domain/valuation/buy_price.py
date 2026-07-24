"""推奨買値の算出(要求仕様10節)。"""

from __future__ import annotations

from decimal import Decimal

from jstock_advisor.config.models import RecommendedBuyPrice
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.valuation.fair_value import round_yen


def compute_recommended_buy_prices(
    fair_value: Decimal, ratios: RecommendedBuyPrice
) -> BuyPriceLevels:
    def _level(ratio: float, label: str) -> PriceWithRationale:
        price = round_yen(fair_value * Decimal(str(ratio)))
        return PriceWithRationale(
            price=price,
            rationale=f"最終適正価格({fair_value}円)の{ratio * 100:.0f}%({label})",
        )

    return BuyPriceLevels(
        tentative=_level(ratios.tentative_buy_ratio, "打診買い"),
        standard=_level(ratios.standard_buy_ratio, "標準買い"),
        aggressive=_level(ratios.aggressive_buy_ratio, "積極買い"),
    )
