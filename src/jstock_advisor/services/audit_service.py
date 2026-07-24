"""監査ログサービス(要求仕様3節 audit_service、13節・21節)。

買い判定・利確判定・投資前提悪化売却判定について、除外・HOLD・データ取得エラーを
含むすべての判定過程を、入力値・計算式・出力値・データ出典・ルールバージョンとともに
記録する。呼び出し側は入力値/出力値をJSON化可能な値(str/int/float/bool/None/
それらのdict・list)として渡すこと(Decimal等は事前にstr化する)。
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from jstock_advisor.domain.entities.audit import AuditLogEntry
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository


class AuditService:
    def __init__(self, repository: AuditLogRepository | None = None) -> None:
        self._repository = repository or AuditLogRepository()

    def record(
        self,
        decision_type: str,
        stock_code: str | None,
        input_values: dict[str, Any],
        calculation_formulas: dict[str, str],
        output_values: dict[str, Any],
        data_sources: list[DataSourceReference],
        rule_version: str,
        timestamp: dt.datetime,
    ) -> AuditLogEntry:
        entry = AuditLogEntry(
            audit_id=str(uuid.uuid4()),
            timestamp=timestamp,
            stock_code=stock_code,
            decision_type=decision_type,
            input_values=input_values,
            calculation_formulas=calculation_formulas,
            output_values=output_values,
            data_sources=data_sources,
            rule_version=rule_version,
        )
        self._repository.save(entry)
        return entry
