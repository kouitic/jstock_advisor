"""保有銘柄(要求仕様4節・5節)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import AccountType


class PurchaseLot(Entity):
    """1回の購入取引に対応するロット。同一銘柄を複数回購入した場合に対応する。"""

    lot_id: str
    stock_code: str
    purchase_date: dt.date
    shares: int
    purchase_price: Decimal
    fee: Decimal = Decimal("0")
    account_type: AccountType

    def amount(self) -> Decimal:
        return self.purchase_price * self.shares


class Holding(Entity):
    """保有銘柄サマリ。average_purchase_price/total_purchase_amount/shares は
    PurchaseLotの集計値のキャッシュであり、ロット追加・編集時にサービス層が再計算する。
    """

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

    # shares/average_purchase_priceが分割調整済みかどうか・どの基準日時点の値かを
    # 明示する(要求仕様2節)。Noneは企業行動調整サービスが未接続の環境
    # (ローカルCLIでcorporate_action_serviceを注入していない場合等)を示す。
    shares_and_price_adjustment_basis_date: dt.date | None = None


def summarize_lots(lots: list[PurchaseLot]) -> tuple[int, Decimal, Decimal, dt.date, dt.date]:
    """購入ロットの一覧から(保有株数合計, 平均購入単価, 総購入金額,
    初回購入日, 最終購入日)を算出する。

    平均購入単価 = 各購入ロットの購入金額合計 ÷ 保有株数合計(要求仕様5節の式のとおり)。
    """
    if not lots:
        raise ValueError("lots must not be empty")

    total_shares = sum(lot.shares for lot in lots)
    if total_shares <= 0:
        raise ValueError("total_shares must be positive")

    total_amount = sum((lot.amount() for lot in lots), start=Decimal("0"))
    average_price = total_amount / total_shares
    first_date = min(lot.purchase_date for lot in lots)
    last_date = max(lot.purchase_date for lot in lots)
    return total_shares, average_price, total_amount, first_date, last_date
