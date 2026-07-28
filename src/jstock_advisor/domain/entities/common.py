"""複数エンティティで共有する値オブジェクト。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from pydantic import model_validator

from jstock_advisor.domain.entities._legacy_migration import remap_legacy_fields
from jstock_advisor.domain.entities.base import Entity, ImmutableSnapshot
from jstock_advisor.domain.entities.enums import PriceFieldBasis, SourceType


class DataSourceReference(ImmutableSnapshot):
    """判定に使用したデータの出典と取得日時(要求仕様13節・14節)。"""

    provider: str
    fetched_at: dt.datetime
    detail: str | None = None

    # --- データソース優先順位(要求仕様14節)で追加。既定はCONTRACTED_PROVIDER
    # (yfinance等の契約データプロバイダ)とし、各Provider実装側で必要に応じて上書きする ---
    source_type: SourceType = SourceType.CONTRACTED_PROVIDER
    primary_source_flag: bool = False
    source_url: str | None = None
    source_title: str | None = None
    source_published_at: dt.datetime | None = None


class PriceWithRationale(ImmutableSnapshot):
    """価格とその算出根拠(要求仕様10節・14節)。

    basisは、priceが現在値と一致する場合に「実際の目標価格」なのか
    「即時執行の目安」「監視開始価格(売却推奨ではない)」なのかを明示する
    (要求仕様11節: 現在値をデフォルト補完した結果と、意図的に現在値と
    一致させた結果を区別できるようにする)。
    """

    price: Decimal
    rationale: str
    basis: PriceFieldBasis = PriceFieldBasis.TARGET_PRICE


class BuyPriceLevels(ImmutableSnapshot):
    """推奨買値3段階(要求仕様10節)。"""

    tentative: PriceWithRationale | None = None  # 打診買い価格
    standard: PriceWithRationale | None = None  # 標準買い価格
    aggressive: PriceWithRationale | None = None  # 積極買い価格


_SELL_PRICE_LEVELS_LEGACY_FIELD_MAP = {
    "partial_take_start": "partial_profit_start_price",
    "profit_take_recommended": "recommended_limit_price",
    "full_take_consider": "full_profit_consideration_price",
    "premise_deterioration_target": "stop_review_price",
    "reassessment_price": "reevaluation_price_upside",
}


class SellPriceLevels(ImmutableSnapshot):
    """推奨売値(要求仕様10節・14節)。

    各フィールドの意味は明確に分離する(要求仕様11節):
    - partial_profit_start_price: 一部利確を開始する条件価格
    - recommended_limit_price: 実際に指値候補として提示する価格
    - full_profit_consideration_price: 全利確を再検討する条件価格
    - reevaluation_price_upside: 上昇時の再評価価格
    - reevaluation_price_downside: 下落時の再評価価格(投資前提再確認価格を兼ねる)
    - stop_review_price: 損切り・投資前提再確認価格(sell_signal側の悪化判定用、
      将来の価格水準であり現在値のコピーではない)
    - trailing_stop_reference_price: トレーリングストップ参考価格(モメンタム層が必要、
      未実装の間は常にNone)
    - immediate_execution_price: 即時執行が真に必要な場合(URGENT_REVIEW等)にのみ
      設定する現在値ベースの参考価格(2026-07仕様§7)。stop_review_price等の
      「将来の再評価条件」とは意味を混同しない。

    算出不能な場合はNone(現在値へのフォールバックは行わない)。現在値と一致する
    値を意図的に返す場合は、PriceWithRationale.basisで即時執行目安か監視専用かを
    明示する。
    """

    partial_profit_start_price: PriceWithRationale | None = None
    recommended_limit_price: PriceWithRationale | None = None
    full_profit_consideration_price: PriceWithRationale | None = None
    reevaluation_price_upside: PriceWithRationale | None = None
    reevaluation_price_downside: PriceWithRationale | None = None
    stop_review_price: PriceWithRationale | None = None
    trailing_stop_reference_price: PriceWithRationale | None = None
    immediate_execution_price: PriceWithRationale | None = None

    @model_validator(mode="before")
    @classmethod
    def _remap_legacy_field_names(cls, data: object) -> object:
        return remap_legacy_fields(data, _SELL_PRICE_LEVELS_LEGACY_FIELD_MAP)


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
