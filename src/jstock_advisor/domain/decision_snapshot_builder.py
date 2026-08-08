"""判定精度向上機能Phase A: DecisionSnapshot構築(自己評価基盤)。

外部I/O(Provider呼び出し)を一切行わない純関数として実装する。DecisionSnapshotは
必ず既存のStockSnapshot・Recommendationのみから導出し、それ以外のデータへは
触れない(point-in-time保証、既存の「推測で補完しない」原則と同じ考え方)。
"""

from __future__ import annotations

import uuid

from jstock_advisor.domain.entities.decision_snapshot import (
    DECISION_SNAPSHOT_MODEL_VERSION,
    DecisionSnapshot,
)
from jstock_advisor.domain.entities.enums import DecisionType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.services.stock_snapshot_service import StockSnapshot


def build_decision_snapshot(
    snapshot: StockSnapshot,
    recommendation: Recommendation,
    decision_type: DecisionType,
) -> DecisionSnapshot:
    """StockSnapshotとRecommendationのみから、外部I/Oなしで構築する。"""
    evaluated_at = recommendation.recommended_at
    fair_value_range = snapshot.fair_value_range
    return DecisionSnapshot(
        decision_id=str(uuid.uuid4()),
        decision_type=decision_type,
        stock_code=snapshot.stock_code,
        evaluated_at=evaluated_at,
        evaluation_date_jst=evaluation_date_jst(evaluated_at),
        recommendation_id=recommendation.recommendation_id,
        existing_action=recommendation.recommendation_type,
        market_price=snapshot.current_price,
        fair_value_bear=fair_value_range.bear,
        fair_value_neutral=fair_value_range.neutral,
        fair_value_bull=fair_value_range.bull,
        fair_value_confidence=fair_value_range.overall_confidence,
        rule_version=recommendation.rule_version,
        model_version=DECISION_SNAPSHOT_MODEL_VERSION,
        data_sources=tuple(snapshot.data_sources),
        data_fetched_at=snapshot.data_fetched_at,
    )
