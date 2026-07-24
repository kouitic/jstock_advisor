"""実際の売買記録(要求仕様27節)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import AccountType, SkipReason, TransactionType


class Transaction(Entity):
    transaction_id: str
    recommendation_id: str | None = None
    stock_code: str
    transaction_type: TransactionType
    execution_date: dt.date
    shares: int
    execution_price: Decimal
    fee: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")
    account_type: AccountType | None = None
    followed_recommendation: bool
    price_diff_from_recommendation: Decimal | None = None
    reason: str | None = None
    memo: str | None = None
    created_at: dt.datetime


class SkippedRecommendation(Entity):
    """推奨に従わなかった場合の記録(要求仕様27節)。"""

    recommendation_id: str
    skip_reason: SkipReason
    reason_detail: str | None = None
    created_at: dt.datetime
