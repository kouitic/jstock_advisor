"""監査ログサービス(要求仕様3節 audit_service、13節・21節)。

買い判定・利確判定・投資前提悪化売却判定について、除外・HOLD・データ取得エラーを
含むすべての判定過程を、入力値・計算式・出力値・データ出典・ルールバージョンとともに
記録する。呼び出し側は入力値/出力値をJSON化可能な値(str/int/float/bool/None/
それらのdict・list)として渡すこと(Decimal等は事前にstr化する)。
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from jstock_advisor.domain.entities.audit import AuditLogEntry
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository

logger = logging.getLogger(__name__)

# 通知検証モード機能(2026-08)コードレビュー対応: 呼び出し元(HoldingDecisionService等)
# ごとにif not validationガードを散在させず、AuditServiceのrecord()/record_if_absent()と
# いう2つの永続化choke pointだけをexecution_context対応させることで、監査ログ書き込み
# 漏れが構造的に発生しにくい設計にする(LineNotificationServiceと同じ流儀)。
_DEFAULT_EXECUTION_CONTEXT = ExecutionContext.normal()


class AuditService:
    def __init__(
        self,
        repository: AuditLogRepository | None = None,
        execution_context: ExecutionContext = _DEFAULT_EXECUTION_CONTEXT,
    ) -> None:
        self._repository = repository or AuditLogRepository()
        self._execution_context = execution_context

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
        raw_input_data: dict[str, Any] | None = None,
        adjusted_input_data: dict[str, Any] | None = None,
        corporate_actions_applied: list[str] | None = None,
        fair_value_results: list[dict[str, Any]] | None = None,
        triggered_rules: list[str] | None = None,
        suppressed_rules: list[str] | None = None,
        consistency_validation_result: dict[str, Any] | None = None,
        data_quality_score: float | None = None,
        confidence_score: float | None = None,
        notification_values: dict[str, Any] | None = None,
        source_metadata: list[dict[str, Any]] | None = None,
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
            raw_input_data=raw_input_data,
            adjusted_input_data=adjusted_input_data,
            corporate_actions_applied=corporate_actions_applied or [],
            fair_value_results=fair_value_results,
            triggered_rules=triggered_rules or [],
            suppressed_rules=suppressed_rules or [],
            consistency_validation_result=consistency_validation_result,
            data_quality_score=data_quality_score,
            confidence_score=confidence_score,
            notification_values=notification_values,
            source_metadata=source_metadata,
        )
        if self._execution_context.is_validation:
            logger.info(
                "VALIDATION MODE audit suppressed (not persisted) decision_type=%s stock_code=%s "
                "audit_id=%s",
                decision_type,
                stock_code,
                entry.audit_id,
            )
            return entry
        self._repository.save(entry)
        return entry

    def record_if_absent(
        self,
        audit_id: str,
        decision_type: str,
        stock_code: str | None,
        input_values: dict[str, Any],
        calculation_formulas: dict[str, str],
        output_values: dict[str, Any],
        data_sources: list[DataSourceReference],
        rule_version: str,
        timestamp: dt.datetime,
        raw_input_data: dict[str, Any] | None = None,
        adjusted_input_data: dict[str, Any] | None = None,
        corporate_actions_applied: list[str] | None = None,
        fair_value_results: list[dict[str, Any]] | None = None,
        triggered_rules: list[str] | None = None,
        suppressed_rules: list[str] | None = None,
        consistency_validation_result: dict[str, Any] | None = None,
        data_quality_score: float | None = None,
        confidence_score: float | None = None,
        notification_values: dict[str, Any] | None = None,
        source_metadata: list[dict[str, Any]] | None = None,
    ) -> AuditLogEntry | None:
        """運用ハードニング第3弾3節: record()と同じ引数だが、audit_idを呼び出し側が
        指定する(uuid4のランダム生成をしない)。既にそのaudit_idの記録が存在すれば
        何もせずNoneを返す(冪等な新規記録専用)。呼び出し側(record_batch_audit等)が
        「バッチ完了処理を再試行しても、監査ログへ書き込み成功後・後続の完了処理前に
        中断した場合に監査ログが重複しない」ことを保証したい場合に、決定的な
        audit_id(例: f"watchlist_batch_audit:{batch_id}")を渡して使う。
        """
        entry = AuditLogEntry(
            audit_id=audit_id,
            timestamp=timestamp,
            stock_code=stock_code,
            decision_type=decision_type,
            input_values=input_values,
            calculation_formulas=calculation_formulas,
            output_values=output_values,
            data_sources=data_sources,
            rule_version=rule_version,
            raw_input_data=raw_input_data,
            adjusted_input_data=adjusted_input_data,
            corporate_actions_applied=corporate_actions_applied or [],
            fair_value_results=fair_value_results,
            triggered_rules=triggered_rules or [],
            suppressed_rules=suppressed_rules or [],
            consistency_validation_result=consistency_validation_result,
            data_quality_score=data_quality_score,
            confidence_score=confidence_score,
            notification_values=notification_values,
            source_metadata=source_metadata,
        )
        if self._execution_context.is_validation:
            logger.info(
                "VALIDATION MODE audit suppressed (not persisted) decision_type=%s stock_code=%s "
                "audit_id=%s",
                decision_type,
                stock_code,
                audit_id,
            )
            return entry
        if not self._repository.save_if_absent(entry):
            return None
        return entry
