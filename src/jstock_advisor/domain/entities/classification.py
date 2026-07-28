"""銘柄タイプ分類の結果(要求仕様7節)。"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import ConfidenceLevel, StockType


class StockTypeClassification(ImmutableSnapshot):
    stock_code: str
    classified_at: dt.datetime
    types: list[StockType]  # 複合タイプを許容(例: CYCLICAL + INCOME)
    primary_type: StockType | None
    confidence: ConfidenceLevel
    classification_basis: list[str]
    data_sources: list[DataSourceReference]
