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
from jstock_advisor.services.backtest_service import BacktestService

_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)
_TARGET = "screening.total_yield.min_total_yield_pct"


def _recommendation(
    rec_id: str, total_yield_pct: float, rec_type: RecommendationType = RecommendationType.BUY
) -> Recommendation:
    return Recommendation(
        recommendation_id=rec_id,
        stock_code="2914",
        stock_name="test",
        recommended_at=_NOW,
        recommendation_type=rec_type,
        price_at_recommendation=Decimal("1000"),
        total_yield_pct_at_recommendation=total_yield_pct,
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1",
    )


def _evaluation(
    eval_id: str, rec_id: str, label: EvaluationLabel, price_return_pct: float
) -> EvaluationResult:
    return EvaluationResult(
        evaluation_id=eval_id,
        recommendation_id=rec_id,
        horizon_business_days=20,
        evaluated_at=_NOW,
        evaluation_date=_NOW.date(),
        price_at_evaluation=Decimal("1100"),
        price_return_pct=price_return_pct,
        evaluation_label=label,
        label_evidence="test",
    )


@pytest.fixture
def service(tmp_path: Path) -> BacktestService:
    rec_repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    rec_repo.save(_recommendation("low", 3.6))
    rec_repo.save(_recommendation("mid", 4.0))
    rec_repo.save(_recommendation("high", 5.0))
    eval_repo.save(_evaluation("e-low", "low", EvaluationLabel.PRICE_TOO_HIGH, -5.0))
    eval_repo.save(_evaluation("e-mid", "mid", EvaluationLabel.SUCCESS, 8.0))
    eval_repo.save(_evaluation("e-high", "high", EvaluationLabel.SUCCESS, 12.0))
    return BacktestService(recommendation_repository=rec_repo, evaluation_repository=eval_repo)


def test_tightening_threshold_excludes_low_yield_recommendations(service: BacktestService) -> None:
    result = service.run(_TARGET, 3.5, 4.0)
    assert result.supported is True
    assert result.evaluation_count_current == 3
    assert result.evaluation_count_proposed == 2
    assert result.excluded_recommendation_ids == ["low"]
    assert result.current_performance is not None
    assert result.proposed_performance is not None


def test_loosening_threshold_is_unsupported(service: BacktestService) -> None:
    result = service.run(_TARGET, 3.5, 3.0)
    assert result.supported is False
    assert result.reason_unsupported is not None
    assert "生存バイアス" in result.reason_unsupported


def test_unregistered_target_is_unsupported(service: BacktestService) -> None:
    result = service.run("unknown.target", 1.0, 2.0)
    assert result.supported is False


def test_no_matching_data_reports_unsupported(tmp_path: Path) -> None:
    empty_service = BacktestService(
        recommendation_repository=RecommendationRepository(store_dir=tmp_path),
        evaluation_repository=EvaluationResultRepository(store_dir=tmp_path),
    )
    result = empty_service.run(_TARGET, 3.5, 4.0)
    assert result.supported is False


def test_non_applicable_recommendation_type_is_excluded_from_pairs(tmp_path: Path) -> None:
    rec_repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    rec_repo.save(_recommendation("sell-1", 5.0, rec_type=RecommendationType.SELL))
    eval_repo.save(_evaluation("e-sell-1", "sell-1", EvaluationLabel.SUCCESS, -5.0))
    service = BacktestService(recommendation_repository=rec_repo, evaluation_repository=eval_repo)

    result = service.run(_TARGET, 3.5, 4.0)
    assert result.supported is False
