"""保有銘柄オーナー機能移行(M2)専用の「新データ形状(V2)」書き込みモデル。

owner/holding_idを必須フィールドとして持つ、移行後の正規形状。legacy_shapes.py
と対になる独立モジュールであり、現在の本番アプリケーションコード
(PortfolioService・CLI・LINE会話型UI等)はまだこの形状を一切参照しない
(M3でのアプリケーション本体切替まで、本番は旧テーブル・旧ドメインモデルの
ままで動作し続ける)。

HoldingV2/HoldingsSnapshotEntryV2はHoldingsTableV2/HoldingsSnapshotTableV2
という新しい物理テーブルへ書き込む。PurchaseLotV2は新テーブルを作らず、
既存のPurchaseLotsTable(lot_idキーは変更しない)へowner/holding_idを追加
した形で上書きする(既存CollectionStoreの正規upsert経路をそのまま使う。
生UpdateItemでトップレベル属性を追加する方法は採らない)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from jstock_advisor.domain.entities.enums import AccountType, TransactionType


class HoldingV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    holding_id: str
    owner: str
    stock_code: str
    stock_name: str
    market_segment: str | None = None
    industry: str | None = None
    shares: int
    average_purchase_price: Decimal
    total_purchase_amount: Decimal
    first_purchase_date: dt.date
    last_purchase_date: dt.date
    account_type: AccountType
    investment_purpose: str | None = None
    sell_policy: str | None = None
    cumulative_dividend_received: Decimal = Decimal("0")
    cumulative_benefit_value_received: Decimal = Decimal("0")
    profit_target_price: Decimal | None = None
    profit_target_rate: float | None = None
    memo: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime
    shares_and_price_adjustment_basis_date: dt.date | None = None
    last_sale_date: dt.date | None = None


class PurchaseLotV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lot_id: str
    holding_id: str
    owner: str
    stock_code: str
    purchase_date: dt.date
    shares: int
    purchase_price: Decimal
    fee: Decimal = Decimal("0")
    account_type: AccountType


class HoldingsSnapshotEntryV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    holding_id: str
    owner: str
    stock_code: str
    shares: int
    average_purchase_price: Decimal | None = None
    recorded_at: dt.date
    last_trade_event_type: TransactionType | None = None
    trade_detected_at: dt.date | None = None
    cooldown_until_date: dt.date | None = None
    active_holding: bool = True
