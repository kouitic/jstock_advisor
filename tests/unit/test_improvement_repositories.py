"""WeeklyReviewMetricsRepository/ImprovementCandidateRepositoryのテスト
(振り返り機能改修)。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from jstock_advisor.domain.entities.enums import (
    ImprovementAction,
    ImprovementPriority,
    RecommendationType,
)
from jstock_advisor.domain.entities.improvement import (
    PROBLEM_CATEGORY_PERFORMANCE_DEGRADED,
    ImprovementCandidate,
    WeeklyReviewMetrics,
)
from jstock_advisor.infrastructure.local_repository.improvement_candidate_repository import (
    ImprovementCandidateRepository,
)
from jstock_advisor.infrastructure.local_repository.weekly_review_metrics_repository import (
    WeeklyReviewMetricsRepository,
)

_NOW = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)


def _metrics(review_week: str, rule_version: str) -> WeeklyReviewMetrics:
    return WeeklyReviewMetrics(
        metrics_id=f"BUY|{rule_version}|ALL|{review_week}",
        review_week=review_week,
        recommendation_type=RecommendationType.BUY,
        rule_version=rule_version,
        segment_key=None,
        sample_count=20,
        conclusive_count=20,
        success_rate_pct=55.0,
        average_return_pct=1.0,
        average_excess_return_pct=0.2,
        period_start=dt.date(2026, 8, 3),
        period_end=dt.date(2026, 8, 9),
        generated_at=_NOW,
    )


def test_list_by_type_version_segment_excludes_other_rule_versions(tmp_path: Path) -> None:
    repo = WeeklyReviewMetricsRepository(store_dir=tmp_path)
    repo.save(_metrics("2026-W31", "v10"))
    repo.save(_metrics("2026-W32", "v11"))

    rows = repo.list_by_type_version_segment(RecommendationType.BUY, "v11", None)
    assert [r.review_week for r in rows] == ["2026-W32"]


def test_list_by_type_version_segment_orders_by_week_descending(tmp_path: Path) -> None:
    repo = WeeklyReviewMetricsRepository(store_dir=tmp_path)
    repo.save(_metrics("2026-W30", "v11"))
    repo.save(_metrics("2026-W32", "v11"))
    repo.save(_metrics("2026-W31", "v11"))

    rows = repo.list_by_type_version_segment(RecommendationType.BUY, "v11", None)
    assert [r.review_week for r in rows] == ["2026-W32", "2026-W31", "2026-W30"]


def test_improvement_candidate_repository_roundtrip(tmp_path: Path) -> None:
    repo = ImprovementCandidateRepository(store_dir=tmp_path)
    candidate = ImprovementCandidate(
        candidate_id="BUY|v11|ALL|PERFORMANCE_DEGRADED|2026-W32",
        candidate_key="BUY|v11|ALL|PERFORMANCE_DEGRADED",
        recommendation_type=RecommendationType.BUY,
        rule_version="v11",
        review_week="2026-W32",
        evaluation_period_start=dt.date(2026, 8, 3),
        evaluation_period_end=dt.date(2026, 8, 9),
        sample_count=20,
        conclusive_count=20,
        consecutive_bad_weeks=1,
        priority=ImprovementPriority.B,
        problem_category=PROBLEM_CATEGORY_PERFORMANCE_DEGRADED,
        recommended_action=ImprovementAction.ADJUST_THRESHOLD,
        is_current_rule_version=True,
    )
    repo.save(candidate)

    assert repo.get(candidate.candidate_id) == candidate
    assert repo.list_by_candidate_key(candidate.candidate_key) == [candidate]
