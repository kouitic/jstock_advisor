"""購入判定の整合性チェック(2026-07 BUYパイプライン再設計。要求仕様20節)。

パイプライン自体が各不変条件を満たすよう設計されていても(例: 信頼度LOWでは
安全余裕率が算出されずbuy_price_levelsが生成されない)、最終的なRecommendation
確定前に独立して再検証する(二重の安全策)。不整合を検出した場合、呼び出し側は
通知を継続せずbuy_action = BuyAction.MANUAL_REVIEWへ切り替える。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.config.models import BuyDecisionRulesConfig
from jstock_advisor.domain.entities.enums import BUY_FAMILY_ACTIONS, BuyAction, ConfidenceLevel


@dataclass(frozen=True)
class ConsistencyViolation:
    code: str
    message: str


def validate_buy_recommendation(
    *,
    action: BuyAction,
    current_price: Decimal,
    entry_price: Decimal | None,
    standard_price: Decimal | None,
    strong_price: Decimal | None,
    confidence: ConfidenceLevel,
    business_days_to_earnings: int | None,
    valuation_dispersion_ratio: float | None,
    config: BuyDecisionRulesConfig,
) -> list[ConsistencyViolation]:
    violations: list[ConsistencyViolation] = []

    if entry_price is not None and standard_price is not None and entry_price < standard_price:
        violations.append(
            ConsistencyViolation(
                "PRICE_ORDER_VIOLATION_ENTRY_STANDARD",
                "打診買い価格が標準買い価格を下回っています",
            )
        )
    if standard_price is not None and strong_price is not None and standard_price < strong_price:
        violations.append(
            ConsistencyViolation(
                "PRICE_ORDER_VIOLATION_STANDARD_STRONG",
                "標準買い価格が積極買い価格を下回っています",
            )
        )

    if action not in BUY_FAMILY_ACTIONS:
        return violations

    if entry_price is None:
        violations.append(
            ConsistencyViolation(
                "BUY_ACTION_WITHOUT_PRICE_LEVELS",
                "買付価格が算出されていないのにBUY系判定になっています",
            )
        )
    elif current_price > entry_price:
        violations.append(
            ConsistencyViolation(
                "CURRENT_PRICE_ABOVE_ENTRY_PRICE",
                "現在値が打診買い価格を上回っているのにBUY系判定になっています",
            )
        )

    if confidence == ConfidenceLevel.LOW:
        violations.append(
            ConsistencyViolation(
                "LOW_CONFIDENCE_BUY_ACTION",
                "信頼度LOWにもかかわらずBUY系判定になっています",
            )
        )

    if (
        business_days_to_earnings is not None
        and business_days_to_earnings <= config.earnings_window.block_buy_business_days
    ):
        violations.append(
            ConsistencyViolation(
                "EARNINGS_WINDOW_VIOLATION",
                "決算直前にもかかわらずBUY系判定になっています",
            )
        )

    if (
        valuation_dispersion_ratio is not None
        and valuation_dispersion_ratio > config.valuation_dispersion.auto_buy_block
    ):
        violations.append(
            ConsistencyViolation(
                "VALUATION_DISPERSION_TOO_HIGH",
                "適正価格手法間のばらつきが大きいにもかかわらずBUY系判定になっています",
            )
        )

    return violations
