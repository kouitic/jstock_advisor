import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.services.recommendation_evaluation_service import (
    RecommendationEvaluationService,
)

_STOCK_CODE = "2914"
_RECOMMENDED_AT = dt.datetime(2024, 1, 4, tzinfo=dt.UTC)


@pytest.fixture
def config() -> AppConfig:
    return load_config()


@pytest.fixture
def calendar(config: AppConfig) -> BusinessCalendar:
    return BusinessCalendar.from_config(config.holiday_calendar)


def _make_recommendation(
    recommendation_id: str = "rec-1",
    recommendation_type: RecommendationType = RecommendationType.BUY,
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=_STOCK_CODE,
        stock_name="テスト銘柄",
        recommended_at=_RECOMMENDED_AT,
        recommendation_type=recommendation_type,
        buy_prices=BuyPriceLevels(
            standard=PriceWithRationale(price=Decimal("2000"), rationale="x"),
        ),
        price_at_recommendation=Decimal("2200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


def _build_service(
    tmp_path: Path,
    config: AppConfig,
    calendar: BusinessCalendar,
    now: dt.datetime,
) -> tuple[RecommendationEvaluationService, RecommendationRepository, EvaluationResultRepository]:
    recommendation_repo = RecommendationRepository(store_dir=tmp_path)
    evaluation_repo = EvaluationResultRepository(store_dir=tmp_path)
    service = RecommendationEvaluationService(
        market_data_provider=MockMarketDataProvider(now=now),
        config=config,
        business_calendar=calendar,
        recommendation_repository=recommendation_repo,
        evaluation_repository=evaluation_repo,
    )
    return service, recommendation_repo, evaluation_repo


def test_run_due_evaluations_computes_price_return(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    now = dt.datetime(2024, 3, 1, tzinfo=dt.UTC)
    service, recommendation_repo, evaluation_repo = _build_service(tmp_path, config, calendar, now)
    recommendation_repo.save(_make_recommendation())

    outcome = service.run_due_evaluations(now)

    assert outcome.evaluated
    for result in outcome.evaluated:
        assert result.recommendation_id == "rec-1"
        assert result.price_return_pct is not None
        assert result.evaluation_date <= now.date()
    saved = evaluation_repo.list_by_recommendation("rec-1")
    assert len(saved) == len(outcome.evaluated)


def test_run_due_evaluations_skips_not_yet_due(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    service, recommendation_repo, _ = _build_service(tmp_path, config, calendar, _RECOMMENDED_AT)
    recommendation_repo.save(_make_recommendation())

    outcome = service.run_due_evaluations(_RECOMMENDED_AT)
    assert outcome.evaluated == []


def test_run_due_evaluations_is_idempotent(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    now = dt.datetime(2024, 3, 1, tzinfo=dt.UTC)
    service, recommendation_repo, _ = _build_service(tmp_path, config, calendar, now)
    recommendation_repo.save(_make_recommendation())

    first = service.run_due_evaluations(now)
    second = service.run_due_evaluations(now)

    assert first.evaluated
    assert second.evaluated == []


def test_buy_price_reach_flags_are_computed(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    now = dt.datetime(2024, 6, 1, tzinfo=dt.UTC)
    service, recommendation_repo, _ = _build_service(tmp_path, config, calendar, now)
    recommendation_repo.save(_make_recommendation())

    outcome = service.run_due_evaluations(now)
    assert outcome.evaluated
    for result in outcome.evaluated:
        # 買値到達フラグはbuy_pricesがある推奨では必ずbool/Noneのどちらかで確定している
        assert result.reached_standard_buy_price in (True, False)


def test_data_error_when_recommendation_after_available_history(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    far_future_recommendation = dt.datetime(2030, 1, 4, tzinfo=dt.UTC)
    now = dt.datetime(2030, 6, 1, tzinfo=dt.UTC)
    service, recommendation_repo, _ = _build_service(tmp_path, config, calendar, now)
    recommendation_repo.save(
        _make_recommendation("rec-far-future").model_copy(
            update={"recommended_at": far_future_recommendation}
        )
    )

    outcome = service.run_due_evaluations(now)
    assert outcome.evaluated == []
    assert outcome.skipped_due_to_data_error
