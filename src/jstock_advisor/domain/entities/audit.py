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

    # --- トレーサビリティ拡張(要求仕様19節)で追加。すべて未設定時はNone/空のまま
    # 保存する(推測で埋めない)。既存レコードはOptionalのため読み込み時も互換 ---
    raw_input_data: dict[str, Any] | None = None  # 企業行動調整前の生値
    adjusted_input_data: dict[str, Any] | None = None  # 企業行動調整後の値
    corporate_actions_applied: list[str] = []  # 適用した企業行動調整の説明
    fair_value_results: list[dict[str, Any]] | None = None  # 適正価格手法別の結果
    triggered_rules: list[str] = []
    suppressed_rules: list[str] = []  # 条件を満たしたが緩和要因等で不採用となったルール
    consistency_validation_result: dict[str, Any] | None = None
    data_quality_score: float | None = None
    confidence_score: float | None = None
    notification_values: dict[str, Any] | None = None  # 実際に通知本文へ出力した値
    source_metadata: list[dict[str, Any]] | None = None  # データ出典の優先順位情報
