"""監査ログ(要求仕様13節・21節)。すべての判定について、入力値・計算式・出力・出典を記録する。"""

from __future__ import annotations

import datetime as dt
from typing import Any

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.common import DataSourceReference


class AuditLogEntry(Entity):
    audit_id: str
    timestamp: dt.datetime
    stock_code: str | None = None
    decision_type: str  # 例: "buy_signal", "profit_taking", "sell_signal", "screening"
    input_values: dict[str, Any]
    calculation_formulas: dict[str, str]
    output_values: dict[str, Any]
    data_sources: list[DataSourceReference]
    rule_version: str
