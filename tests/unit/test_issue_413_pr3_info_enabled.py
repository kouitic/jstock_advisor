"""Issue #413 PR-3: recommendation_evaluation_service / weekly_improvement_review_service。

2 module は module 直下で `logger.setLevel(logging.INFO)` を宣言した。ここでは次を確認する。

    1 宣言が実際に効く: Lambda の root logger の既定(WARNING)のもとでも、INFO が有効になる。
    2 INFO が実際に出力される(各 module の代表的な INFO の経路を 1 件以上)。
    3 出力に、生の owner / holding_id を含めない(有効化の時点の PII 確認。#135 / #416)。

**3 は、架空の owner / holding_id を、試験対象の module が実際に読むデータへ入れて確認する**
(PR #444 のレビュー指摘 F1)。データに値が無ければ「出力に現れない」ことは常に真で、何も保証しない。
そのため、各テストは「架空値が実際にデータへ入っていること」を先に確認し、対象 module が
INFO へ出したら赤になることを、注入した INFO で確認している(PR 本文に結果を記録)。

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
from jstock_advisor.infrastructure.local_repository.weekly_review_metrics_repository import (
    WeeklyReviewMetricsRepository,
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

#: 実在しない架空値。試験対象の module が読むデータへ入れ、出力に現れたら生の値を出している。
_OWNER = "owner-a"
_HOLDING_ID = "owner-a:holding-1"


def _assert_no_raw_owner_or_holding_id(
    caplog: pytest.LogCaptureFixture, module_name: str
) -> list[str]:
    """対象 module が出した全レベルの記録に、架空の owner / holding_id が無いことを確認する。"""
    messages = [r.getMessage() for r in caplog.records if r.name == module_name]
    for message in messages:
        assert _OWNER not in message, message
        assert _HOLDING_ID not in message, message
    return messages


def test_the_leak_check_fails_when_a_record_contains_the_owner_or_holding_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """検査そのものの確認: 架空値を含む記録があれば、検査は赤になる(常に緑の検査ではない)。"""
    module_name = recommendation_evaluation_service.__name__
    for leaked in (f"x owner={_OWNER}", f"x holding_id={_HOLDING_ID}"):
        caplog.clear()
        logging.getLogger(module_name).info("%s", leaked)

        with pytest.raises(AssertionError):
            _assert_no_raw_owner_or_holding_id(caplog, module_name)


@pytest.mark.parametrize("module_name", _MODULES)
def test_info_is_enabled_even_when_the_root_logger_is_at_warning(
    module_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lambda の root 既定(WARNING)に左右されず、INFO が有効になる(宣言が効いている)。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)

    assert logging.getLogger().getEffectiveLevel() == logging.WARNING
    assert logging.getLogger(module_name).isEnabledFor(logging.INFO)
    assert not logging.getLogger(module_name).isEnabledFor(logging.DEBUG)


def _recommendation_with_owner(recommendation_id: str = "rec-1") -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
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
        owner=_OWNER,
        holding_id=_HOLDING_ID,
    )


def test_recommendation_evaluation_scan_info_is_emitted_and_the_data_carries_the_owner(
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
    recommendation_repo.save(_recommendation_with_owner())
    stored = recommendation_repo.get("rec-1")
    assert stored is not None
    # 架空値が、試験対象が読むデータへ実際に入っている(入っていなければ以下の検査は何も保証しない)
    assert (stored.owner, stored.holding_id) == (_OWNER, _HOLDING_ID)

    service.run_due_evaluations(now)

    messages = _assert_no_raw_owner_or_holding_id(
        caplog, recommendation_evaluation_service.__name__
    )
    assert any(m.startswith("evaluation scan done:") for m in messages)


def _weekly_service(
    evaluations: EvaluationResultRepository,
    recommendations: RecommendationRepository,
    metrics: WeeklyReviewMetricsRepository,
) -> WeeklyImprovementReviewService:
    return WeeklyImprovementReviewService(
        config=load_config(),
        evaluation_repository=evaluations,
        recommendation_repository=recommendations,
        weekly_review_metrics_repository=metrics,
        improvement_candidate_repository=MagicMock(),
        rule_version_service=MagicMock(),
        audit_service=MagicMock(),
    )


def test_weekly_review_scan_and_recompute_info_is_emitted_and_the_data_carries_the_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    evaluations = EvaluationResultRepository(store_dir=tmp_path)
    recommendations = RecommendationRepository(store_dir=tmp_path)
    metrics = WeeklyReviewMetricsRepository(store_dir=tmp_path)
    service = _weekly_service(evaluations, recommendations, metrics)
    horizon = service._review_config.evaluation_horizon_days
    past_label = "2026-W37"  # 2026-09-07(月)〜2026-09-13(日)
    evaluated_at = dt.datetime(2026, 9, 9, 3, 0, tzinfo=dt.UTC)
    recommendations.save(_recommendation_with_owner())
    evaluations.save(
        EvaluationResult(
            evaluation_id="eval-1",
            recommendation_id="rec-1",
            horizon_calendar_days=horizon,
            evaluated_at=evaluated_at,
            evaluation_date=evaluated_at.date(),
            price_at_evaluation=Decimal("1010"),
            price_return_pct=1.0,
            excess_return_pct=1.0,
            evaluation_label=EvaluationLabel.SUCCESS,
            label_evidence="x",
        )
    )
    stored = recommendations.get("rec-1")
    assert stored is not None
    # 架空値が、試験対象が読むデータへ実際に入っている
    assert (stored.owner, stored.holding_id) == (_OWNER, _HOLDING_ID)
    window = (past_label, dt.date(2026, 9, 7), dt.date(2026, 9, 13))

    aggregates = service._aggregate_windows([window])
    recomputed, per_week = service._recompute_past_weeks_from_aggregates(
        [past_label], aggregates, dt.datetime(2026, 9, 20, tzinfo=dt.UTC)
    )

    # 走査・join・集計が実際にデータを通った(空のデータではない)
    assert aggregates[past_label].matched == 1
    assert (recomputed, per_week) == (1, {past_label: 1})
    messages = _assert_no_raw_owner_or_holding_id(
        caplog, weekly_improvement_review_service.__name__
    )
    assert any(m.startswith("weekly review single-pass scan done") for m in messages)
    assert any(m.startswith("weekly review past-week recompute weeks_back=") for m in messages)
