"""ルール改善案の限定的バックテスト(要求仕様41・44・45節)。

このシステムは判定に使用した生データ(個別の財務指標や株価履歴全体)をそのまま
永続化しておらず、Recommendationスナップショットに記録された値のみを事後的に
参照できる。そのため、スクリーニング・利確・売却の各判定パイプライン全体を
過去の任意のパラメータで完全に再現することはできない。

本サービスが提供するのは、既存の推奨のうち「提案する新しい閾値では条件を
満たさなくなるもの」を取り除いた場合に、実績(EvaluationResult)ベースの
成績がどう変化するかを見る限定的な感応度分析である。

対応範囲:
- 閾値を"厳しくする"方向の変更のみ対応する(例: 最低総合利回りの引き上げ)。
  閾値を緩める変更は、当時その条件で除外され記録が残っていない銘柄が
  存在するため、生存バイアスにより検証不能(データ不足として報告する)。
- _METRIC_REGISTRYに登録された、Recommendationスナップショットに直接
  記録されている指標のみ対応する。未登録のtargetは「対象外」として明示する。
"""

from __future__ import annotations

from dataclasses import dataclass

from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.performance_metrics_service import (
    MetricsBucket,
    build_metrics_bucket,
)


@dataclass(frozen=True)
class _MetricSpec:
    attribute: str  # Recommendationの属性名
    # "min"(閾値以上でPASS、引き上げのみ検証可) or "max"(閾値以下でPASS、引き下げのみ検証可)
    direction: str
    applicable_types: tuple[RecommendationType, ...]


_METRIC_REGISTRY: dict[str, _MetricSpec] = {
    "screening.total_yield.min_total_yield_pct": _MetricSpec(
        attribute="total_yield_pct_at_recommendation",
        direction="min",
        applicable_types=(RecommendationType.BUY, RecommendationType.WATCH_BUY),
    ),
}


@dataclass(frozen=True)
class BacktestResult:
    target: str
    supported: bool
    reason_unsupported: str | None
    current_value: float
    proposed_value: float
    evaluation_count_current: int = 0
    evaluation_count_proposed: int = 0
    current_performance: MetricsBucket | None = None
    proposed_performance: MetricsBucket | None = None
    excluded_recommendation_ids: list[str] | None = None


class BacktestService:
    def __init__(
        self,
        recommendation_repository: RecommendationRepository | None = None,
        evaluation_repository: EvaluationResultRepository | None = None,
    ) -> None:
        self._recommendations = recommendation_repository or RecommendationRepository()
        self._evaluations = evaluation_repository or EvaluationResultRepository()

    def run(self, target: str, current_value: float, proposed_value: float) -> BacktestResult:
        spec = _METRIC_REGISTRY.get(target)
        if spec is None:
            return BacktestResult(
                target=target,
                supported=False,
                reason_unsupported=(
                    f"target={target} はバックテスト未対応です"
                    "(登録済みの単一指標感応度分析のみサポートしています)"
                ),
                current_value=current_value,
                proposed_value=proposed_value,
            )

        if not self._is_tightening(spec, current_value, proposed_value):
            return BacktestResult(
                target=target,
                supported=False,
                reason_unsupported=(
                    "閾値を緩める方向の変更は、当時その条件を満たさず除外された銘柄の"
                    "データが存在しないため検証できません(生存バイアス)"
                ),
                current_value=current_value,
                proposed_value=proposed_value,
            )

        pairs = self._collect_pairs(spec)
        if not pairs:
            return BacktestResult(
                target=target,
                supported=False,
                reason_unsupported="対象となる評価済みの推奨がありません(データ不足)",
                current_value=current_value,
                proposed_value=proposed_value,
            )

        retained_ids: set[str] = set()
        proposed_evals: list[EvaluationResult] = []
        for evaluation, recommendation in pairs:
            value = getattr(recommendation, spec.attribute)
            if self._passes(spec, value, proposed_value):
                retained_ids.add(recommendation.recommendation_id)
                proposed_evals.append(evaluation)

        excluded_ids = [
            r.recommendation_id for _, r in pairs if r.recommendation_id not in retained_ids
        ]

        return BacktestResult(
            target=target,
            supported=True,
            reason_unsupported=None,
            current_value=current_value,
            proposed_value=proposed_value,
            evaluation_count_current=len(pairs),
            evaluation_count_proposed=len(proposed_evals),
            current_performance=build_metrics_bucket("current", [e for e, _ in pairs]),
            proposed_performance=build_metrics_bucket("proposed", proposed_evals),
            excluded_recommendation_ids=excluded_ids,
        )

    def _collect_pairs(self, spec: _MetricSpec) -> list[tuple[EvaluationResult, Recommendation]]:
        pairs: list[tuple[EvaluationResult, Recommendation]] = []
        for evaluation in self._evaluations.list_all():
            recommendation = self._recommendations.get(evaluation.recommendation_id)
            if recommendation is None or recommendation.recommendation_type not in (
                spec.applicable_types
            ):
                continue
            if getattr(recommendation, spec.attribute) is None:
                continue
            pairs.append((evaluation, recommendation))
        return pairs

    @staticmethod
    def _is_tightening(spec: _MetricSpec, current_value: float, proposed_value: float) -> bool:
        if spec.direction == "min":
            return proposed_value > current_value
        return proposed_value < current_value

    @staticmethod
    def _passes(spec: _MetricSpec, value: float, threshold: float) -> bool:
        if spec.direction == "min":
            return value >= threshold
        return value <= threshold
