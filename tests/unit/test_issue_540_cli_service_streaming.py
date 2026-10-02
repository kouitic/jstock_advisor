"""#540: CLI専用サービスの「評価ごとのN+1」と「全件list保持」を解消した後も、結果が変わらないこと。

対象は`PerformanceMetricsService.summarize()`と`BacktestService.run()`(いずれもCLI専用で、
Lambdaからは到達しない)。従来は、評価を`list_all()`で全件読み、評価1件ごとに
`RecommendationRepository.get()`を呼んで(N+1)、評価の件数分のRecommendationを別個の
オブジェクトとして保持していた。ローカルJSONストアの`get()`は呼ぶたびにファイル全体を
読んで復元するため、評価E件 × 推奨R件の復元になる。

修正後は、評価を`iter_all()`で1件ずつ読み、Recommendationは`get_many()`で100件ずつ
一括取得し、集計は既存の`MetricsAccumulator`へ1件ずつ足し込む。★ 出力は変えない。

★ 反証: 修正前の実装(`get()`のN+1・`list_all()`)では、`get()`が評価の件数分呼ばれて
  T2 / T6 が落ちる(実装時にrevertして確認した)。

fixtureは架空値のみ(実在の銘柄・保有は使わない)。
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Iterable, Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EvaluationLabel,
    RecommendationType,
)
from jstock_advisor.domain.entities.evaluation import (
    EVALUATION_SEMANTICS_V1,
    EVALUATION_SEMANTICS_V2,
    EvaluationResult,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.backtest_service import _METRIC_REGISTRY, BacktestService
from jstock_advisor.services.performance_metrics_service import (
    MetricsBucket,
    PerformanceMetricsService,
    PerformanceSummary,
    build_metrics_bucket,
    iter_evaluations_with_recommendations,
)

_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)
_TARGET = "screening.total_yield.min_total_yield_pct"

_TYPES = (RecommendationType.BUY, RecommendationType.WATCH_BUY, RecommendationType.SELL)
_CONFIDENCES = (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM, ConfidenceLevel.LOW)
_LABELS = (
    EvaluationLabel.SUCCESS,
    EvaluationLabel.ACCEPTABLE,
    EvaluationLabel.PRICE_TOO_HIGH,
    EvaluationLabel.DATA_ISSUE,
    EvaluationLabel.INCONCLUSIVE,
)


class _SpyRecommendationRepository(RecommendationRepository):
    """呼び出しの種類と回数を数える(中身は実リポジトリへ委譲)。"""

    def __init__(self, store_dir: Path) -> None:
        super().__init__(store_dir=store_dir)
        self.get_calls = 0
        self.get_many_sizes: list[int] = []
        self.list_all_calls = 0

    def get(self, recommendation_id: str) -> Recommendation | None:
        self.get_calls += 1
        return super().get(recommendation_id)

    def get_many(self, recommendation_ids: Iterable[str]) -> dict[str, Recommendation]:
        ids = list(recommendation_ids)
        self.get_many_sizes.append(len(ids))
        return super().get_many(ids)

    def list_all(self) -> list[Recommendation]:
        self.list_all_calls += 1
        return super().list_all()


class _SpyEvaluationRepository(EvaluationResultRepository):
    def __init__(self, store_dir: Path) -> None:
        super().__init__(store_dir=store_dir)
        self.list_all_calls = 0
        self.iter_all_calls = 0

    def list_all(self) -> list[EvaluationResult]:
        self.list_all_calls += 1
        return super().list_all()

    def iter_all(self) -> Iterator[EvaluationResult]:
        self.iter_all_calls += 1
        return super().iter_all()


def _recommendation(index: int) -> Recommendation:
    return Recommendation(
        recommendation_id=f"r{index:04d}",
        stock_code="0000",
        stock_name="test",
        recommended_at=_NOW,
        recommendation_type=_TYPES[index % len(_TYPES)],
        price_at_recommendation=Decimal("1000"),
        total_yield_pct_at_recommendation=3.0 + (index % 7) * 0.4 if index % 11 else None,
        confidence=_CONFIDENCES[index % len(_CONFIDENCES)],
        rule_version=f"v{index % 4}",
    )


def _evaluation(
    index: int,
    recommendation_id: str,
    *,
    horizon: int = 20,
    semantics: str = EVALUATION_SEMANTICS_V1,
) -> EvaluationResult:
    return EvaluationResult(
        evaluation_id=f"e{index:05d}",
        recommendation_id=recommendation_id,
        horizon_business_days=horizon,
        evaluated_at=_NOW,
        evaluation_date=_NOW.date(),
        price_at_evaluation=Decimal("1100"),
        # 浮動小数点の合計の順序依存を検出できる、桁の揃わない値
        price_return_pct=math.sin(index) * 17.3 + index * 0.0137,
        excess_return_pct=None if index % 5 == 0 else math.cos(index) * 4.1,
        evaluation_label=_LABELS[index % len(_LABELS)],
        label_evidence="test",
        evaluation_semantics_version=semantics,
    )


_RECOMMENDATION_COUNT = 120
_EVALUATION_COUNT = 253  # 100件ずつの一括取得の境界(100 / 200)をまたぐ


@pytest.fixture
def world(tmp_path: Path) -> tuple[_SpyRecommendationRepository, _SpyEvaluationRepository]:
    rec_repo = _SpyRecommendationRepository(tmp_path)
    eval_repo = _SpyEvaluationRepository(tmp_path)
    # save()は1件ごとにファイル全体を書き直すため、まとめて書く(件数が多いので)。
    rec_repo._store.upsert_many(_recommendation(i) for i in range(_RECOMMENDATION_COUNT))
    evaluations = []
    for i in range(_EVALUATION_COUNT):
        # i % 13 == 0 は、対応する推奨が存在しない評価
        ghost = i % 13 == 0
        recommendation_id = f"ghost{i:04d}" if ghost else f"r{i % _RECOMMENDATION_COUNT:04d}"
        horizon = 20 if i % 3 else 5
        semantics = EVALUATION_SEMANTICS_V2 if i % 17 == 0 else EVALUATION_SEMANTICS_V1
        evaluations.append(_evaluation(i, recommendation_id, horizon=horizon, semantics=semantics))
    eval_repo._store.upsert_many(evaluations)
    rec_repo.get_calls = 0
    rec_repo.get_many_sizes.clear()
    rec_repo.list_all_calls = 0
    eval_repo.list_all_calls = 0
    eval_repo.iter_all_calls = 0
    return rec_repo, eval_repo


# ===== 参照実装(修正前の実装を、そのまま写したもの。結果の同一性を比べるための基準) =====


def _reference_summarize(
    eval_repo: EvaluationResultRepository,
    rec_repo: RecommendationRepository,
    horizon: int | None,
    now: dt.datetime,
) -> PerformanceSummary:
    evaluations = eval_repo.list_all()
    if horizon is not None:
        evaluations = [e for e in evaluations if e.horizon_business_days == horizon]
    pairs: list[tuple[EvaluationResult, Recommendation]] = []
    for evaluation in evaluations:
        recommendation = rec_repo.get(evaluation.recommendation_id)
        if recommendation is not None:
            pairs.append((evaluation, recommendation))

    def group(key_fn: Callable[[Recommendation], str]) -> list[MetricsBucket]:
        grouped: dict[str, list[EvaluationResult]] = {}
        for evaluation, recommendation in pairs:
            grouped.setdefault(key_fn(recommendation), []).append(evaluation)
        return [build_metrics_bucket(k, v) for k, v in sorted(grouped.items())]

    return PerformanceSummary(
        generated_at=now,
        horizon_business_days=horizon,
        overall=build_metrics_bucket("overall", evaluations),
        by_recommendation_type=group(lambda r: r.recommendation_type.value),
        by_confidence=group(lambda r: r.confidence.value),
        by_rule_version=group(lambda r: r.rule_version),
    )


# ===== PerformanceMetricsService.summarize() =====


@pytest.mark.parametrize("horizon", [None, 20, 5, 999])
def test_t1_summarize_is_identical_to_the_previous_implementation(
    world: tuple[_SpyRecommendationRepository, _SpyEvaluationRepository], horizon: int | None
) -> None:
    rec_repo, eval_repo = world
    service = PerformanceMetricsService(
        evaluation_repository=eval_repo, recommendation_repository=rec_repo
    )
    actual = service.summarize(horizon_business_days=horizon, now=_NOW)
    expected = _reference_summarize(eval_repo, rec_repo, horizon, _NOW)
    # 浮動小数点の値も含め完全に一致する(MetricsAccumulatorはbuild_metrics_bucketとbit単位で同じ)。
    assert actual == expected


def test_t2_summarize_has_no_per_evaluation_get_and_uses_batched_get_many(
    world: tuple[_SpyRecommendationRepository, _SpyEvaluationRepository],
) -> None:
    """★ 反証: 修正前は`get()`が評価の件数分呼ばれ、`list_all()`が使われる。"""
    rec_repo, eval_repo = world
    service = PerformanceMetricsService(
        evaluation_repository=eval_repo, recommendation_repository=rec_repo
    )
    service.summarize(now=_NOW)
    assert rec_repo.get_calls == 0
    assert rec_repo.list_all_calls == 0
    assert eval_repo.list_all_calls == 0
    assert eval_repo.iter_all_calls == 1
    assert sum(rec_repo.get_many_sizes) <= _EVALUATION_COUNT
    assert len(rec_repo.get_many_sizes) == math.ceil(_EVALUATION_COUNT / 100)
    assert all(size <= 100 for size in rec_repo.get_many_sizes)


def test_t4_evaluation_without_a_recommendation_counts_overall_but_not_the_groups(
    tmp_path: Path,
) -> None:
    rec_repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    rec_repo.save(_recommendation(1))
    eval_repo.save(_evaluation(1, "r0001"))
    eval_repo.save(_evaluation(2, "missing"))
    summary = PerformanceMetricsService(eval_repo, rec_repo).summarize(now=_NOW)
    assert summary.overall.count == 2
    assert sum(b.count for b in summary.by_recommendation_type) == 1
    assert sum(b.count for b in summary.by_confidence) == 1
    assert sum(b.count for b in summary.by_rule_version) == 1


def test_summarize_with_no_evaluations_returns_empty_buckets(tmp_path: Path) -> None:
    rec_repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    summary = PerformanceMetricsService(eval_repo, rec_repo).summarize(now=_NOW)
    assert summary == _reference_summarize(eval_repo, rec_repo, None, _NOW)
    assert summary.overall.count == 0
    assert summary.by_recommendation_type == []


# ===== iter_evaluations_with_recommendations()(一括取得の部品) =====


def test_t3_helper_reads_lazily_and_does_not_hold_all_evaluations(
    world: tuple[_SpyRecommendationRepository, _SpyEvaluationRepository],
) -> None:
    """最初の1件を受け取った時点で、先読みしているのは1バッチ分だけである(全件ではない)。"""
    rec_repo, eval_repo = world
    pulled = 0

    def source() -> Iterator[EvaluationResult]:
        nonlocal pulled
        for evaluation in eval_repo.iter_all():
            pulled += 1
            yield evaluation

    iterator = iter_evaluations_with_recommendations(source(), rec_repo, batch_size=50)
    next(iterator)
    assert pulled == 50
    assert len(rec_repo.get_many_sizes) == 1
    assert rec_repo.get_many_sizes[0] <= 50


@pytest.mark.parametrize("batch_size", [1, 3, 100, 1000])
def test_helper_preserves_order_and_completeness(
    world: tuple[_SpyRecommendationRepository, _SpyEvaluationRepository], batch_size: int
) -> None:
    rec_repo, eval_repo = world
    expected = [e.evaluation_id for e in eval_repo.list_all()]
    results = list(
        iter_evaluations_with_recommendations(eval_repo.iter_all(), rec_repo, batch_size=batch_size)
    )
    assert [e.evaluation_id for e, _ in results] == expected
    for evaluation, recommendation in results:
        if evaluation.recommendation_id.startswith("ghost"):
            assert recommendation is None
        else:
            assert recommendation is not None
            assert recommendation.recommendation_id == evaluation.recommendation_id


# ===== BacktestService.run() =====


def _reference_backtest(
    eval_repo: EvaluationResultRepository,
    rec_repo: RecommendationRepository,
    current_value: float,
    proposed_value: float,
    semantics: str,
) -> tuple[int, int, MetricsBucket, MetricsBucket, list[str]]:
    """修正前のrun()(supported=Trueの経路)を写したもの。"""
    spec = _METRIC_REGISTRY[_TARGET]
    pairs = []
    for evaluation in eval_repo.list_all():
        if evaluation.evaluation_semantics_version != semantics:
            continue
        recommendation = rec_repo.get(evaluation.recommendation_id)
        if recommendation is None or recommendation.recommendation_type not in (
            spec.applicable_types
        ):
            continue
        if getattr(recommendation, spec.attribute) is None:
            continue
        pairs.append((evaluation, recommendation))
    retained_ids: set[str] = set()
    proposed_evals = []
    for evaluation, recommendation in pairs:
        value = getattr(recommendation, spec.attribute)
        if BacktestService._passes(spec, value, proposed_value):
            retained_ids.add(recommendation.recommendation_id)
            proposed_evals.append(evaluation)
    excluded_ids = [
        r.recommendation_id for _, r in pairs if r.recommendation_id not in retained_ids
    ]
    return (
        len(pairs),
        len(proposed_evals),
        build_metrics_bucket("current", [e for e, _ in pairs]),
        build_metrics_bucket("proposed", proposed_evals),
        excluded_ids,
    )


@pytest.mark.parametrize("semantics", [EVALUATION_SEMANTICS_V1, EVALUATION_SEMANTICS_V2])
@pytest.mark.parametrize("proposed_value", [3.2, 4.0, 5.5, 99.0])
def test_t5_backtest_is_identical_to_the_previous_implementation(
    world: tuple[_SpyRecommendationRepository, _SpyEvaluationRepository],
    proposed_value: float,
    semantics: str,
) -> None:
    rec_repo, eval_repo = world
    result = BacktestService(
        recommendation_repository=rec_repo, evaluation_repository=eval_repo
    ).run(_TARGET, 3.0, proposed_value, evaluation_semantics_version=semantics)
    count, proposed_count, current, proposed, excluded = _reference_backtest(
        eval_repo, rec_repo, 3.0, proposed_value, semantics
    )
    assert result.supported is True
    assert result.evaluation_count_current == count
    assert result.evaluation_count_proposed == proposed_count
    assert result.current_performance == current
    assert result.proposed_performance == proposed
    assert result.excluded_recommendation_ids == excluded
    assert result.evaluation_semantics_version == semantics


def test_t6_backtest_has_no_per_evaluation_get_and_uses_batched_get_many(
    world: tuple[_SpyRecommendationRepository, _SpyEvaluationRepository],
) -> None:
    """★ 反証: 修正前は`get()`が評価の件数分呼ばれ、`list_all()`が使われる。"""
    rec_repo, eval_repo = world
    BacktestService(recommendation_repository=rec_repo, evaluation_repository=eval_repo).run(
        _TARGET, 3.0, 4.0
    )
    assert rec_repo.get_calls == 0
    assert eval_repo.list_all_calls == 0
    assert eval_repo.iter_all_calls == 1
    assert all(size <= 100 for size in rec_repo.get_many_sizes)


def test_backtest_with_no_passing_recommendation_has_an_empty_proposed_bucket(
    world: tuple[_SpyRecommendationRepository, _SpyEvaluationRepository],
) -> None:
    rec_repo, eval_repo = world
    result = BacktestService(
        recommendation_repository=rec_repo, evaluation_repository=eval_repo
    ).run(_TARGET, 3.0, 99.0)
    assert result.supported is True
    assert result.evaluation_count_proposed == 0
    assert result.proposed_performance is not None
    assert result.proposed_performance.count == 0
    assert result.proposed_performance == build_metrics_bucket("proposed", [])
    assert result.excluded_recommendation_ids is not None
    assert len(result.excluded_recommendation_ids) == result.evaluation_count_current


def test_t7_unsupported_branches_are_unchanged(
    world: tuple[_SpyRecommendationRepository, _SpyEvaluationRepository], tmp_path: Path
) -> None:
    rec_repo, eval_repo = world
    service = BacktestService(recommendation_repository=rec_repo, evaluation_repository=eval_repo)
    unknown = service.run("unknown.target", 1.0, 2.0)
    assert unknown.supported is False
    assert "バックテスト未対応" in (unknown.reason_unsupported or "")
    loosening = service.run(_TARGET, 4.0, 3.0)
    assert loosening.supported is False
    assert "緩める" in (loosening.reason_unsupported or "")

    empty_rec = RecommendationRepository(store_dir=tmp_path / "empty")
    empty_eval = EvaluationResultRepository(store_dir=tmp_path / "empty")
    no_data = BacktestService(
        recommendation_repository=empty_rec, evaluation_repository=empty_eval
    ).run(_TARGET, 3.0, 4.0, evaluation_semantics_version=EVALUATION_SEMANTICS_V2)
    assert no_data.supported is False
    assert "データ不足" in (no_data.reason_unsupported or "")
    assert no_data.evaluation_semantics_version == EVALUATION_SEMANTICS_V2
