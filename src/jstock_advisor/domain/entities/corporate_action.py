"""企業行動(株式分割等)調整済み値の値オブジェクト(要求仕様3節)。

すべての分析値はadjustment_basis_dateを持ち、基準日が異なる値同士の
計算・比較はcorporate_action_service.require_matching_basis_dates()で
明示的に禁止する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import CorporateActionType


class AdjustedDecimal(ImmutableSnapshot):
    """株価・EPS・BPS・DPS・平均取得単価等、企業行動調整の対象となる金額値。"""

    raw_value: Decimal
    adjusted_value: Decimal
    adjustment_factor: Decimal  # adjusted_value = raw_value / adjustment_factor
    adjustment_basis_date: dt.date
    corporate_action_type: CorporateActionType | None = None
    corporate_action_effective_date: dt.date | None = None
    source: DataSourceReference
    source_timestamp: dt.datetime


class AdjustedShares(ImmutableSnapshot):
    """保有株数・株主優待必要株数等、企業行動調整の対象となる株数値。"""

    raw_value: int
    adjusted_value: int
    adjustment_factor: Decimal  # adjusted_value = round(raw_value * adjustment_factor)
    adjustment_basis_date: dt.date
    corporate_action_type: CorporateActionType | None = None
    corporate_action_effective_date: dt.date | None = None
    source: DataSourceReference
    source_timestamp: dt.datetime
