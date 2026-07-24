"""ルールバージョン管理・改善提案(要求仕様41〜43節)。"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import ApprovalStatus


class RuleVersion(Entity):
    rule_version: str
    created_at: dt.datetime
    effective_from: dt.datetime | None = None
    effective_to: dt.datetime | None = None
    change_description: str
    change_reason: str
    approval_status: ApprovalStatus
    approved_by: str | None = None
    based_on_review: str | None = None
    backtest_result_ref: str | None = None
    previous_version: str | None = None
    rollback_target_version: str | None = None
    is_active: bool = False


class RuleProposal(Entity):
    """判断ロジック改善案(要求仕様41節)。"""

    proposal_id: str
    created_at: dt.datetime
    target: str  # 変更対象(例: total_yield.min_total_yield_pct)
    current_value: Any
    proposed_value: Any
    reason: str
    evaluation_count: int
    current_rule_performance: dict[str, Any]
    proposed_rule_backtest_performance: dict[str, Any]
    performance_diff: dict[str, Any]
    risk_impact: str
    overfitting_risk_assessment: str
    recommended_application_period: str | None = None
    rollback_condition: str
    status: ApprovalStatus = ApprovalStatus.DRAFT

    # 45節: 最低評価件数を満たさない場合、変更を提案せず"データ不足"とする
    MIN_EVALUATION_COUNT_FOR_PROPOSAL: ClassVar[int] = 30
    MIN_EVALUATION_COUNT_FOR_THRESHOLD_CHANGE: ClassVar[int] = 60
