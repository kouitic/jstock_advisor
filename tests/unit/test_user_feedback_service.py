import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    RecommendationType,
    TransactionType,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.transaction import Transaction
from jstock_advisor.infrastructure.local_repository.feedback_repository import (
    UserFeedbackRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.local_repository.transaction_repository import (
    TransactionRepository,
)
from jstock_advisor.services.user_feedback_service import UserFeedbackService

_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)


@pytest.fixture
def service(tmp_path: Path) -> UserFeedbackService:
    rec_repo = RecommendationRepository(store_dir=tmp_path)
    rec_repo.save(
        Recommendation(
            recommendation_id="rec-1",
            stock_code="2914",
            stock_name="test",
            recommended_at=_NOW,
            recommendation_type=RecommendationType.BUY,
            price_at_recommendation=Decimal("1000"),
            confidence=ConfidenceLevel.HIGH,
            rule_version="v1",
        )
    )
    tx_repo = TransactionRepository(store_dir=tmp_path)
    tx_repo.save(
        Transaction(
            transaction_id="tx-1",
            stock_code="2914",
            transaction_type=TransactionType.BUY,
            execution_date=_NOW.date(),
            shares=100,
            execution_price=Decimal("1000"),
            followed_recommendation=True,
            created_at=_NOW,
        )
    )
    return UserFeedbackService(
        feedback_repository=UserFeedbackRepository(store_dir=tmp_path),
        recommendation_repository=rec_repo,
        transaction_repository=tx_repo,
    )


def test_submit_with_valid_recommendation_and_transaction(service: UserFeedbackService) -> None:
    feedback = service.submit(
        recommendation_id="rec-1", transaction_id="tx-1", satisfaction_score=5
    )
    assert feedback.satisfaction_score == 5
    assert feedback.recommendation_id == "rec-1"


def test_submit_rejects_out_of_range_score(service: UserFeedbackService) -> None:
    with pytest.raises(ValueError):
        service.submit(satisfaction_score=6)


def test_submit_rejects_unknown_recommendation(service: UserFeedbackService) -> None:
    with pytest.raises(ValueError, match="見つかりません"):
        service.submit(recommendation_id="does-not-exist")


def test_submit_rejects_unknown_transaction(service: UserFeedbackService) -> None:
    with pytest.raises(ValueError, match="見つかりません"):
        service.submit(transaction_id="does-not-exist")


def test_submit_without_links_succeeds(service: UserFeedbackService) -> None:
    feedback = service.submit(comment="general feedback")
    assert feedback.recommendation_id is None
    assert feedback.transaction_id is None


def test_list_feedback_filters_by_recommendation(service: UserFeedbackService) -> None:
    service.submit(recommendation_id="rec-1", comment="a")
    service.submit(comment="b")
    assert len(service.list_feedback("rec-1")) == 1
    assert len(service.list_feedback()) == 2
