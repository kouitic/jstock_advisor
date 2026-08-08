"""判定精度向上機能Phase A: DecisionSnapshot構築(自己評価基盤)。

外部I/O(Provider呼び出し)を一切行わない純関数として実装する。DecisionSnapshotは
必ずRecommendation(判定パイプラインが実際に確定した最終判断値)のみから導出し、
StockSnapshot(判定処理の中間生成物)には一切触れない(コードレビュー対応:
BUYパイプライン等ではStockSnapshot取得後に補正・ゲート適用を行いfinal
Recommendationを作るため、StockSnapshotの値と最終Recommendationの値は将来
一致しなくなる可能性がある。「後から現在ロジックで過去判断を復元する」のではなく
「当時実際に確定した判断値」を保存するというPhase Aの目的に忠実であるため)。
"""

from __future__ import annotations

from jstock_advisor.domain.entities.decision_snapshot import (
    DECISION_SNAPSHOT_MODEL_VERSION,
    DecisionSnapshot,
    build_decision_id,
)
from jstock_advisor.domain.entities.enums import DecisionType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.jst import evaluation_date_jst


def build_decision_snapshot(
    recommendation: Recommendation,
    decision_type: DecisionType,
) -> DecisionSnapshot:
    """Recommendationのみから、外部I/Oなしで構築する。

    market_price/fair_value_*はRecommendationに保存された最終判断値を
    そのままコピーする。Recommendation側に値が無い場合(旧方式等)は
    Noneのまま保存し、StockSnapshotや現在の計算ロジックから補完しない。
    """
    evaluated_at = recommendation.recommended_at
    return DecisionSnapshot(
        decision_id=build_decision_id(decision_type, recommendation.recommendation_id),
        decision_type=decision_type,
        stock_code=recommendation.stock_code,
        evaluated_at=evaluated_at,
        evaluation_date_jst=evaluation_date_jst(evaluated_at),
        recommendation_id=recommendation.recommendation_id,
        existing_action=recommendation.recommendation_type,
        market_price=recommendation.price_at_recommendation,
        fair_value_bear=recommendation.fair_value_bear,
        fair_value_neutral=recommendation.fair_value_neutral,
        fair_value_bull=recommendation.fair_value_bull,
        fair_value_confidence=recommendation.fair_value_overall_confidence,
        rule_version=recommendation.rule_version,
        model_version=DECISION_SNAPSHOT_MODEL_VERSION,
        config_values_used=dict(recommendation.config_values_used),
        data_sources=tuple(recommendation.data_sources),
    )
