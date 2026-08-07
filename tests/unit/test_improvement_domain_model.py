"""改善候補ドメインモデルの基本テスト(振り返り機能改修)。"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.enums import (
    ImprovementAction,
    ImprovementPriority,
    ImprovementTaskStatus,
    RecommendationType,
)
from jstock_advisor.domain.entities.improvement import (
    PROBLEM_CATEGORY_EVALUATION_CRITERIA_UNDEFINED,
    PROBLEM_CATEGORY_PERFORMANCE_DEGRADED,
    ImprovementCandidate,
    ImprovementTask,
    WeeklyReviewMetrics,
)
from jstock_advisor.domain.improvement_rules import build_candidate_key

_NOW = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)


def test_build_candidate_key_is_deterministic_regardless_of_reason_code_order() -> None:
    key1 = build_candidate_key(
        RecommendationType.BUY, "v11", None, PROBLEM_CATEGORY_PERFORMANCE_DEGRADED
    )
    key2 = build_candidate_key(
        RecommendationType.BUY, "v11", None, PROBLEM_CATEGORY_PERFORMANCE_DEGRADED
    )
    assert key1 == key2 == "BUY|v11|ALL|PERFORMANCE_DEGRADED"


def test_build_candidate_key_uses_all_for_missing_segment() -> None:
    key = build_candidate_key(
        RecommendationType.WATCH, "v15", None, PROBLEM_CATEGORY_EVALUATION_CRITERIA_UNDEFINED
    )
    assert key == "WATCH|v15|ALL|EVALUATION_CRITERIA_UNDEFINED"


def test_weekly_review_metrics_roundtrip() -> None:
    metrics = WeeklyReviewMetrics(
        metrics_id="BUY|v11|ALL|2026-W32",
        review_week="2026-W32",
        recommendation_type=RecommendationType.BUY,
        rule_version="v11",
        segment_key=None,
        sample_count=30,
        conclusive_count=30,
        success_rate_pct=49.0,
        average_return_pct=1.2,
        average_excess_return_pct=-0.5,
        period_start=dt.date(2026, 8, 3),
        period_end=dt.date(2026, 8, 9),
        generated_at=_NOW,
    )
    assert metrics.success_rate_pct == 49.0


def test_improvement_candidate_defaults() -> None:
    candidate = ImprovementCandidate(
        candidate_id="BUY|v11|ALL|PERFORMANCE_DEGRADED|2026-W32",
        candidate_key="BUY|v11|ALL|PERFORMANCE_DEGRADED",
        recommendation_type=RecommendationType.BUY,
        rule_version="v11",
        review_week="2026-W32",
        evaluation_period_start=dt.date(2026, 8, 3),
        evaluation_period_end=dt.date(2026, 8, 9),
        sample_count=30,
        conclusive_count=30,
        consecutive_bad_weeks=1,
        priority=ImprovementPriority.B,
        problem_category=PROBLEM_CATEGORY_PERFORMANCE_DEGRADED,
        recommended_action=ImprovementAction.ADJUST_THRESHOLD,
        is_current_rule_version=True,
    )
    assert candidate.reason_codes == ()
    assert candidate.expected_improvement_pct is None
    assert candidate.evidence == ()


def test_improvement_task_defaults() -> None:
    task = ImprovementTask(
        candidate_key="BUY|v11|ALL|PERFORMANCE_DEGRADED",
        recommendation_type=RecommendationType.BUY,
        rule_version="v11",
        priority=ImprovementPriority.B,
        status=ImprovementTaskStatus.CANDIDATE,
        created_at=_NOW,
        updated_at=_NOW,
    )
    assert task.github_issue_number is None
    assert task.last_commented_review_week is None
