"""複数エンティティで共有する値オブジェクト。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.base import Entity, ImmutableSnapshot


class DataSourceReference(ImmutableSnapshot):
    """判定に使用したデータの出典と取得日時(要求仕様13節)。"""

    provider: str
    fetched_at: dt.datetime
    detail: str | None = None


class PriceWithRationale(ImmutableSnapshot):
    """価格とその算出根拠(要求仕様10節・14節)。"""

    price: Decimal
    rationale: str


class BuyPriceLevels(ImmutableSnapshot):
    """推奨買値3段階(要求仕様10節)。"""

    tentative: PriceWithRationale | None = None  # 打診買い価格
    standard: PriceWithRationale | None = None  # 標準買い価格
    aggressive: PriceWithRationale | None = None  # 積極買い価格


class SellPriceLevels(ImmutableSnapshot):
    """推奨売値(要求仕様14節)。"""

    partial_take_start: PriceWithRationale | None = None  # 一部利確開始価格
    profit_take_recommended: PriceWithRationale | None = None  # 利確推奨価格
    full_take_consider: PriceWithRationale | None = None  # 全株利確検討価格
    premise_deterioration_target: PriceWithRationale | None = None  # 投資前提悪化時の売却目安価格
    reassessment_price: PriceWithRationale | None = None  # 保有継続判断を再評価する価格


class ScoreBreakdown(ImmutableSnapshot):
    """買い候補スコアの内訳(要求仕様15節)。合計は100点満点。"""

    total_yield_attractiveness: float
    dividend_sustainability: float
    financial_health: float
    undervaluation: float
    shareholder_benefit_value: float
    earnings_stability: float
    price_stability: float
    total: float


class BenefitUtilityCoefficients(Entity):
    """銘柄ごとに上書き可能な株主優待の利用可能性係数(要求仕様7節)。"""

    cash_equivalent: float = 1.0
    versatile_point: float = 0.9
    in_house_service: float = 0.7
    in_house_product: float = 0.6
    discount_voucher: float = 0.3
    lottery_or_commemorative: float = 0.0
