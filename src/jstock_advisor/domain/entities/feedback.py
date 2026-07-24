"""ユーザーによる定性評価(要求仕様47節)。"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.base import Entity


class UserFeedback(Entity):
    feedback_id: str
    recommendation_id: str | None = None
    transaction_id: str | None = None
    satisfaction_score: int | None = None  # 1-5等、CLI/入力側でレンジ検証
    risk_explanation_adequate: bool | None = None
    notification_timing_appropriate: bool | None = None
    recommended_price_practical: bool | None = None
    reason_convincing: bool | None = None
    helpful_for_decision: bool | None = None
    comment: str | None = None
    created_at: dt.datetime
