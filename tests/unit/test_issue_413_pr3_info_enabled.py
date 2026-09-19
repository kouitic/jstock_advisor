"""Issue #413 PR-3: recommendation_evaluation_service / weekly_improvement_review_service。

2 module は module 直下で `logger.setLevel(logging.INFO)` を宣言した。ここでは次を確認する。

    1 宣言が実際に効く: Lambda の root logger の既定(WARNING)のもとでも、INFO が有効になる。
    2 INFO が実際に出力される(各 module の代表的な INFO の経路を 1 件以上)。
    3 出力に、生の owner / holding_id を含めない(有効化の時点の PII 確認。#135 / #416)。

宣言があること自体は tests/unit/test_issue_413_logger_level_declared.py(PR #436 の guard)が見る。
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jstock_advisor.config.loader import load_config
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
from jstock_advisor.services import (
    recommendation_evaluation_service,
    weekly_improvement_review_service,
)
from jstock_advisor.services.recommendation_evaluation_service import (
    RecommendationEvaluationService,
)
from jstock_advisor.services.weekly_improvement_review_service import (
    WeeklyImprovementReviewService,
)

_MODULES = [
    recommendation_evaluation_service.__name__,
    weekly_improvement_review_service.__name__,
]

#: 実在しない架空値。出力に現れたら、生の owner / holding_id を出している。
_OWNER = "owner-a"
_HOLDING_ID = "owner-a:holding-1"


@pytest.mark.parametrize("module_name", _MODULES)
def test_info_is_enabled_even_when_the_root_logger_is_at_warning(
    module_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lambda の root 既定(WARNING)に左右されず、INFO が有効になる(宣言が効いている)。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)

    assert logging.getLogger().getEffectiveLevel() == logging.WARNING
    assert logging.getLogger(module_name).isEnabledFor(logging.INFO)
    assert not logging.getLogger(module_name).isEnabledFor(logging.DEBUG)


def test_recommendation_evaluation_scan_done_info_is_emitted_without_owner_or_holding_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    config = load_config()
    now = dt.datetime(2024, 3, 1, tzinfo=dt.UTC)
    recommendation_repo = RecommendationRepository(store_dir=tmp_path)
    service = RecommendationEvaluationService(
        market_data_provider=MockMarketDataProvider(now=now),
        config=config,
        business_calendar=BusinessCalendar.from_config(config.holiday_calendar),
        recommendation_repository=recommendation_repo,
        evaluation_repository=EvaluationResultRepository(store_dir=tmp_path),
    )
    recommendation_repo.save(
        Recommendation(
            recommendation_id="rec-1",
            stock_code="0000",
            stock_name="test",
            recommended_at=dt.datetime(2024, 1, 4, tzinfo=dt.UTC),
            recommendation_type=RecommendationType.BUY,
            buy_prices=BuyPriceLevels(
                standard=PriceWithRationale(price=Decimal("2000"), rationale="x"),
            ),
            price_at_recommendation=Decimal("2200"),
            confidence=ConfidenceLevel.HIGH,
            rule_version="v1-mvp",
        )
    )

    service.run_due_evaluations(now)

    messages = [
        r.getMessage()
        for r in caplog.records
        if r.name == recommendation_evaluation_service.__name__
    ]
    assert any(m.startswith("evaluation scan done:") for m in messages)
    for message in messages:
        assert _OWNER not in message
        assert _HOLDING_ID not in message


def _weekly_service(evaluations: MagicMock) -> WeeklyImprovementReviewService:
    return WeeklyImprovementReviewService(
        config=load_config(),
        evaluation_repository=evaluations,
        recommendation_repository=MagicMock(),
        weekly_review_metrics_repository=MagicMock(),
        improvement_candidate_repository=MagicMock(),
        rule_version_service=MagicMock(),
        audit_service=MagicMock(),
    )


def test_weekly_review_scan_and_recompute_info_is_emitted_without_owner_or_holding_id(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    evaluations = MagicMock()
    evaluations.iter_all.return_value = iter([])
    service = _weekly_service(evaluations)
    window = ("2026-W38", dt.date(2026, 9, 14), dt.date(2026, 9, 20))

    buckets = service._collect_evaluations_for_windows([window])
    recomputed, per_week = service._recompute_past_weeks_from_buckets(
        [], buckets, dt.datetime(2026, 9, 20, tzinfo=dt.UTC)
    )

    assert (recomputed, per_week) == (0, {})
    messages = [
        r.getMessage()
        for r in caplog.records
        if r.name == weekly_improvement_review_service.__name__
    ]
    assert any(m.startswith("weekly review single-pass scan done") for m in messages)
    assert any(m.startswith("weekly review past-week recompute skipped") for m in messages)
    for message in messages:
        assert _OWNER not in message
        assert _HOLDING_ID not in message
