"""判定精度向上機能Phase A: DecisionSnapshotの成績集計(非永続)。

コードレビュー対応: DecisionSnapshot専用のEvaluationResultは生成しない
(recommendation_evaluation_service.pyが既に生成しているEvaluationResultを
そのまま再利用する)。よってjoinは`EvaluationResult.recommendation_id` ==
`DecisionSnapshot.recommendation_id`で行い、Phase A成績評価の対象ホライズン
(既定5/20/60/120/250営業日、config.decision_evaluation.horizons_business_days)
のみへ絞り込む。これにより1営業日の短期チェックポイントや7暦日評価
(週次改善レビュー専用)がDecisionPerformanceへ混入しない。

performance_metrics_service.pyのMetricsBucket/build_metrics_bucketをそのまま
再利用し、既存のPerformanceMetricsService/週次改善レビューとは完全に独立させる
(既存ファイルは変更しない)。
Phase A時点ではDecisionSnapshotのスコア項目が全てNoneのため、意味のある内訳は
by_decision_type/by_existing_action程度に限られる(スコアリングロジックが揃う
Phase B以降で本格的に活用される)。
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
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
    # MFE(Maximum Favorable Excursion)/MAE(Maximum Adverse Excursion)。
    # 既存EvaluationResult.max_gain_pct/max_drawdown_pctの平均値をそのまま使う。
    median_price_return_pct: float | None = None
    avg_mfe_pct: float | None = None
    avg_mae_pct: float | None = None
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
        config: AppConfig | None = None,
    ) -> None:
        self._evaluations = evaluation_repository or EvaluationResultRepository()
        self._decisions = decision_repository or DecisionSnapshotRepository()
        self._config = config or load_config()

    def summarize(
        self, horizon_business_days: int | None = None, now: dt.datetime | None = None
    ) -> DecisionPerformanceSummary:
        phase_a_horizons = set(self._config.decision_evaluation.horizons_business_days)
        # Phase A成績評価の対象ホライズンのみ(既存の1営業日共通チェックポイント・
        # 7暦日評価・その他RecommendationType固有ホライズンは対象外)。
        evaluations = [
            e
            for e in self._evaluations.list_all()
            if e.horizon_business_days is not None and e.horizon_business_days in phase_a_horizons
        ]
        if horizon_business_days is not None:
            evaluations = [
                e for e in evaluations if e.horizon_business_days == horizon_business_days
            ]

        decisions_by_recommendation_id = {
            d.recommendation_id: d for d in self._decisions.list_all() if d.recommendation_id
        }
        pairs: list[tuple[EvaluationResult, DecisionSnapshot]] = []
        for evaluation in evaluations:
            decision = decisions_by_recommendation_id.get(evaluation.recommendation_id)
            if decision is not None:
                pairs.append((evaluation, decision))

        price_returns = [e.price_return_pct for e, _ in pairs]
        mfe_values = [e.max_gain_pct for e, _ in pairs if e.max_gain_pct is not None]
        mae_values = [e.max_drawdown_pct for e, _ in pairs if e.max_drawdown_pct is not None]

        return DecisionPerformanceSummary(
            generated_at=now or dt.datetime.now(dt.UTC),
            horizon_business_days=horizon_business_days,
            overall=build_metrics_bucket("overall", [e for e, _ in pairs]),
            median_price_return_pct=(
                statistics.median(price_returns) if price_returns else None
            ),
            avg_mfe_pct=sum(mfe_values) / len(mfe_values) if mfe_values else None,
            avg_mae_pct=sum(mae_values) / len(mae_values) if mae_values else None,
            by_decision_type=_group_bucket(pairs, lambda d: d.decision_type.value),
            by_existing_action=_group_bucket(
                pairs, lambda d: d.existing_action.value if d.existing_action else "UNKNOWN"
            ),
            by_model_version=_group_bucket(pairs, lambda d: d.model_version),
        )
