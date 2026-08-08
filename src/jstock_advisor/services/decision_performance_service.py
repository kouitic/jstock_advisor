"""判定精度向上機能Phase A: DecisionSnapshotの成績集計(非永続)。

performance_metrics_service.pyのMetricsBucket/build_metrics_bucketをそのまま
再利用し、recommendation_idではなくdecision_id経由でDecisionSnapshotとjoinする
(既存のPerformanceMetricsService/週次改善レビューとは完全に独立)。
Phase A時点ではDecisionSnapshotのスコア項目が全てNoneのため、意味のある内訳は
by_decision_type/by_existing_action程度に限られる(スコアリングロジックが揃う
Phase B以降で本格的に活用される)。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field

from jstock_advisor.domain.entities.decision_snapshot import DecisionSnapshot
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.services.performance_metrics_service import MetricsBucket, build_metrics_bucket


@dataclass(frozen=True)
class DecisionPerformanceSummary:
    generated_at: dt.datetime
    horizon_business_days: int | None
    overall: MetricsBucket
    by_decision_type: list[MetricsBucket] = field(default_factory=list)
    by_existing_action: list[MetricsBucket] = field(default_factory=list)
    by_model_version: list[MetricsBucket] = field(default_factory=list)


def _group_bucket(
    pairs: list[tuple[EvaluationResult, DecisionSnapshot]],
    key_fn: Callable[[DecisionSnapshot], str],
) -> list[MetricsBucket]:
    grouped: dict[str, list[EvaluationResult]] = {}
    for evaluation, decision in pairs:
        key = key_fn(decision)
        grouped.setdefault(key, []).append(evaluation)
    return [build_metrics_bucket(key, evals) for key, evals in sorted(grouped.items())]


class DecisionPerformanceService:
    def __init__(
        self,
        evaluation_repository: EvaluationResultRepository | None = None,
        decision_repository: DecisionSnapshotRepository | None = None,
    ) -> None:
        self._evaluations = evaluation_repository or EvaluationResultRepository()
        self._decisions = decision_repository or DecisionSnapshotRepository()

    def summarize(
        self, horizon_business_days: int | None = None, now: dt.datetime | None = None
    ) -> DecisionPerformanceSummary:
        # decision_idが設定されている評価のみ対象(既存recommendation_idベースの
        # 評価・週次改善レビューとは完全に独立させる)。
        evaluations = [e for e in self._evaluations.list_all() if e.decision_id is not None]
        if horizon_business_days is not None:
            evaluations = [
                e for e in evaluations if e.horizon_business_days == horizon_business_days
            ]

        pairs: list[tuple[EvaluationResult, DecisionSnapshot]] = []
        for evaluation in evaluations:
            if evaluation.decision_id is None:
                continue
            decision = self._decisions.get(evaluation.decision_id)
            if decision is not None:
                pairs.append((evaluation, decision))

        return DecisionPerformanceSummary(
            generated_at=now or dt.datetime.now(dt.UTC),
            horizon_business_days=horizon_business_days,
            overall=build_metrics_bucket("overall", evaluations),
            by_decision_type=_group_bucket(pairs, lambda d: d.decision_type.value),
            by_existing_action=_group_bucket(
                pairs, lambda d: d.existing_action.value if d.existing_action else "UNKNOWN"
            ),
            by_model_version=_group_bucket(pairs, lambda d: d.model_version),
        )
