"""Issue #377 Track1: 週次改善レビューが、評価・Recommendationを全件保持せずに集計すること。

2026-09-21 19:00 JSTの自然実行は、scanまでは成功したが(scanned=53,906 / matched=14,256)、その後の
処理でRuntime.OutOfMemory(Max Memory Used = 512MB / 512MB)になった。原因は、
`_join_recommendations()`が`joined`へ(evaluation, recommendation)の組を全件追加して
Recommendationを保持し続けたこと(取得はchunkに区切っていたが、保持は該当件数に比例していた)。
ここでは次を確認する。

    1 集計器`MetricsAccumulator`が、`build_metrics_bucket()`と**bit単位で同じ**値を返す
      (成功率の分母から除外するラベル・None・空・大量データ・floatの合計の順序を含む)。
    2 chunk境界(99 / 100 / 101 / 199 / 200 / 201件)で、件数・get_many()の呼び出し・集計が正しい。
    3 **保持が有界**である: 同時に生きているRecommendation・評価の数が、chunkの大きさで抑えられ、
      14,256件以上でもピークメモリが該当件数に比例しない(全件をjoinedへ保持する実装へ戻すと落ちる)。
    4 週×(推奨種別, rule_version)の集計、missingの扱い、組の挿入順・候補の検出が、旧方式(評価の
      リストを作って結合・集計する方式)と同じになる。
"""

from __future__ import annotations

import datetime as dt
import random
import tracemalloc
import weakref
from collections.abc import Iterable, Iterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import EvaluationLabel, RecommendationType
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.services import weekly_improvement_review_service as module
from jstock_advisor.services.performance_metrics_service import (
    MetricsAccumulator,
    build_metrics_bucket,
)
from jstock_advisor.services.weekly_improvement_review_service import (
    WeeklyImprovementReviewService,
)

_CHUNK = module._RECOMMENDATION_JOIN_CHUNK_SIZE
_LABELS = list(EvaluationLabel)


# --- 1 集計器 = build_metrics_bucket() -------------------------------------------------------


def _stub(label: EvaluationLabel, price: float, excess: float | None) -> Any:
    """build_metrics_bucket() と MetricsAccumulator が読む 3 項目だけを持つ評価の代役。"""
    return SimpleNamespace(
        evaluation_label=label, price_return_pct=price, excess_return_pct=excess
    )


def _random_stubs(rnd: random.Random, n: int) -> list[Any]:
    scales = [1.0, 1e-9, 1e9, 123456.789]
    return [
        _stub(
            rnd.choice(_LABELS),
            rnd.uniform(-50, 50) * rnd.choice(scales),
            None if rnd.random() < 0.3 else rnd.uniform(-30, 30) * rnd.choice(scales),
        )
        for _ in range(n)
    ]


def _accumulate(evaluations: list[Any]) -> MetricsAccumulator:
    accumulator = MetricsAccumulator()
    for evaluation in evaluations:
        accumulator.add(evaluation)
    return accumulator


@pytest.mark.parametrize("n", [0, 1, 2, 5, 99, 100, 101, 1000, 10_000])
def test_accumulator_matches_build_metrics_bucket_bit_for_bit(n: int) -> None:
    """同じ入力を同じ順序で足すと、`build_metrics_bucket()` と全項目が完全一致する(`==`)。
    floatの合計は、Python 3.12の`sum()`と同じ補償付き加算で累積している(通常の`+=`では、
    最後の桁が食い違いうる)。乱数を変えて繰り返す。
    """
    for seed in range(20):
        evaluations = _random_stubs(random.Random(seed * 1000 + n), n)
        assert _accumulate(evaluations).to_bucket("k") == build_metrics_bucket("k", evaluations)


def test_accumulator_excludes_data_issue_and_inconclusive_from_the_success_denominator() -> None:
    evaluations = [
        _stub(EvaluationLabel.SUCCESS, 1.0, 1.0),
        _stub(EvaluationLabel.ACCEPTABLE, 2.0, None),
        _stub(EvaluationLabel.PRICE_TOO_HIGH, -1.0, -1.0),
        _stub(EvaluationLabel.DATA_ISSUE, 0.0, 0.0),
        _stub(EvaluationLabel.INCONCLUSIVE, 0.0, 5.0),
    ]

    bucket = _accumulate(evaluations).to_bucket("k")

    assert bucket == build_metrics_bucket("k", evaluations)
    assert (bucket.count, bucket.conclusive_count) == (5, 3)  # DATA_ISSUE・INCONCLUSIVE は除外
    assert bucket.success_rate_pct == pytest.approx(2 / 3 * 100)  # SUCCESS・ACCEPTABLE = 成功
    # excess が None の1件(ACCEPTABLE)だけを除いた4件の平均(分母は成功率と別)
    assert bucket.avg_excess_return_pct == pytest.approx((1.0 - 1.0 + 0.0 + 5.0) / 4)


def test_accumulator_none_semantics_match_when_denominators_are_zero() -> None:
    """分母が 0 のとき(空・全件が除外ラベル・excess が全て None)の None の扱いが同じ。"""
    cases = [
        [],
        [
            _stub(EvaluationLabel.DATA_ISSUE, 1.0, None),
            _stub(EvaluationLabel.INCONCLUSIVE, 2.0, None),
        ],
        [_stub(EvaluationLabel.SUCCESS, 1.0, None), _stub(EvaluationLabel.SUCCESS, 3.0, None)],
    ]
    for evaluations in cases:
        assert _accumulate(evaluations).to_bucket("k") == build_metrics_bucket("k", evaluations)
    empty = _accumulate([]).to_bucket("k")
    assert (empty.success_rate_pct, empty.avg_price_return_pct, empty.avg_excess_return_pct) == (
        None,
        None,
        None,
    )


def test_a_naive_running_sum_differs_from_sum_so_the_accumulator_compensates() -> None:
    """検査の意味: 単純な `+=` の累積は、`sum()` と最後の桁が食い違いうる(だから補償が要る)。
    食い違う入力を実際に探し、集計器はそれでも `sum()` と一致することを確認する。
    """
    rnd = random.Random(7)
    found = False
    for _ in range(200):
        values = [rnd.uniform(-1e9, 1e9) * rnd.choice([1.0, 1e-8]) for _ in range(500)]
        naive = 0.0
        for v in values:
            naive += v
        if naive != sum(values):
            found = True
            evaluations = [_stub(EvaluationLabel.SUCCESS, v, None) for v in values]
            assert _accumulate(evaluations).to_bucket("k") == build_metrics_bucket("k", evaluations)
            break
    assert found, "単純な累積と sum() が食い違う入力が見つからない(検査が空振りしている)"


# --- 2・3 chunk境界と、保持の有界化(実サービスの _aggregate_windows を、代役の repository で通す)


class _Tracker:
    """同時に生きているRecommendation・評価の数と、get_many()の呼び出しを数える。"""

    def __init__(self) -> None:
        self.alive_recs = 0
        self.peak_recs = 0
        self.alive_evals = 0
        self.peak_evals = 0
        self.get_many_sizes: list[int] = []
        self.payload_bytes = 0  # Recommendationに持たせる大きさ(保持の検出を確実にするため)

    def new_rec(self, recommendation_type: RecommendationType, rule_version: str) -> Any:
        rec = _TrackedRecommendation(self, recommendation_type, rule_version)
        self.alive_recs += 1
        self.peak_recs = max(self.peak_recs, self.alive_recs)
        return rec

    def watch_eval(self, evaluation: EvaluationResult) -> None:
        self.alive_evals += 1
        self.peak_evals = max(self.peak_evals, self.alive_evals)
        weakref.finalize(evaluation, self._eval_gone)

    def _eval_gone(self) -> None:
        self.alive_evals -= 1


class _TrackedRecommendation:
    def __init__(
        self, tracker: _Tracker, recommendation_type: RecommendationType, rule_version: str
    ) -> None:
        self._tracker = tracker
        self.recommendation_type = recommendation_type
        self.rule_version = rule_version
        self.payload = bytearray(tracker.payload_bytes)  # tracemalloc が数える実体

    def __del__(self) -> None:
        self._tracker.alive_recs -= 1


class _FakeEvaluations:
    """評価を、走査のたびに**その場で作って**返す(リストで持たない)。iter_all()が
    保持しない前提で、サービス側の保持だけを測るため。"""

    def __init__(self, tracker: _Tracker, specs: list[tuple]) -> None:
        self._tracker = tracker
        self._specs = specs

    def iter_all(self) -> Iterator[EvaluationResult]:
        for spec in self._specs:
            evaluation = _evaluation(*spec)
            self._tracker.watch_eval(evaluation)
            yield evaluation


class _FakeRecommendations:
    def __init__(
        self, tracker: _Tracker, kinds: dict[str, tuple[RecommendationType, str]]
    ) -> None:
        self._tracker = tracker
        self._kinds = kinds  # recommendation_id -> (種別, rule_version)。無いIDは「欠落」

    def get_many(self, recommendation_ids: Iterable[str]) -> dict[str, Any]:
        ids = list(recommendation_ids)
        self._tracker.get_many_sizes.append(len(ids))
        found: dict[str, Any] = {}
        for rec_id in dict.fromkeys(ids):
            kind = self._kinds.get(rec_id)
            if kind is not None:
                found[rec_id] = self._tracker.new_rec(*kind)
        return found


def _evaluation(
    index: int, evaluation_date: dt.date, rec_id: str, label: EvaluationLabel
) -> EvaluationResult:
    at = dt.datetime.combine(evaluation_date, dt.time(12), tzinfo=dt.UTC)
    return EvaluationResult(
        evaluation_id=f"e{index}",
        recommendation_id=rec_id,
        horizon_calendar_days=7,
        evaluated_at=at,
        evaluation_date=evaluation_date,
        price_at_evaluation=Decimal("1010"),
        price_return_pct=(index % 17) - 8.5,
        excess_return_pct=None if index % 5 == 0 else (index % 11) - 5.25,
        evaluation_label=label,
        label_evidence="x",
    )


_WINDOWS = [
    (
        f"W{i}",
        dt.date(2026, 8, 3) + dt.timedelta(days=7 * i),
        dt.date(2026, 8, 9) + dt.timedelta(days=7 * i),
    )
    for i in range(5)
]


def _service(tracker: _Tracker, specs: list[tuple], kinds: dict) -> Any:
    """`_aggregate_windows()` が使う属性だけを持つサービス(他の repository は不要)。"""
    service = object.__new__(WeeklyImprovementReviewService)
    service._review_config = load_config().review_improvement
    service._evaluations = _FakeEvaluations(tracker, specs)  # type: ignore[assignment]
    service._recommendations = _FakeRecommendations(tracker, kinds)  # type: ignore[assignment]
    return service


def _generate(
    count: int, *, windows: int = 1, missing_every: int = 0
) -> tuple[list[tuple], dict[str, tuple[RecommendationType, str]]]:
    """countの評価の仕様(`_evaluation()`の引数)を、windows個の週へ順に振り分けて作る。
    kindsは recommendation_id -> (種別, rule_version)。missing_every件ごとに1件、欠落させる。
    """
    kinds: dict[str, tuple[RecommendationType, str]] = {}
    specs: list[tuple] = []
    types = [RecommendationType.BUY, RecommendationType.SELL, RecommendationType.HOLD]
    for i in range(count):
        rec_id = f"rec{i}"
        if not (missing_every and i % missing_every == 0):
            kinds[rec_id] = (types[i % 3], f"v{i % 2}")
        label = _LABELS[i % len(_LABELS)]
        window_date = _WINDOWS[i % windows][1] + dt.timedelta(days=i % 7)
        specs.append((i, window_date, rec_id, label))
    return specs, kinds


@pytest.mark.parametrize("count", [0, 1, 99, 100, 101, 199, 200, 201, 301])
def test_chunk_boundaries_counts_and_get_many_calls(count: int) -> None:
    """99 / 100 / 101 / 199 / 200 / 201 件などの境界で、件数・結合・get_many()が正しい。"""
    tracker = _Tracker()
    specs, kinds = _generate(count, missing_every=7)
    service = _service(tracker, specs, kinds)

    aggregates = service._aggregate_windows(_WINDOWS[:1])
    aggregate = aggregates["W0"]

    missing = sum(1 for i in range(count) if i % 7 == 0)
    assert aggregate.matched == count
    assert aggregate.joined == count - missing
    assert len(aggregate.missing_ids) == missing
    assert sum(a.count for a in aggregate.groups.values()) == aggregate.joined
    expected_calls = -(-count // _CHUNK)  # ceil
    assert len(tracker.get_many_sizes) == expected_calls
    assert all(size <= _CHUNK for size in tracker.get_many_sizes)
    assert sum(tracker.get_many_sizes) == count
    assert tracker.alive_recs == 0  # 終了後、Recommendationは1件も残っていない


def test_get_many_is_called_per_window_and_never_exceeds_the_chunk_size() -> None:
    """5つの週へ交互に振り分けても、週ごとに chunk が溜まり、呼び出しは 1 回あたり chunk 以下。"""
    tracker = _Tracker()
    specs, kinds = _generate(1234, windows=5)
    service = _service(tracker, specs, kinds)

    aggregates = service._aggregate_windows(_WINDOWS)

    assert sum(a.matched for a in aggregates.values()) == 1234
    assert all(size <= _CHUNK for size in tracker.get_many_sizes)
    per_window = [a.matched for a in aggregates.values()]
    assert len(tracker.get_many_sizes) == sum(-(-n // _CHUNK) for n in per_window)


def test_recommendations_and_evaluations_alive_at_once_are_bounded_by_the_chunk() -> None:
    """★ 保持の有界化の直接の確認: 同時に生きているRecommendationは chunk 件以下、評価は
    「週の数 × chunk + 走査中の 1 件」以下。全件を`joined`へ保持する旧実装へ戻すと、
    ここが該当件数に比例して落ちる(500 件でも chunk を超える)。
    """
    tracker = _Tracker()
    specs, kinds = _generate(2500, windows=5)
    service = _service(tracker, specs, kinds)

    service._aggregate_windows(_WINDOWS)

    assert tracker.peak_recs <= _CHUNK
    assert tracker.peak_evals <= len(_WINDOWS) * _CHUNK + 2
    assert tracker.alive_recs == 0
    # 全件(2500)を持つ実装なら、どちらも 2500 に達する(検査が空振りしていない)
    assert tracker.peak_recs > 0 and tracker.peak_evals > _CHUNK


def test_peak_traced_memory_does_not_grow_with_the_number_of_matched_evaluations() -> None:
    """★ 本番と同じ規模(該当 14,256 件以上)でも、ピークメモリが該当件数に比例しない。
    各Recommendationに 20KB の実体を持たせる(実測 1 件あたり約 26KB の JSON)。全件を保持すると
    14,256 × 20KB ≒ 285MB になるが、有界なら chunk 分(約 2MB)+ 集計に収まる。
    """
    tracker = _Tracker()
    tracker.payload_bytes = 20_000
    specs, kinds = _generate(14_256, windows=5)
    service = _service(tracker, specs, kinds)

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        base, _ = tracemalloc.get_traced_memory()
        aggregates = service._aggregate_windows(_WINDOWS)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert sum(a.matched for a in aggregates.values()) == 14_256
    growth_mb = (peak - base) / 1024 / 1024
    # 該当件数の全件を保持する実装は ≒ 285MB(Recommendation)+ 評価。有界なら数 MB。
    assert growth_mb < 30, f"ピークメモリが {growth_mb:.1f}MB(全件保持の疑い)"


# --- 4 旧方式との同値性(週×種別×rule_version、missing、組の挿入順)----------------------------


def test_aggregation_equals_the_list_based_oracle_for_every_week_group_and_missing_id() -> None:
    """旧方式(週ごとに評価のリストを作り、結合して、リストから集計する)を再現したoracleと、
    週×(種別, rule_version)の集計(全項目)・missing_ids・件数・組の挿入順が一致する。
    """
    tracker = _Tracker()
    specs, kinds = _generate(2222, windows=5, missing_every=9)
    evaluations = [_evaluation(*spec) for spec in specs]  # oracle 用(実サービスとは別物)
    service = _service(tracker, specs, kinds)

    aggregates = service._aggregate_windows(_WINDOWS)

    for label, start, end in _WINDOWS:
        in_window = [e for e in evaluations if start <= e.evaluation_date <= end]
        joined: list[tuple[EvaluationResult, tuple[RecommendationType, str]]] = []
        missing: list[str] = []
        for e in in_window:
            kind = kinds.get(e.recommendation_id)
            if kind is None:
                missing.append(e.recommendation_id)
            else:
                joined.append((e, kind))
        groups: dict[tuple[RecommendationType, str], list[EvaluationResult]] = {}
        for e, kind in joined:
            groups.setdefault(kind, []).append(e)

        aggregate = aggregates[label]
        assert aggregate.matched == len(in_window)
        assert aggregate.joined == len(joined)
        assert aggregate.missing_ids == missing
        assert list(aggregate.groups) == list(groups)  # 組の挿入順(= 最初に現れた順)
        for key, evals in groups.items():
            assert aggregate.groups[key].to_bucket(key[0].value) == build_metrics_bucket(
                key[0].value, evals
            )


def test_missing_ids_are_kept_once_per_evaluation_even_when_the_id_repeats() -> None:
    """同じ recommendation_id が欠落している評価が複数あっても、評価ごとに 1 件ずつ残す。
    欠落した評価は、結合の件数にも集計にも入らない。
    """
    tracker = _Tracker()
    day = _WINDOWS[0][1]
    specs = [
        (0, day, "missing-x", EvaluationLabel.SUCCESS),
        (1, day, "missing-x", EvaluationLabel.SUCCESS),
        (2, day, "rec2", EvaluationLabel.SUCCESS),
        (3, day, "missing-y", EvaluationLabel.SUCCESS),
    ]
    service = _service(tracker, specs, {"rec2": (RecommendationType.BUY, "v1")})

    aggregate = service._aggregate_windows(_WINDOWS[:1])["W0"]

    assert aggregate.missing_ids == ["missing-x", "missing-x", "missing-y"]
    assert (aggregate.matched, aggregate.joined) == (4, 1)
    ((_key, accumulator),) = aggregate.groups.items()
    assert accumulator.count == 1


def test_only_the_target_horizon_and_windows_are_aggregated() -> None:
    tracker = _Tracker()
    specs, kinds = _generate(60, windows=2)
    # 別ホライズン(14日)の評価は、対象外。ここでは spec でなく実サービスの fake を差し替える
    service = _service(tracker, specs, kinds)
    original = service._evaluations.iter_all

    def with_noise() -> Iterator[EvaluationResult]:
        yield from original()
        wrong = _evaluation(999, _WINDOWS[0][1], "rec0", EvaluationLabel.SUCCESS)
        yield wrong.model_copy(update={"horizon_calendar_days": 14})
        yield _evaluation(998, dt.date(2020, 1, 1), "rec0", EvaluationLabel.SUCCESS)

    service._evaluations.iter_all = with_noise

    aggregates = service._aggregate_windows(_WINDOWS[:2])

    assert sum(a.matched for a in aggregates.values()) == 60  # 別ホライズンと範囲外は捨てる
