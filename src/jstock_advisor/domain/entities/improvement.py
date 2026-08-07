"""週次改善レビューのドメインモデル(振り返り機能改修)。

日次で確定したRecommendationごとの7暦日評価(EvaluationResult)を、
RecommendationType×rule_version×segment_key単位で週次集計したものが
WeeklyReviewMetrics(履歴の正、Candidateの有無に関わらず毎週保存)。そこから
検出された改善候補がImprovementCandidate(週次スナップショット)、GitHub Issueとの
対応関係を複数週に渡って追跡する状態機械がImprovementTask(candidate_key単位で
1件のみ、原子的な状態遷移はinfrastructure.aws.improvement_task_trackerが担う)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import (
    ImprovementAction,
    ImprovementPriority,
    ImprovementTaskStatus,
    RecommendationType,
)

# problem_categoryの固定値(Issueスパム防止のため、RecommendationType×rule_version×
# segmentにつき週1Candidateまでとする、両方に該当した場合はreason_codesで区別する)。
PROBLEM_CATEGORY_PERFORMANCE_DEGRADED = "PERFORMANCE_DEGRADED"
PROBLEM_CATEGORY_EVALUATION_CRITERIA_UNDEFINED = "EVALUATION_CRITERIA_UNDEFINED"


class WeeklyReviewMetrics(Entity):
    """RecommendationType×rule_version×segment_key×review_week単位の週次実績。

    Candidateの有無に関わらず、サンプルが1件以上ある組み合わせは毎週保存する
    (履歴の正。前週比較・consecutive_bad_weeksの算出に使う)。成績指標は
    PerformanceMetricsService.build_metrics_bucket()と同じ0〜100スケール
    (_pct命名)で統一する。
    """

    metrics_id: str
    review_week: str  # ISO week, 例 "2026-W32"
    recommendation_type: RecommendationType
    rule_version: str
    segment_key: str | None = None
    sample_count: int
    conclusive_count: int
    success_rate_pct: float | None = None
    average_return_pct: float | None = None
    average_excess_return_pct: float | None = None
    period_start: dt.date
    period_end: dt.date
    generated_at: dt.datetime


class ImprovementCandidate(Entity):
    """週次スナップショットとしての改善候補(1レビュー週につき1件)。"""

    candidate_id: str  # f"{candidate_key}|{review_week}"
    candidate_key: str  # 複数週を跨ぐ同一問題の識別子(ImprovementTaskのPKと一致)
    recommendation_type: RecommendationType
    rule_version: str
    segment_key: str | None = None
    review_week: str
    evaluation_period_start: dt.date
    evaluation_period_end: dt.date
    sample_count: int
    conclusive_count: int
    success_rate_pct: float | None = None
    average_return_pct: float | None = None
    average_excess_return_pct: float | None = None
    previous_success_rate_pct: float | None = None
    success_rate_change_points: float | None = None
    consecutive_bad_weeks: int
    priority: ImprovementPriority
    problem_category: str
    reason_codes: tuple[str, ...] = ()
    # バックテストしていない場合は必ずNone(要求仕様: 推測で期待改善値を書かない)
    expected_improvement_pct: float | None = None
    recommended_action: ImprovementAction
    evidence: tuple[str, ...] = ()
    # True=現行rule_version/False=過去版/None=判定不能(resolve_current_rule_version
    # がACTIVE版・直近Recommendationのどちらも得られなかった場合)。Trueの場合のみ
    # GitHub新規Issue化の対象になる(既存OPEN Issueへのコメント追記は対象外)。
    is_current_rule_version: bool | None = None


class ImprovementTask(Entity):
    """candidate_key単位で1件のみ存在する、GitHub Issue対応状況の状態機械。"""

    candidate_key: str
    recommendation_type: RecommendationType
    rule_version: str
    segment_key: str | None = None
    priority: ImprovementPriority
    status: ImprovementTaskStatus
    github_issue_number: int | None = None
    github_issue_url: str | None = None
    # Closed Issue検出後、再発として新規Issueを作った場合に旧番号を退避する。
    previous_github_issue_number: int | None = None
    issue_claimed_at: dt.datetime | None = None
    issue_claim_expires_at: dt.datetime | None = None
    last_commented_review_week: str | None = None
    comment_claim_review_week: str | None = None
    comment_claim_expires_at: dt.datetime | None = None
    created_at: dt.datetime
    updated_at: dt.datetime
