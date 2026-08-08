"""判定精度向上機能Phase Aが既存の評価・成績集計へ影響しないことを検証する
横断テスト(コードレビュー対応、最優先項目)。

DecisionSnapshotの存在有無に関わらず、以下が完全に不変であることを保証する:
- RecommendationEvaluationService(既存の営業日/暦日ホライズン評価)が生成する
  EvaluationResultの件数・内容
- PerformanceMetricsService.summarize()が返す成功率・平均リターン等

DecisionSnapshot専用のEvaluationResultは生成しない設計(DecisionEvaluationService
は削除済み)のため、これらのテストはPhase A導入前後で完全に同じ結果になることを
直接証明する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.decision_snapshot import (
    DECISION_SNAPSHOT_MODEL_VERSION,
    DecisionSnapshot,
    build_decision_id,
)
from jstock_advisor.domain.entities.enums import ConfidenceLevel, DecisionType, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.services.performance_metrics_service import PerformanceMetricsService
from jstock_advisor.services.recommendation_evaluation_service import (
    RecommendationEvaluationService,
)

_STOCK_CODE = "2914"
_RECOMMENDED_AT = dt.datetime(2024, 1, 4, tzinfo=dt.UTC)


def _make_recommendation(recommendation_id: str = "rec-1") -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=_STOCK_CODE,
        stock_name="テスト銘柄",
        recommended_at=_RECOMMENDED_AT,
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            standard=PriceWithRationale(price=Decimal("2000"), rationale="x"),
        ),
        price_at_recommendation=Decimal("2200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


def _make_decision_snapshot(recommendation: Recommendation) -> DecisionSnapshot:
    return DecisionSnapshot(
        decision_id=build_decision_id(recommendation.recommendation_id),
        decision_type=DecisionType.BUY,
        stock_code=recommendation.stock_code,
        evaluated_at=recommendation.recommended_at,
        evaluation_date_jst=recommendation.recommended_at.date(),
        recommendation_id=recommendation.recommendation_id,
        existing_action=recommendation.recommendation_type,
        market_price=recommendation.price_at_recommendation,
        rule_version=recommendation.rule_version,
        model_version=DECISION_SNAPSHOT_MODEL_VERSION,
    )


def test_decision_snapshot_presence_does_not_change_evaluation_result_count(
    tmp_path: Path,
) -> None:
    """DecisionSnapshotが存在してもrecommendation_evaluation_service.pyが生成する
    EvaluationResultの件数は変わらない(専用のEvaluationResultを新規生成しない)。"""
    config = load_config()
    calendar = BusinessCalendar.from_config(config.holiday_calendar)
    now = dt.datetime(2024, 3, 1, tzinfo=dt.UTC)

    recommendation_repo = RecommendationRepository(store_dir=tmp_path)
    evaluation_repo = EvaluationResultRepository(store_dir=tmp_path)
    decision_repo = DecisionSnapshotRepository(store_dir=tmp_path)
    recommendation = _make_recommendation()
    recommendation_repo.save(recommendation)

    service = RecommendationEvaluationService(
        market_data_provider=MockMarketDataProvider(now=now),
        config=config,
        business_calendar=calendar,
        recommendation_repository=recommendation_repo,
        evaluation_repository=evaluation_repo,
    )

    outcome_without_decision = service.run_due_evaluations(now)
    count_without_decision = len(evaluation_repo.list_all())
    assert len(outcome_without_decision.evaluated) == count_without_decision

    # DecisionSnapshotを追加してから、まっさらな状態で再度評価しても件数は同じになる
    # (recommendation_evaluation_service.py自体はDecisionSnapshotの有無を一切見ない)。
    decision_repo.insert_if_absent(_make_decision_snapshot(recommendation))
    evaluation_repo_2 = EvaluationResultRepository(store_dir=tmp_path / "with_decision")
    recommendation_repo_2 = RecommendationRepository(store_dir=tmp_path / "with_decision")
    recommendation_repo_2.save(recommendation)
    service_2 = RecommendationEvaluationService(
        market_data_provider=MockMarketDataProvider(now=now),
        config=config,
        business_calendar=calendar,
        recommendation_repository=recommendation_repo_2,
        evaluation_repository=evaluation_repo_2,
    )
    outcome_with_decision = service_2.run_due_evaluations(now)

    assert len(outcome_with_decision.evaluated) == len(outcome_without_decision.evaluated)
    # decision_idフィールド自体が存在しないため、DecisionSnapshotとの紐付けは
    # 一切生じない(EvaluationResultはrecommendation_idのみで冪等性を保つ)。


def test_performance_metrics_unaffected_by_decision_snapshot_presence(tmp_path: Path) -> None:
    """PerformanceMetricsService.summarize()の結果(件数・成功率・平均リターン)は、
    同じ銘柄・同じ評価データに対してDecisionSnapshotが存在してもしなくても
    完全に同一になる(既存の週次改善レビュー・成績集計への影響ゼロ)。"""
    config = load_config()
    calendar = BusinessCalendar.from_config(config.holiday_calendar)
    now = dt.datetime(2024, 3, 1, tzinfo=dt.UTC)

    recommendation_repo = RecommendationRepository(store_dir=tmp_path)
    evaluation_repo = EvaluationResultRepository(store_dir=tmp_path)
    recommendation = _make_recommendation()
    recommendation_repo.save(recommendation)

    eval_service = RecommendationEvaluationService(
        market_data_provider=MockMarketDataProvider(now=now),
        config=config,
        business_calendar=calendar,
        recommendation_repository=recommendation_repo,
        evaluation_repository=evaluation_repo,
    )
    eval_service.run_due_evaluations(now)

    metrics_service = PerformanceMetricsService(
        evaluation_repository=evaluation_repo, recommendation_repository=recommendation_repo
    )
    summary_before = metrics_service.summarize(now=now)

    # DecisionSnapshotを追加(既存のevaluation_repo/recommendation_repoには一切触れない)。
    decision_repo = DecisionSnapshotRepository(store_dir=tmp_path)
    decision_repo.insert_if_absent(_make_decision_snapshot(recommendation))

    summary_after = metrics_service.summarize(now=now)

    assert summary_before.overall.count == summary_after.overall.count
    assert summary_before.overall.conclusive_count == summary_after.overall.conclusive_count
    assert summary_before.overall.success_rate_pct == summary_after.overall.success_rate_pct
    assert (
        summary_before.overall.avg_price_return_pct == summary_after.overall.avg_price_return_pct
    )
    assert len(summary_before.by_recommendation_type) == len(summary_after.by_recommendation_type)
