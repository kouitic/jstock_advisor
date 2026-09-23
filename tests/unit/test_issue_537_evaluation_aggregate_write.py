"""Issue #537: 定点評価の保存が、設定 ON のとき Transaction(EvaluationResult + Aggregate)になる。

確認するもの:
- 既定(OFF)は従来どおり(Aggregate のストアを呼ばない)
- ON: 暦日 7 日(週次レビューの対象)の評価だけが Aggregate へ加算される。営業日の評価は加算しない
- 加算される値は、保存された EvaluationResult と一致する(種別・rule_version は Recommendation から)
- 再実行しても二重加算しない
- Aggregate の更新に失敗したら、EvaluationResult は保存せず、評価済みと数えず、翌日に再試行(Q-2)
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

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
from jstock_advisor.infrastructure.weekly_evaluation_aggregate_store import (
    LocalWeeklyEvaluationAggregateStore,
)
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.services.recommendation_evaluation_service import (
    RecommendationEvaluationService,
)

# tests/unit/test_issue_71_fc12_evaluation_unique_key.py と同じ銘柄・日付(mock の株価がある銘柄)
_STOCK_CODE = "2914"
_RECOMMENDED_AT = dt.datetime(2024, 1, 4, tzinfo=dt.UTC)
_NOW = dt.datetime(2024, 3, 1, tzinfo=dt.UTC)
_RULE_VERSION = "rv-537"


@pytest.fixture
def config() -> AppConfig:
    return load_config()


@pytest.fixture
def calendar(config: AppConfig) -> BusinessCalendar:
    return BusinessCalendar.from_config(config.holiday_calendar)


def _recommendation() -> Recommendation:
    return Recommendation(
        recommendation_id="rec-537",
        stock_code=_STOCK_CODE,
        stock_name="テスト銘柄",
        recommended_at=_RECOMMENDED_AT,
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            standard=PriceWithRationale(price=Decimal("2000"), rationale="x"),
        ),
        price_at_recommendation=Decimal("2200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version=_RULE_VERSION,
    )


class _Harness:
    def __init__(self, tmp_path: Path, config: AppConfig, calendar: BusinessCalendar) -> None:
        self.evaluations = EvaluationResultRepository(store_dir=tmp_path)
        self.recommendations = RecommendationRepository(store_dir=tmp_path)
        self.recommendations.save(_recommendation())
        self.store = LocalWeeklyEvaluationAggregateStore(self.evaluations.insert_if_absent)
        self._config = config
        self._calendar = calendar

    def service(
        self, *, aggregate_write: bool, store: Any = None
    ) -> RecommendationEvaluationService:
        return RecommendationEvaluationService(
            market_data_provider=MockMarketDataProvider(now=_NOW),  # type: ignore[arg-type]
            config=self._config,
            business_calendar=self._calendar,
            recommendation_repository=self.recommendations,
            evaluation_repository=self.evaluations,
            aggregate_store=store if store is not None else self.store,
            aggregate_write=aggregate_write,
        )

    def run(self, service: RecommendationEvaluationService) -> Any:
        return service.run_due_evaluations_single_pass(
            _NOW,
            calendar_horizon_days=self._config.review_improvement.evaluation_horizon_days,
        )

    def calendar_evaluations(self) -> list[Any]:
        return [e for e in self.evaluations.list_all() if e.horizon_calendar_days is not None]

    def aggregate_rows(self) -> list[Any]:
        weeks = {e.evaluation_date.isocalendar()[:2] for e in self.calendar_evaluations()}
        rows: list[Any] = []
        for year, week in sorted(weeks):
            rows.extend(self.store.query_week(f"{year}-W{week:02d}"))
        return rows


@pytest.fixture
def harness(tmp_path: Path, config: AppConfig, calendar: BusinessCalendar) -> _Harness:
    return _Harness(tmp_path, config, calendar)


def test_default_off_does_not_touch_the_aggregate_store(harness: _Harness) -> None:
    class _Forbidden:
        def commit_evaluation(self, *_: Any, **__: Any) -> bool:
            raise AssertionError("既定(OFF)では Aggregate のストアを呼ばない")

    outcome = harness.run(harness.service(aggregate_write=False, store=_Forbidden()))

    assert outcome.evaluated
    assert harness.calendar_evaluations()
    assert harness.aggregate_rows() == []
    assert outcome.summary.aggregate_commit_failed_count == 0


def test_on_aggregates_only_the_calendar_evaluations_that_the_weekly_review_reads(
    harness: _Harness,
) -> None:
    outcome = harness.run(harness.service(aggregate_write=True))

    saved = harness.calendar_evaluations()
    assert saved and len(saved) == outcome.summary.calendar_evaluated_count
    assert outcome.summary.business_evaluated_count > 0  # 営業日の評価もある(加算はしない)
    rows = harness.aggregate_rows()
    assert sum(r.sample_count for r in rows) == len(saved)
    assert {(r.recommendation_type, r.rule_version) for r in rows} == {
        (RecommendationType.BUY, _RULE_VERSION)  # Recommendation の種別・rule_version
    }
    # 加算の値は、保存された EvaluationResult と一致する
    assert sum(r.price_return_sum for r in rows) == sum(
        Decimal(repr(e.price_return_pct)) for e in saved
    )
    assert outcome.summary.aggregate_commit_failed_count == 0
    # 加算した週は marker(REVIEW_RECOMPUTE_PENDING)が付いている
    assert harness.store.list_pending_weeks()


def test_rerun_does_not_double_count(harness: _Harness) -> None:
    service = harness.service(aggregate_write=True)
    harness.run(service)
    counted = sum(r.sample_count for r in harness.aggregate_rows())

    harness.run(harness.service(aggregate_write=True))  # 別のサービス(=別の run)でもう一度

    assert sum(r.sample_count for r in harness.aggregate_rows()) == counted


def test_aggregate_failure_saves_nothing_and_the_evaluation_is_retried_next_run(
    harness: _Harness,
) -> None:
    class _Failing:
        def commit_evaluation(self, *_: Any, **__: Any) -> bool:
            raise RuntimeError("aggregate table unavailable")

    failed = harness.run(harness.service(aggregate_write=True, store=_Failing()))

    assert harness.calendar_evaluations() == []  # ★ EvaluationResult も保存されていない
    assert failed.summary.aggregate_commit_failed_count >= 1
    assert failed.summary.calendar_evaluated_count == 0  # 評価済みとして数えない
    assert any("週次評価集計" in reason for _, _, reason in failed.skipped_due_to_data_error)
    # 営業日の評価は Aggregate と無関係なので、従来どおり保存される
    assert failed.summary.business_evaluated_count > 0

    recovered = harness.run(harness.service(aggregate_write=True))  # 障害が解消した翌日の run

    assert (
        recovered.summary.calendar_evaluated_count == failed.summary.aggregate_commit_failed_count
    )
    assert sum(r.sample_count for r in harness.aggregate_rows()) == len(
        harness.calendar_evaluations()
    )


def test_horizon_that_the_weekly_review_does_not_read_is_saved_without_aggregation(
    harness: _Harness, config: AppConfig
) -> None:
    """週次レビューの `evaluation_horizon_days` と違うホライズンは、集計しない(従来どおり保存)。"""
    other = config.review_improvement.evaluation_horizon_days + 3
    service = harness.service(aggregate_write=True)

    outcome = service.run_due_evaluations_single_pass(_NOW, calendar_horizon_days=other)

    assert outcome.summary.calendar_evaluated_count > 0
    assert any(e.horizon_calendar_days == other for e in harness.evaluations.list_all())
    assert harness.store.list_pending_weeks() == []  # 加算されていない(marker も無い)
