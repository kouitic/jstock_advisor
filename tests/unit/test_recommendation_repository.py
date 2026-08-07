"""RecommendationRepository.get_latest_by_typeのテスト(振り返り機能改修)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)


def _recommendation(
    rec_id: str,
    rec_type: RecommendationType,
    recommended_at: dt.datetime,
    rule_version: str,
) -> Recommendation:
    return Recommendation(
        recommendation_id=rec_id,
        stock_code="2914",
        stock_name="test",
        recommended_at=recommended_at,
        recommendation_type=rec_type,
        price_at_recommendation=Decimal("1000"),
        confidence=ConfidenceLevel.HIGH,
        rule_version=rule_version,
    )


def test_get_latest_by_type_returns_most_recent(tmp_path: Path) -> None:
    repo = RecommendationRepository(store_dir=tmp_path)
    repo.save(
        _recommendation(
            "r1", RecommendationType.BUY, dt.datetime(2026, 8, 1, tzinfo=dt.UTC), "v10"
        )
    )
    repo.save(
        _recommendation(
            "r2", RecommendationType.BUY, dt.datetime(2026, 8, 5, tzinfo=dt.UTC), "v11"
        )
    )
    repo.save(
        _recommendation(
            "r3", RecommendationType.SELL, dt.datetime(2026, 8, 6, tzinfo=dt.UTC), "v20"
        )
    )

    latest_buy = repo.get_latest_by_type(RecommendationType.BUY)
    assert latest_buy is not None
    assert latest_buy.recommendation_id == "r2"
    assert latest_buy.rule_version == "v11"


def test_get_latest_by_type_returns_none_when_no_match(tmp_path: Path) -> None:
    repo = RecommendationRepository(store_dir=tmp_path)
    repo.save(
        _recommendation(
            "r1", RecommendationType.BUY, dt.datetime(2026, 8, 1, tzinfo=dt.UTC), "v10"
        )
    )

    assert repo.get_latest_by_type(RecommendationType.SELL) is None
