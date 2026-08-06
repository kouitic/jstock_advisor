"""domain/entities/evaluation_audit.pyのsummary_category()のテスト。"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EvaluationStatus,
    NotificationStatus,
)
from jstock_advisor.domain.entities.evaluation_audit import (
    HoldingEvaluationAudit,
    summary_category,
)


def _audit(notification_status: NotificationStatus) -> HoldingEvaluationAudit:
    return HoldingEvaluationAudit(
        stock_code="2914",
        evaluated_at=dt.datetime.now(dt.UTC),
        evaluation_status=EvaluationStatus.COMPLETED,
        raw_sell_recommendation_type=None,
        raw_profit_recommendation_type=None,
        final_recommendation_type=None,
        notification_status=notification_status,
        notification_suppression_reason=None,
        sell_signal_status="NO_SIGNAL",
        profit_taking_status="NO_SIGNAL",
        fair_value_status="NOT_AVAILABLE",
        data_quality_status="OK",
        confidence=ConfidenceLevel.HIGH,
        error_code=None,
    )


def test_kill_switch_suppressed_maps_to_suppressed_summary_category() -> None:
    """NotificationStatus.KILL_SWITCH_SUPPRESSED(コードレビュー対応で追加)は、
    既存のフォールバック分岐によりそのまま「再通知抑止」区分へ吸収されることを
    明示的に確認する(ロジック変更不要であることの回帰確認)。"""
    audit = _audit(NotificationStatus.KILL_SWITCH_SUPPRESSED)
    assert summary_category(audit) == "suppressed"
