"""保有銘柄オーナー機能移行(M2)専用の「旧データ形状」読み取りモデル。

現在の本番ドメインモデル(Holding/PurchaseLot/HoldingsSnapshotEntry)を
直接importして流用せず、現時点のスキーマを凍結した独立のコピーとして
定義する。理由: 将来(M3以降)本番モデル側にowner/holding_idが必須
フィールドとして追加された場合、本番モデルを直接参照していると、この
移行モジュール(将来再実行される可能性がある)が「旧データにowner
フィールドが無い」ことを理由に検証エラーとなってしまう。旧データの形状を
恒久的に正しく読めることを保証するため、あえて本番モデルへの依存を断つ。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from jstock_advisor.domain.entities.enums import AccountType, TransactionType


class LegacyHoldingV1(BaseModel):
    """owner概念導入前のHoldingsTableの形状(domain/entities/holding.py:Holding)。"""

    model_config = ConfigDict(extra="forbid")

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


class LegacyPurchaseLotV1(BaseModel):
    """owner概念導入前のPurchaseLotsTableの形状(domain/entities/holding.py:PurchaseLot)。

    PurchaseLotsTableはHoldingsTable等と異なり新テーブルを作らず、既存の
    "purchase_lots.json"へowner/holding_idを直接追加する(v4プラン11節)。
    そのため、移行を一度実行した後に再度preflight/migrationを実行すると、
    このモデルは既にowner/holding_idが追加済みのレコードを読むことになる。
    冪等な再実行を壊さないよう、ここだけextra="ignore"とし、未知の追加
    フィールド(既に移行済みのowner/holding_id)を許容して無視する
    (他のLegacy*V1モデルは対応するV1テーブル自体を一切書き換えないため
    extra="forbid"のままでよい)。
    """

    model_config = ConfigDict(extra="ignore")

    lot_id: str
    stock_code: str
    purchase_date: dt.date
    shares: int
    purchase_price: Decimal
    fee: Decimal = Decimal("0")
    account_type: AccountType


class LegacyHoldingsSnapshotEntryV1(BaseModel):
    """owner概念導入前のHoldingsSnapshotTableの形状
    (domain/entities/holdings_snapshot.py:HoldingsSnapshotEntry)。"""

    model_config = ConfigDict(extra="forbid")

    stock_code: str
    shares: int
    average_purchase_price: Decimal | None = None
    recorded_at: dt.date
    last_trade_event_type: TransactionType | None = None
    trade_detected_at: dt.date | None = None
    cooldown_until_date: dt.date | None = None
    active_holding: bool = True


class LegacyBaselineSequenceCounterV1(BaseModel):
    """holding_id(旧: stock_codeの1:1エイリアス)ごとのbaseline連番カウンタ
    (infrastructure/aws/baseline_sequence.py:_BaselineSequenceCounter)。

    本番実装はDynamoDB版でトップレベル属性のみ(dataブロブを使わない)で
    保存されるため、この移行モジュールでも同じ形状をローカル/AWS双方の
    読み書きに使う(baseline_migration.py参照)。current_versionは移行の
    前後で値を変更しない(リセットしない)。
    """

    model_config = ConfigDict(extra="forbid")

    holding_id: str
    current_version: int
    updated_at: dt.datetime
