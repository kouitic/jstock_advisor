"""推奨の成績集計サービス(要求仕様37〜40節)。

recommendation_evaluation_serviceが生成したEvaluationResultを、推奨種別・信頼度・
ルールバージョンごとに集計し、成功率や平均リターンを算出する。horizon_business_daysを
指定しない場合は全ホライズンを合算するため、短期・長期の結果が混在する点に注意
(比較する際はhorizon_business_daysを指定して呼び出すことを推奨)。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field

from jstock_advisor.domain.entities.enums import EvaluationLabel
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)

# 成功率算出の分母から除外するラベル(判断の巧拙ではなくデータ欠如を示すため)
_EXCLUDED_FROM_SUCCESS_RATE = frozenset({EvaluationLabel.DATA_ISSUE})
_SUCCESS_LABELS = frozenset({EvaluationLabel.SUCCESS, EvaluationLabel.ACCEPTABLE})


@dataclass(frozen=True)
class MetricsBucket:
    key: str
    count: int
    success_rate_pct: float | None
    avg_price_return_pct: float | None
    avg_excess_return_pct: float | None
    label_counts: dict[str, int]


@dataclass(frozen=True)
class PerformanceSummary:
    generated_at: dt.datetime
    horizon_business_days: int | None
    overall: MetricsBucket
    by_recommendation_type: list[MetricsBucket] = field(default_factory=list)
    by_confidence: list[MetricsBucket] = field(default_factory=list)
    by_rule_version: list[MetricsBucket] = field(default_factory=list)


def build_metrics_bucket(key: str, evaluations: list[EvaluationResult]) -> MetricsBucket:
    label_counts: dict[str, int] = {}
    for e in evaluations:
        label_counts[e.evaluation_label.value] = label_counts.get(e.evaluation_label.value, 0) + 1

    conclusive = [e for e in evaluations if e.evaluation_label not in _EXCLUDED_FROM_SUCCESS_RATE]
    success_rate_pct = (
        sum(1 for e in conclusive if e.evaluation_label in _SUCCESS_LABELS) / len(conclusive) * 100
        if conclusive
        else None
    )

    price_returns = [e.price_return_pct for e in evaluations]
    avg_price_return_pct = sum(price_returns) / len(price_returns) if price_returns else None

    excess_returns = [e.excess_return_pct for e in evaluations if e.excess_return_pct is not None]
    avg_excess_return_pct = sum(excess_returns) / len(excess_returns) if excess_returns else None

    return MetricsBucket(
        key=key,
        count=len(evaluations),
        success_rate_pct=success_rate_pct,
        avg_price_return_pct=avg_price_return_pct,
        avg_excess_return_pct=avg_excess_return_pct,
        label_counts=label_counts,
    )


def _group_bucket(
    pairs: list[tuple[EvaluationResult, Recommendation]], key_fn: Callable[[Recommendation], str]
) -> list[MetricsBucket]:
    grouped: dict[str, list[EvaluationResult]] = {}
    for evaluation, recommendation in pairs:
        key = key_fn(recommendation)
        grouped.setdefault(key, []).append(evaluation)
    return [build_metrics_bucket(key, evals) for key, evals in sorted(grouped.items())]


class PerformanceMetricsService:
    def __init__(
        self,
        evaluation_repository: EvaluationResultRepository | None = None,
        recommendation_repository: RecommendationRepository | None = None,
    ) -> None:
        self._evaluations = evaluation_repository or EvaluationResultRepository()
        self._recommendations = recommendation_repository or RecommendationRepository()

    def summarize(
        self, horizon_business_days: int | None = None, now: dt.datetime | None = None
    ) -> PerformanceSummary:
        evaluations = self._evaluations.list_all()
        if horizon_business_days is not None:
            evaluations = [
                e for e in evaluations if e.horizon_business_days == horizon_business_days
            ]

        pairs: list[tuple[EvaluationResult, Recommendation]] = []
        for evaluation in evaluations:
            recommendation = self._recommendations.get(evaluation.recommendation_id)
            if recommendation is not None:
                pairs.append((evaluation, recommendation))

        return PerformanceSummary(
            generated_at=now or dt.datetime.now(dt.UTC),
            horizon_business_days=horizon_business_days,
            overall=build_metrics_bucket("overall", evaluations),
            by_recommendation_type=_group_bucket(pairs, lambda r: r.recommendation_type.value),
            by_confidence=_group_bucket(pairs, lambda r: r.confidence.value),
            by_rule_version=_group_bucket(pairs, lambda r: r.rule_version),
        )
