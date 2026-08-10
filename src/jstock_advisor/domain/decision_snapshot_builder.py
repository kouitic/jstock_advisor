"""判定精度向上機能Phase A: DecisionSnapshot構築(自己評価基盤)。

外部I/O(Provider呼び出し)を一切行わない純関数として実装する。DecisionSnapshotは
必ずRecommendation(判定パイプラインが実際に確定した最終判断値)のみから導出し、
StockSnapshot(判定処理の中間生成物)には一切触れない(コードレビュー対応:
BUYパイプライン等ではStockSnapshot取得後に補正・ゲート適用を行いfinal
Recommendationを作るため、StockSnapshotの値と最終Recommendationの値は将来
一致しなくなる可能性がある。「後から現在ロジックで過去判断を復元する」のではなく
「当時実際に確定した判断値」を保存するというPhase Aの目的に忠実であるため)。

コードレビュー対応(RecommendationType/DecisionType対応関係の明示): 生産コードの
横断調査(cli/analyze.py, buy_candidates_handler.py, holdings_watchlist_handler.py
の全9箇所のsave_decision_snapshot_safely()呼び出し)の結果、以下の対応が常に
1対1で成立している(同一recommendation_idが複数のDecisionTypeへ保存される経路は
存在しない。各Recommendationはuuid4で都度新規発行されるrecommendation_idを持ち、
1つのRecommendationインスタンスに対しsave_decision_snapshot_safely()が呼ばれる
のは常に1回のみ)。

    BUY / WATCH_BUY 系Recommendation(買い候補パイプライン)
        -> DecisionType.BUY
    legacy SELL系Recommendation(投資前提悪化売却パイプライン)
        -> DecisionType.SELL
    HoldingDecision由来Recommendation(保有判断スコアパイプライン)
        -> DecisionType.HOLDING_DECISION
    ProfitTaking由来Recommendation(利益確定パイプライン)
        -> DecisionType.PROFIT_TAKING

このため「1 Recommendation = 1 DecisionSnapshot」をモデルとして採用し、
decision_idはrecommendation_idのみから決定的に生成する
(domain/entities/decision_snapshot.pyのbuild_decision_id()参照)。
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
        decision_id=build_decision_id(recommendation.recommendation_id),
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
        historical_valuation_score=recommendation.historical_valuation_score,
        historical_valuation_confidence=recommendation.historical_valuation_confidence,
        historical_valuation_coverage=recommendation.historical_valuation_coverage,
        historical_valuation_reason_codes=recommendation.historical_valuation_reason_codes,
        historical_valuation_metrics=dict(recommendation.historical_valuation_metrics),
        timing_score=recommendation.timing_score,
        timing_confidence=recommendation.timing_confidence,
        timing_coverage=recommendation.timing_coverage,
        timing_reason_codes=recommendation.timing_reason_codes,
        timing_metrics=dict(recommendation.timing_metrics),
        earnings_surprise_score=recommendation.earnings_surprise_score,
        earnings_surprise_confidence=recommendation.earnings_surprise_confidence,
        earnings_surprise_coverage=recommendation.earnings_surprise_coverage,
        earnings_surprise_reason_codes=recommendation.earnings_surprise_reason_codes,
        earnings_surprise_metrics=dict(recommendation.earnings_surprise_metrics),
        earnings_trend_score=recommendation.earnings_trend_score,
        earnings_trend_confidence=recommendation.earnings_trend_confidence,
        earnings_trend_coverage=recommendation.earnings_trend_coverage,
        earnings_trend_reason_codes=recommendation.earnings_trend_reason_codes,
        earnings_trend_metrics=dict(recommendation.earnings_trend_metrics),
        entry_price_range_state=recommendation.entry_price_range_state,
        entry_price_range_confidence=recommendation.entry_price_range_confidence,
        entry_price_range_coverage=recommendation.entry_price_range_coverage,
        entry_price_range_reason_codes=recommendation.entry_price_range_reason_codes,
        entry_price_range_metrics=dict(recommendation.entry_price_range_metrics),
        entry_price_range_starter_price=recommendation.entry_price_range_starter_price,
        entry_price_range_preferred_price=recommendation.entry_price_range_preferred_price,
        entry_price_range_strong_price=recommendation.entry_price_range_strong_price,
        entry_price_range_max_price=recommendation.entry_price_range_max_price,
        entry_price_range_stop_review_price=recommendation.entry_price_range_stop_review_price,
        exit_price_range_state=recommendation.exit_price_range_state,
        exit_price_range_confidence=recommendation.exit_price_range_confidence,
        exit_price_range_coverage=recommendation.exit_price_range_coverage,
        exit_price_range_reason_codes=recommendation.exit_price_range_reason_codes,
        exit_price_range_metrics=dict(recommendation.exit_price_range_metrics),
        exit_price_range_partial_low_price=recommendation.exit_price_range_partial_low_price,
        exit_price_range_partial_high_price=recommendation.exit_price_range_partial_high_price,
        exit_price_range_strong_price=recommendation.exit_price_range_strong_price,
        exit_price_range_downside_review_price=recommendation.exit_price_range_downside_review_price,
        exit_price_range_exit_review_price=recommendation.exit_price_range_exit_review_price,
    )
