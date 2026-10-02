"""推奨の成績集計サービス(要求仕様37〜40節)。

recommendation_evaluation_serviceが生成したEvaluationResultを、推奨種別・信頼度・
ルールバージョンごとに集計し、成功率や平均リターンを算出する。horizon_business_daysを
指定しない場合は全ホライズンを合算するため、短期・長期の結果が混在する点に注意
(比較する際はhorizon_business_daysを指定して呼び出すことを推奨)。
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Iterator
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

# 成功率算出の分母から除外するラベル(判断の巧拙ではなくデータ欠如・対象外を示すため)。
# INCONCLUSIVEはdetermine_evaluation_label()がWATCH_BEFORE_EARNINGS等「自動評価の
# 対象外」の種別(2026-08-20時点、Issue #10で継続検討中)へ無条件に付与するラベルで
# あり、DATA_ISSUE(データ取得失敗)と同様に「判断の巧拙を測れない」ケースである。
# 従来はDATA_ISSUEのみ除外しておりINCONCLUSIVEが分母に残ったまま失敗扱いになる
# (=対象種別が常に成功率0%になる)不具合があったため、振り返り機能改修で
# INCONCLUSIVEも除外対象に追加した。
_EXCLUDED_FROM_SUCCESS_RATE = frozenset({EvaluationLabel.DATA_ISSUE, EvaluationLabel.INCONCLUSIVE})
_SUCCESS_LABELS = frozenset({EvaluationLabel.SUCCESS, EvaluationLabel.ACCEPTABLE})


@dataclass(frozen=True)
class MetricsBucket:
    key: str
    count: int
    conclusive_count: int
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
        conclusive_count=len(conclusive),
        success_rate_pct=success_rate_pct,
        avg_price_return_pct=avg_price_return_pct,
        avg_excess_return_pct=avg_excess_return_pct,
        label_counts=label_counts,
    )


class _FloatSum:
    """float の合計を、組み込みの `sum()`(Python 3.12 以降)と**同じ手順・同じ結果**で累積する。

    Python 3.12 の `sum()` は float に補償付き加算(Neumaier)を使う。`build_metrics_bucket()` は
    値のリストを `sum()` するため、値を 1 件ずつ足し込む集計器が同じ結果(bit 単位)を返すには、
    同じ手順で累積する必要がある(通常の `+=` では、最後の桁が食い違いうる)。
    """

    __slots__ = ("_compensation", "_total")

    def __init__(self) -> None:
        self._total = 0.0
        self._compensation = 0.0

    def add(self, value: float) -> None:
        total = self._total
        new_total = total + value
        if abs(total) >= abs(value):
            self._compensation += (total - new_total) + value
        else:
            self._compensation += (value - new_total) + total
        self._total = new_total

    @property
    def value(self) -> float:
        # sum() と同じ: 補償項が 0 でなく有限のときだけ足し戻す。
        if self._compensation and math.isfinite(self._compensation):
            return self._total + self._compensation
        return self._total


class MetricsAccumulator:
    """評価を 1 件ずつ足し込んで `MetricsBucket` を作る集計器(Issue #377)。

    `build_metrics_bucket()` は評価の**リスト全体**を受け取るため、呼び出し側が対象の評価を
    全件保持する必要がある(週次改善レビューで、保持したまま Lambda の Memory が上限に
    達した)。本クラスは、必要な集計値(件数・成功件数・合計)だけを持ち、評価そのものは保持しない。

    ★ **意味論は `build_metrics_bucket()` と完全に同じ**である(同じ入力を、同じ順序で
      `add()` すれば、`to_bucket()` は `build_metrics_bucket()` と同じ値を返す)。
      - DATA_ISSUE / INCONCLUSIVE は成功率の分母(conclusive)から除外する。
      - SUCCESS / ACCEPTABLE を成功として数える。
      - 平均リターンは、全件の price_return_pct の平均。excess_return_pct は None を除いた平均。
      - 分母が 0 のときの成功率・平均は None。
    """

    __slots__ = (
        "_conclusive",
        "_count",
        "_excess_count",
        "_excess_sum",
        "_label_counts",
        "_price_sum",
        "_success",
    )

    def __init__(self) -> None:
        self._count = 0
        self._conclusive = 0
        self._success = 0
        self._price_sum = _FloatSum()
        self._excess_sum = _FloatSum()
        self._excess_count = 0
        self._label_counts: dict[str, int] = {}

    @property
    def count(self) -> int:
        return self._count

    def add(self, evaluation: EvaluationResult) -> None:
        label = evaluation.evaluation_label
        self._count += 1
        self._label_counts[label.value] = self._label_counts.get(label.value, 0) + 1
        if label not in _EXCLUDED_FROM_SUCCESS_RATE:
            self._conclusive += 1
            if label in _SUCCESS_LABELS:
                self._success += 1
        self._price_sum.add(evaluation.price_return_pct)
        if evaluation.excess_return_pct is not None:
            self._excess_sum.add(evaluation.excess_return_pct)
            self._excess_count += 1

    def to_bucket(self, key: str) -> MetricsBucket:
        return MetricsBucket(
            key=key,
            count=self._count,
            conclusive_count=self._conclusive,
            success_rate_pct=(
                self._success / self._conclusive * 100 if self._conclusive else None
            ),
            avg_price_return_pct=(
                self._price_sum.value / self._count if self._count else None
            ),
            avg_excess_return_pct=(
                self._excess_sum.value / self._excess_count if self._excess_count else None
            ),
            label_counts=dict(self._label_counts),
        )


# Recommendationを一括取得する単位(Issue #540)。BatchGetItemの上限(100件/リクエスト)に合わせる。
_RECOMMENDATION_BATCH_SIZE = 100


def iter_evaluations_with_recommendations(
    evaluations: Iterable[EvaluationResult],
    recommendations: RecommendationRepository,
    batch_size: int = _RECOMMENDATION_BATCH_SIZE,
) -> Iterator[tuple[EvaluationResult, Recommendation | None]]:
    """評価を1件ずつ受け取り、対応するRecommendationを`get_many()`で一括取得して返す(Issue #540)。

    従来は評価ごとに`get()`を呼び(N+1)、評価の件数分のRecommendation(1件約20KB)を
    別個のオブジェクトとして保持していた。ここでは`batch_size`件ずつまとめて取得し、
    呼び出し側が1件ずつ消費したら破棄できる形で返す(件数に比例して保持しない)。

    ★ 列挙順は入力の`evaluations`と同じ(集計の浮動小数点の合計が従来と同じ結果になるため)。
    ★ 対応するRecommendationが存在しない評価は、`(evaluation, None)`で返す(捨てない。
      「推奨が無い評価も全体の集計には入る」という従来の意味を保つため)。
    """
    batch: list[EvaluationResult] = []
    for evaluation in evaluations:
        batch.append(evaluation)
        if len(batch) >= batch_size:
            yield from _resolve_recommendations(batch, recommendations)
            batch = []
    if batch:
        yield from _resolve_recommendations(batch, recommendations)


def _resolve_recommendations(
    batch: list[EvaluationResult], recommendations: RecommendationRepository
) -> Iterator[tuple[EvaluationResult, Recommendation | None]]:
    by_id = recommendations.get_many({e.recommendation_id for e in batch})
    for evaluation in batch:
        yield evaluation, by_id.get(evaluation.recommendation_id)


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
        # Issue #540: 評価を1件ずつ読み(iter_all)、Recommendationは100件ずつ一括取得し、
        # 集計は既存のMetricsAccumulatorへ1件ずつ足し込む。評価・Recommendationの全件を
        # listへ保持しない。結果(各bucketの値)は従来のbuild_metrics_bucket()と同一
        # (MetricsAccumulatorがbit単位で同じ結果を返すことは#377のテストが固定している)。
        def _matching_evaluations() -> Iterator[EvaluationResult]:
            for evaluation in self._evaluations.iter_all():
                if (
                    horizon_business_days is None
                    or evaluation.horizon_business_days == horizon_business_days
                ):
                    yield evaluation

        overall = MetricsAccumulator()
        by_type: dict[str, MetricsAccumulator] = {}
        by_confidence: dict[str, MetricsAccumulator] = {}
        by_rule_version: dict[str, MetricsAccumulator] = {}
        for evaluation, recommendation in iter_evaluations_with_recommendations(
            _matching_evaluations(), self._recommendations
        ):
            overall.add(evaluation)
            if recommendation is None:
                continue
            by_type.setdefault(recommendation.recommendation_type.value, MetricsAccumulator()).add(
                evaluation
            )
            by_confidence.setdefault(recommendation.confidence.value, MetricsAccumulator()).add(
                evaluation
            )
            by_rule_version.setdefault(recommendation.rule_version, MetricsAccumulator()).add(
                evaluation
            )

        return PerformanceSummary(
            generated_at=now or dt.datetime.now(dt.UTC),
            horizon_business_days=horizon_business_days,
            overall=overall.to_bucket("overall"),
            by_recommendation_type=[a.to_bucket(k) for k, a in sorted(by_type.items())],
            by_confidence=[a.to_bucket(k) for k, a in sorted(by_confidence.items())],
            by_rule_version=[a.to_bucket(k) for k, a in sorted(by_rule_version.items())],
        )
