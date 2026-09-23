"""週次評価集計(WeeklyEvaluationAggregate)の変換・backfill・照合・rebuild(Issue #537)。

- `aggregate_to_bucket()`: 集計行 → `MetricsBucket`。旧方式(`build_metrics_bucket()` /
  `MetricsAccumulator`)と同じ意味論で、同じ値(合計は Decimal のため最後の桁は 1 ulp
    級で異なりうる)を返す。
- `scan_raw_aggregates()`: raw の EvaluationResult(+ Recommendation の結合)から、
  全週の集計行を作る。
  backfill・照合・rebuild が共通で使う。**通常の週次レビューは呼ばない**(全件 Scan になるため)。
- `WeeklyAggregateMaintenanceService`: backfill / 照合(verify)/ rebuild。**dry-run が既定**で、
  write は `execute=True` を明示したときだけ。Production での実行は別 Human Gate(本サービスは、
  どの環境で動かすか[ローカル / Lambda]を、
    ストアの選択[`build_weekly_evaluation_aggregate_store`]に委ねる)。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.weekly_evaluation_aggregate import (
    WeeklyEvaluationAggregate,
    delta_of,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.weekly_evaluation_aggregate_store import (
    WeeklyEvaluationAggregateStore,
)
from jstock_advisor.services.performance_metrics_service import MetricsBucket

logger = logging.getLogger(__name__)
# Issue #413: INFO を CloudWatch Logs へ出力する宣言。
# 出す値は週数・行数だけで、所有者・holding_id 等を含めない。
logger.setLevel(logging.INFO)

#: Recommendation の取得は chunk 単位(BatchGetItem の上限 100 件に揃える)。週次レビューと同じ理由。
_RECOMMENDATION_JOIN_CHUNK_SIZE = 100


def aggregate_to_bucket(row: WeeklyEvaluationAggregate, key: str) -> MetricsBucket:
    """集計行から `MetricsBucket` を作る(旧方式と同じ意味論)。

    - 成功率 = 成功件数 / conclusive 件数 × 100(conclusive が 0 なら None)
    - 平均リターン = 合計 / 件数(件数 0 なら None)。超過リターンは None を除いた件数で割る
    """
    return MetricsBucket(
        key=key,
        count=row.sample_count,
        conclusive_count=row.conclusive_count,
        success_rate_pct=(
            row.success_count / row.conclusive_count * 100 if row.conclusive_count else None
        ),
        avg_price_return_pct=(
            float(row.price_return_sum) / row.price_return_count if row.price_return_count else None
        ),
        avg_excess_return_pct=(
            float(row.excess_return_sum) / row.excess_return_count
            if row.excess_return_count
            else None
        ),
        label_counts=dict(row.label_counts),
    )


@dataclass
class RawScan:
    """raw から作った集計(全週)と、走査の内訳。"""

    rows_by_week: dict[str, dict[tuple[RecommendationType, str], WeeklyEvaluationAggregate]] = (
        field(default_factory=dict)
    )
    scanned: int = 0
    matched: int = 0
    missing_recommendation_count: int = 0

    @property
    def week_count(self) -> int:
        return len(self.rows_by_week)

    @property
    def row_count(self) -> int:
        return sum(len(rows) for rows in self.rows_by_week.values())


def scan_raw_aggregates(
    evaluations: Iterable[EvaluationResult],
    recommendations: RecommendationRepository,
    horizon_calendar_days: int,
    now: dt.datetime,
    *,
    only_weeks: frozenset[str] | None = None,
) -> RawScan:
    """raw の EvaluationResult を 1 回走査し、(週, 推奨種別, rule_version) ごとに集計する。

    週次レビューの旧方式と同じ条件で数える: `horizon_calendar_days` が一致するもの /
      Recommendation と
    結合できたもの(結合できなかった評価は数えない。件数だけ `missing_recommendation_count` に残す)。
    メモリは chunk 1 つ + 集計の組の数に有界(評価もRecommendationも全件は保持しない)。
    """
    scan = RawScan()
    chunk: list[EvaluationResult] = []

    def fold() -> None:
        found = recommendations.get_many(e.recommendation_id for e in chunk)
        for evaluation in chunk:
            recommendation = found.get(evaluation.recommendation_id)
            if recommendation is None:
                scan.missing_recommendation_count += 1
                continue
            delta = delta_of(evaluation)
            rows = scan.rows_by_week.setdefault(delta.review_week, {})
            key = (recommendation.recommendation_type, recommendation.rule_version)
            row = rows.get(key)
            if row is None:
                row = rows[key] = WeeklyEvaluationAggregate(
                    review_week=delta.review_week,
                    recommendation_type=recommendation.recommendation_type,
                    rule_version=recommendation.rule_version,
                    updated_at=now,
                )
            row.apply(delta, now)
        chunk.clear()

    for evaluation in evaluations:
        scan.scanned += 1
        if evaluation.horizon_calendar_days != horizon_calendar_days:
            continue
        if only_weeks is not None and delta_of(evaluation).review_week not in only_weeks:
            continue
        scan.matched += 1
        chunk.append(evaluation)
        if len(chunk) >= _RECOMMENDATION_JOIN_CHUNK_SIZE:
            fold()
    if chunk:
        fold()
    return scan


@dataclass(frozen=True)
class BackfillPlan:
    """backfill の計画(dry-run の報告)。write は行っていない。"""

    week_count: int
    row_count: int
    matched_evaluations: int
    scanned_evaluations: int
    missing_recommendation_count: int
    #  想定 write 数(Transaction の項目数の合計)。1 週 = 集計行 + 状態 + PENDING + REBUILD の 4
    # 項目分の固定費。
    estimated_write_items: int
    first_week: str | None
    last_week: str | None


@dataclass(frozen=True)
class WeekMismatch:
    review_week: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class VerifyReport:
    weeks_checked: int
    mismatches: tuple[WeekMismatch, ...]
    marked_rebuild_required: tuple[str, ...] = ()

    @property
    def consistent(self) -> bool:
        return not self.mismatches


_PER_WEEK_FIXED_WRITE_ITEMS = 3  # 週の状態 + PENDING + REBUILD


class WeeklyAggregateMaintenanceService:
    """backfill / 照合 / rebuild。dry-run が既定(`execute=False`)。"""

    def __init__(
        self,
        store: WeeklyEvaluationAggregateStore,
        evaluations: Iterable[EvaluationResult] | None,
        recommendations: RecommendationRepository,
        horizon_calendar_days: int,
    ) -> None:
        self._store = store
        self._evaluations_source = evaluations
        self._recommendations = recommendations
        self._horizon = horizon_calendar_days

    def _scan(self, now: dt.datetime, only_weeks: frozenset[str] | None = None) -> RawScan:
        if self._evaluations_source is None:
            raise ValueError("raw の EvaluationResult の走査元がありません")
        return scan_raw_aggregates(
            self._evaluations_source,
            self._recommendations,
            self._horizon,
            now,
            only_weeks=only_weeks,
        )

    # --- backfill ---------------------------------------------------------

    def plan_backfill(self, now: dt.datetime) -> BackfillPlan:
        """read-only。全履歴を 1 回走査して、対象週数・行数・想定 write 数を報告する。"""
        return _plan_of(self._scan(now))

    def execute_backfill(self, now: dt.datetime) -> BackfillPlan:
        """全履歴の集計を、週ごとに SET(上書き = 冪等)で作り、backfill 状態を COMPLETE にする。

        ★ Aggregate の書き込み経路(評価の保存 Transaction)を ON にする**前**に実行する想定
          (切替の順序: deploy[OFF] → backfill → 書き込み ON + 照合)。ON にした後に実行した場合は、
          走査後に届いた評価を取りこぼしうるため、`verify()` で照合する。
        """
        states_before = {}
        scan = self._scan(now)
        for week in sorted(scan.rows_by_week):
            state = self._store.get_state(week)
            states_before[week] = state.mark_seq if state.mark_seq else None
        replaced = 0
        for week in sorted(scan.rows_by_week):
            # ★ request_recompute=False: backfill だけを理由に、保存済みの過去の Metrics
            # を書き換えない。
            ok = self._store.replace_week(
                week,
                scan.rows_by_week[week].values(),
                states_before[week],
                now,
                request_recompute=False,
            )
            if not ok:
                raise RuntimeError(
                    f"backfill 中に週 {week} へ新しい評価が届きました。もう一度実行してください"
                )
            replaced += 1
        self._store.set_backfill_complete(now, scan.week_count, scan.row_count)
        logger.info("weekly aggregate backfill complete weeks=%d rows=%d", replaced, scan.row_count)
        return _plan_of(scan)

    # --- 照合 -------------------------------------------------------------

    def verify(
        self,
        now: dt.datetime,
        weeks: frozenset[str] | None = None,
        *,
        mark_rebuild_required: bool = False,
    ) -> VerifyReport:
        """raw から作った集計と、保存済みの Aggregate を突合する(件数・合計は厳密に一致するはず)。

        不一致の週は、`mark_rebuild_required=True` のとき
          AGGREGATE_REBUILD_REQUIRED(RECONCILE_MISMATCH)
        として登録する(指定週だけ rebuild するため)。
        """
        scan = self._scan(now, only_weeks=weeks)
        target_weeks = set(scan.rows_by_week)
        if weeks is not None:
            target_weeks |= set(weeks)
        mismatches: list[WeekMismatch] = []
        for week in sorted(target_weeks):
            expected = scan.rows_by_week.get(week, {})
            actual = {
                (r.recommendation_type, r.rule_version): r for r in self._store.query_week(week)
            }
            reasons = _compare_rows(expected, actual)
            if reasons:
                mismatches.append(WeekMismatch(week, tuple(reasons)))
        marked: list[str] = []
        if mark_rebuild_required:
            for mismatch in mismatches:
                self._store.mark_rebuild_required(mismatch.review_week, "RECONCILE_MISMATCH", now)
                marked.append(mismatch.review_week)
        return VerifyReport(len(target_weeks), tuple(mismatches), tuple(marked))

    # --- rebuild(指定週だけ) ------------------------------------------------

    def rebuild_weeks(
        self, weeks: frozenset[str], now: dt.datetime, *, execute: bool = False
    ) -> dict[str, int]:
        """指定した週だけを、raw から作り直す(複数週は 1 回の走査にまとめる)。dry-run が既定。

        戻り値は 週 -> 作り直す(作り直した)集計行の数。
        """
        states_before = {}
        for week in weeks:
            state = self._store.get_state(week)
            states_before[week] = state.mark_seq if state.mark_seq else None
        scan = self._scan(now, only_weeks=weeks)
        result = {week: len(scan.rows_by_week.get(week, {})) for week in sorted(weeks)}
        if not execute:
            return result
        for week in sorted(weeks):
            ok = self._store.replace_week(
                week, scan.rows_by_week.get(week, {}).values(), states_before[week], now
            )
            if not ok:
                raise RuntimeError(
                    f"rebuild 中に週 {week} へ新しい評価が届きました。もう一度実行してください"
                )
        return result


def _plan_of(scan: RawScan) -> BackfillPlan:
    weeks = sorted(scan.rows_by_week)
    return BackfillPlan(
        week_count=scan.week_count,
        row_count=scan.row_count,
        matched_evaluations=scan.matched,
        scanned_evaluations=scan.scanned,
        missing_recommendation_count=scan.missing_recommendation_count,
        estimated_write_items=scan.row_count + scan.week_count * _PER_WEEK_FIXED_WRITE_ITEMS,
        first_week=weeks[0] if weeks else None,
        last_week=weeks[-1] if weeks else None,
    )


def _compare_rows(
    expected: dict[tuple[RecommendationType, str], WeeklyEvaluationAggregate],
    actual: dict[tuple[RecommendationType, str], WeeklyEvaluationAggregate],
) -> list[str]:
    reasons: list[str] = []
    for key in sorted(set(expected) | set(actual), key=lambda k: (k[0].value, k[1])):
        label = f"{key[0].value}/{key[1]}"
        want = expected.get(key)
        have = actual.get(key)
        if want is None or have is None:
            reasons.append(
                f"{label}: 片方にしか存在しません"
                f"(raw={want is not None}, aggregate={have is not None})"
            )
            continue
        if _tuple(want) != _tuple(have) or want.label_counts != have.label_counts:
            reasons.append(f"{label}: 件数または合計が一致しません")
    return reasons


def _tuple(row: WeeklyEvaluationAggregate) -> tuple[int, int, int, Decimal, int, Decimal, int]:
    return (
        row.sample_count,
        row.conclusive_count,
        row.success_count,
        row.price_return_sum,
        row.price_return_count,
        row.excess_return_sum,
        row.excess_return_count,
    )
