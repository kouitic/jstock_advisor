"""買付価格3段階の算出(2026-07 BUYパイプライン再設計。要求仕様6節・11節)。

固定95%/90%/85%方式(旧`buy_price.py`)を廃止し、購入判断基準価格
(valuation_anchor)から安全余裕率(margin_of_safety)を差し引いて算出する。
entry(打診買い) >= standard(標準買い) >= strong(積極買い)の順序は
`BuyPriceLevels`側のバリデータで保証される。
"""

from __future__ import annotations

from decimal import Decimal

from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.valuation.fair_value import round_yen
from jstock_advisor.domain.valuation.margin_of_safety import MarginOfSafetyResult


def compute_buy_price_levels(
    valuation_anchor: Decimal | None, margin_result: MarginOfSafetyResult
) -> BuyPriceLevels:
    if (
        valuation_anchor is None
        or not margin_result.allowed
        or margin_result.entry_margin is None
        or margin_result.standard_margin is None
        or margin_result.strong_margin is None
    ):
        return BuyPriceLevels()

    def _level(margin: Decimal, label: str) -> PriceWithRationale:
        price = round_yen(valuation_anchor * (1 - margin))
        return PriceWithRationale(
            price=price,
            rationale=(
                f"購入判断基準価格({valuation_anchor}円)から"
                f"安全余裕{margin * 100:.0f}%を確保({label})"
            ),
        )

    return BuyPriceLevels(
        entry=_level(margin_result.entry_margin, "打診買い"),
        standard=_level(margin_result.standard_margin, "標準買い"),
        strong=_level(margin_result.strong_margin, "積極買い"),
    )
