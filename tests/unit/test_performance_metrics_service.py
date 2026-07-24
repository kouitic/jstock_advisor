import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EvaluationLabel,
    RecommendationType,
)
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.performance_metrics_service import PerformanceMetricsService

_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)


def _recommendation(
    rec_id: str,
    rec_type: RecommendationType,
    confidence: ConfidenceLevel,
    rule_version: str,
) -> Recommendation:
    return Recommendation(
        recommendation_id=rec_id,
        stock_code="2914",
        stock_name="test",
        recommended_at=_NOW,
        recommendation_type=rec_type,
        price_at_recommendation=Decimal("1000"),
        confidence=confidence,
        rule_version=rule_version,
    )


def _evaluation(
    eval_id: str,
    rec_id: str,
    label: EvaluationLabel,
    price_return_pct: float,
    excess_return_pct: float | None = None,
    horizon: int = 20,
) -> EvaluationResult:
    return EvaluationResult(
        evaluation_id=eval_id,
        recommendation_id=rec_id,
        horizon_business_days=horizon,
        evaluated_at=_NOW,
        evaluation_date=_NOW.date(),
        price_at_evaluation=Decimal("1100"),
        price_return_pct=price_return_pct,
        excess_return_pct=excess_return_pct,
        evaluation_label=label,
        label_evidence="test",
    )


@pytest.fixture
def service(tmp_path: Path) -> PerformanceMetricsService:
    rec_repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    rec_repo.save(_recommendation("r1", RecommendationType.BUY, ConfidenceLevel.HIGH, "v1"))
    rec_repo.save(_recommendation("r2", RecommendationType.BUY, ConfidenceLevel.LOW, "v1"))
    rec_repo.save(_recommendation("r3", RecommendationType.SELL, ConfidenceLevel.HIGH, "v2"))
    eval_repo.save(_evaluation("e1", "r1", EvaluationLabel.SUCCESS, 10.0, 3.0))
    eval_repo.save(_evaluation("e2", "r2", EvaluationLabel.PRICE_TOO_HIGH, -5.0))
    eval_repo.save(_evaluation("e3", "r3", EvaluationLabel.SUCCESS, -8.0))
    return PerformanceMetricsService(
        evaluation_repository=eval_repo, recommendation_repository=rec_repo
    )


def test_overall_bucket_aggregates_all(service: PerformanceMetricsService) -> None:
    summary = service.summarize()
    assert summary.overall.count == 3
    assert summary.overall.success_rate_pct == pytest.approx(200 / 3)
    assert summary.overall.avg_price_return_pct == pytest.approx((10.0 - 5.0 - 8.0) / 3)


def test_by_recommendation_type_groups_correctly(service: PerformanceMetricsService) -> None:
    summary = service.summarize()
    by_type = {b.key: b for b in summary.by_recommendation_type}
    assert by_type["BUY"].count == 2
    assert by_type["SELL"].count == 1


def test_by_confidence_groups_correctly(service: PerformanceMetricsService) -> None:
    summary = service.summarize()
    by_conf = {b.key: b for b in summary.by_confidence}
    assert by_conf["HIGH"].count == 2
    assert by_conf["LOW"].count == 1


def test_by_rule_version_groups_correctly(service: PerformanceMetricsService) -> None:
    summary = service.summarize()
    by_version = {b.key: b for b in summary.by_rule_version}
    assert by_version["v1"].count == 2
    assert by_version["v2"].count == 1


def test_horizon_filter(service: PerformanceMetricsService) -> None:
    summary = service.summarize(horizon_business_days=20)
    assert summary.overall.count == 3

    summary_none = service.summarize(horizon_business_days=999)
    assert summary_none.overall.count == 0
    assert summary_none.overall.success_rate_pct is None


def test_data_issue_excluded_from_success_rate_denominator(tmp_path: Path) -> None:
    rec_repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    rec_repo.save(_recommendation("r1", RecommendationType.BUY, ConfidenceLevel.HIGH, "v1"))
    eval_repo.save(_evaluation("e1", "r1", EvaluationLabel.DATA_ISSUE, 0.0))
    service = PerformanceMetricsService(
        evaluation_repository=eval_repo, recommendation_repository=rec_repo
    )

    summary = service.summarize()
    assert summary.overall.count == 1
    assert summary.overall.success_rate_pct is None
