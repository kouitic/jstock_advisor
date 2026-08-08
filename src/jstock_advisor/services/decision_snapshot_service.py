"""判定精度向上機能Phase A: DecisionSnapshot保存の安全なラッパー。

domain層(decision_snapshot_builder.py)はinfrastructure層に依存しないため、
Repositoryを扱うこの薄いラッパーはservices層に置く。
"""

from __future__ import annotations

import logging

from jstock_advisor.domain.decision_snapshot_builder import build_decision_snapshot
from jstock_advisor.domain.entities.enums import DecisionType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)
from jstock_advisor.services.stock_snapshot_service import StockSnapshot


def save_decision_snapshot_safely(
    repo: DecisionSnapshotRepository,
    snapshot: StockSnapshot,
    recommendation: Recommendation,
    decision_type: DecisionType,
    logger: logging.Logger,
) -> None:
    """DecisionSnapshotの構築・保存失敗が既存のRecommendation保存・通知フローを
    絶対にブロックしないためのラッパー。例外はWARNINGログのみに留め、呼び出し元へ
    伝播させない。"""
    try:
        repo.save(build_decision_snapshot(snapshot, recommendation, decision_type))
    except Exception:
        logger.warning(
            "decision_snapshot_save_failed stock_code=%s recommendation_id=%s",
            recommendation.stock_code,
            recommendation.recommendation_id,
            exc_info=True,
        )
