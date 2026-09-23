"""週次評価集計(WeeklyEvaluationAggregate。Issue #537)。

週次改善レビューが毎週、raw の EvaluationResult を全件 Scan して再集計する構造をやめるための
**中間集計**である。3 層のうちの 2 番目にあたる。

```
① Raw        EvaluationResult                評価事実の正本(履歴として保持。削除・TTL は行わない)
② Aggregate  WeeklyEvaluationAggregate(本モジュール)  raw から得られる中間集計(review_week ×
  推奨種別 × rule_version)
③ Review     WeeklyReviewMetrics / ImprovementCandidate / GitHub Issue   レビュー結果(既存)
```

★ WeeklyReviewMetrics(レビュー結果)とは別物である。Aggregate は「件数」と「合計」だけを持ち、
  平均・成功率は読む側が求める(丸めを持ち込まない)。

★ 意味論は `performance_metrics_service.build_metrics_bucket()` / `MetricsAccumulator` と同じである
  (DATA_ISSUE / INCONCLUSIVE は成功率の分母から除外。SUCCESS / ACCEPTABLE を成功とする。
  平均リターンは全件、超過リターンは None を除く)。定数は domain 層の本モジュールが持ち、
  同値であることをテストで固定する(service 層から domain 層へは import できないため)。

★ 合計は Decimal で持つ(DynamoDB の Number は 38 桁の 10 進数で、ADD が厳密に加算できる)。
  float の合計と比べて、最後の桁が食い違いうる(旧方式は Neumaier 補償付きの float 加算)。
  食い違いは 1 ulp 級であり、指標(パーセント表示・閾値比較)には影響しない。
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from decimal import Decimal

from pydantic import Field

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import EvaluationLabel, RecommendationType
from jstock_advisor.domain.entities.evaluation import EvaluationResult

AGGREGATE_SCHEMA_VERSION = 1

#: 成功率の分母から除外するラベル(performance_metrics_service._EXCLUDED_FROM_SUCCESS_RATE と同値)。
EXCLUDED_FROM_SUCCESS_RATE = frozenset({EvaluationLabel.DATA_ISSUE, EvaluationLabel.INCONCLUSIVE})
#: 成功として数えるラベル(performance_metrics_service._SUCCESS_LABELS と同値)。
SUCCESS_LABELS = frozenset({EvaluationLabel.SUCCESS, EvaluationLabel.ACCEPTABLE})


def review_week_label(d: dt.date) -> str:
    """ISO 週のラベル(例 `2026-W38`)。週次改善レビューの `review_week` と同じ形式。"""
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def aggregate_item_key(recommendation_type: RecommendationType | str, rule_version: str) -> str:
    """1 週の中の集計行のキー。"""
    value = (
        recommendation_type.value
        if isinstance(recommendation_type, RecommendationType)
        else recommendation_type
    )
    return f"AGG#{value}#{rule_version}"


def _to_decimal(value: float) -> Decimal:
    """float を、最短の往復表現(repr)経由で Decimal にする(2 進の誤差を新たに持ち込まない)。

    有限でない値(NaN / ±inf)は集計へ入れられない(合計が壊れる)。呼び出し側は、その評価の
    保存自体を成立させない(USER 決定 Q-2 = rollback)。
    """
    if not math.isfinite(value):
        raise ValueError("非有限の値は週次評価集計へ加算できません")
    return Decimal(repr(value))


@dataclass(frozen=True)
class AggregateDelta:
    """1 件の EvaluationResult が集計へ加える増分。"""

    review_week: str
    label: str
    conclusive: int
    success: int
    price_return: Decimal
    excess_return: Decimal
    excess_count: int


def delta_of(evaluation: EvaluationResult) -> AggregateDelta:
    """EvaluationResult 1 件の増分。週は **evaluation_date** で決める(evaluated_at ではない)。"""
    label = evaluation.evaluation_label
    conclusive = 0 if label in EXCLUDED_FROM_SUCCESS_RATE else 1
    success = 1 if (conclusive and label in SUCCESS_LABELS) else 0
    excess = evaluation.excess_return_pct
    return AggregateDelta(
        review_week=review_week_label(evaluation.evaluation_date),
        label=label.value,
        conclusive=conclusive,
        success=success,
        price_return=_to_decimal(evaluation.price_return_pct),
        excess_return=_to_decimal(excess) if excess is not None else Decimal(0),
        excess_count=0 if excess is None else 1,
    )


class WeeklyEvaluationAggregate(Entity):
    """review_week × recommendation_type × rule_version の集計 1 行。"""

    review_week: str
    recommendation_type: RecommendationType
    rule_version: str
    sample_count: int = 0
    conclusive_count: int = 0
    success_count: int = 0
    price_return_sum: Decimal = Decimal(0)
    price_return_count: int = 0
    excess_return_sum: Decimal = Decimal(0)
    excess_return_count: int = 0
    label_counts: dict[str, int] = Field(default_factory=dict)
    updated_at: dt.datetime
    schema_version: int = AGGREGATE_SCHEMA_VERSION

    @property
    def item_key(self) -> str:
        return aggregate_item_key(self.recommendation_type, self.rule_version)

    def apply(self, delta: AggregateDelta, now: dt.datetime) -> None:
        """増分を加える(週が一致すること)。in-memory 実装と backfill の集計で使う。"""
        if delta.review_week != self.review_week:
            raise ValueError("増分の週と集計の週が一致しません")
        self.sample_count += 1
        self.conclusive_count += delta.conclusive
        self.success_count += delta.success
        self.price_return_sum += delta.price_return
        self.price_return_count += 1
        self.excess_return_sum += delta.excess_return
        self.excess_return_count += delta.excess_count
        self.label_counts[delta.label] = self.label_counts.get(delta.label, 0) + 1
        self.updated_at = now
