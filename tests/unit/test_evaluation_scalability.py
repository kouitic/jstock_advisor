"""Issue #113: 定点評価のリソース枯渇に対する構造的な契約を固定するテスト。

本番で定点評価Lambdaが11日以上1度も完走せず、2026-08-31には処理能力が0件/日へ
悪化していた。原因は次の3点の合成であり、それぞれが**再発しないこと**を
機械的に固定する。

1. `exists_for_horizon()`を評価ループ内で呼び、dueな組ごとに
   evaluation_resultsのフルテーブルScanが発生していた(時間の支配項)
2. `list_all()`で全Recommendationをmaterializeし、営業日/暦日で2回走査していた
   (メモリの支配項)
3. 評価1件ごとに株価とTOPIXを取り直していた(外部I/O)

実時間300/900秒を待つテストは書かない(残時間はfake contextで注入する)。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.common import (
    BuyPriceLevels,
    DataSourceReference,
    PriceWithRationale,
)
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EvaluationLabel,
    RecommendationType,
)
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    CompletedHorizonIndex,
)
from jstock_advisor.interfaces.types import PriceBar, PriceHistory, PriceSnapshot
from jstock_advisor.services.recommendation_evaluation_service import (
    RecommendationEvaluationService,
    TimeBudget,
)

_STOCK_CODE = "2914"
_BENCHMARK = "TOPIX"
_SOURCE = DataSourceReference(provider="fake", fetched_at=dt.datetime(2026, 8, 31, tzinfo=dt.UTC))
# JST 2026-08-31 12:00。営業日horizon 1/5 が到来している十分な過去を推奨日にする。
_NOW = dt.datetime(2026, 8, 31, 3, 0, tzinfo=dt.UTC)


@pytest.fixture
def config() -> AppConfig:
    return load_config()


@pytest.fixture
def calendar(config: AppConfig) -> BusinessCalendar:
    return BusinessCalendar.from_config(config.holiday_calendar)


def _bar(date: dt.date, close: str = "2200", *, high: str | None = None) -> PriceBar:
    value = Decimal(close)
    return PriceBar(
        date=date,
        open=value,
        high=Decimal(high) if high is not None else value,
        low=value,
        close=value,
        volume=1000,
    )


def _daily_bars(start: dt.date, days: int, close: str = "2200") -> list[PriceBar]:
    return [_bar(start + dt.timedelta(days=offset), close) for offset in range(days)]


def _make_recommendation(
    recommendation_id: str = "rec-1",
    recommended_at: dt.datetime = dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
    stock_code: str = _STOCK_CODE,
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name="テスト銘柄",
        recommended_at=recommended_at,
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            standard=PriceWithRationale(price=Decimal("2000"), rationale="x"),
        ),
        price_at_recommendation=Decimal("2200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


def _clone_many(
    count: int, prefix: str, recommended_at: dt.datetime, stock_code: str = _STOCK_CODE
) -> list[Recommendation]:
    """大量件数用。1件だけ検証を通し、以降はmodel_copyで複製する(テストの高速化)。"""
    template = _make_recommendation(f"{prefix}-0", recommended_at, stock_code)
    return [
        template.model_copy(update={"recommendation_id": f"{prefix}-{i:05d}"})
        for i in range(count)
    ]


def _seed_evaluated(
    evaluation_repo: _CountingEvaluationRepository,
    recommendation_id: str,
    *,
    business: tuple[int, ...] = (),
    calendar_days: int | None = None,
) -> None:
    """指定の(recommendation, horizon)を「評価済み」として事前投入する。

    `label_evidence="seed"` で印を付け、run中に新規保存された分と区別できるようにする。
    """
    for horizon in business:
        evaluation_repo.saved.append(
            _seed_result(recommendation_id, business_days=horizon)
        )
    if calendar_days is not None:
        evaluation_repo.saved.append(
            _seed_result(recommendation_id, calendar_days=calendar_days)
        )


def _seed_result(
    recommendation_id: str,
    *,
    business_days: int | None = None,
    calendar_days: int | None = None,
) -> EvaluationResult:
    suffix = f"bd{business_days}" if business_days is not None else f"cd{calendar_days}"
    return EvaluationResult(
        evaluation_id=f"seed-{recommendation_id}-{suffix}",
        recommendation_id=recommendation_id,
        horizon_business_days=business_days,
        horizon_calendar_days=calendar_days,
        evaluated_at=_NOW - dt.timedelta(days=1),
        evaluation_date=dt.date(2026, 8, 1),
        price_at_evaluation=Decimal("2200"),
        price_return_pct=0.0,
        evaluation_label=EvaluationLabel.ACCEPTABLE,
        label_evidence="seed",
    )


# --- テストダブル -------------------------------------------------------------


class _CountingRecommendationRepository:
    """Recommendationの走査回数と取得方法を観測できるリポジトリダブル。

    `list_all()`は**意図的に実装しない**。サービスが誤って全件materializeへ
    戻った場合にAttributeErrorで即座に落ちるようにするため。
    """

    def __init__(self, recommendations: list[Recommendation]) -> None:
        self._items = {r.recommendation_id: r for r in recommendations}
        self.iter_all_calls = 0
        self.get_many_calls = 0
        self.fetched_ids: list[str] = []

    def iter_all(self) -> Iterator[Recommendation]:
        self.iter_all_calls += 1
        yield from self._items.values()

    def get_many(self, recommendation_ids: list[str]) -> dict[str, Recommendation]:
        self.get_many_calls += 1
        ids = list(recommendation_ids)
        self.fetched_ids.extend(ids)
        return {i: self._items[i] for i in ids if i in self._items}


class _CountingEvaluationRepository:
    """評価済み索引の読み込み回数と、禁止されたexists_for_*呼び出しを観測する。"""

    def __init__(self) -> None:
        self.saved: list[EvaluationResult] = []
        self.index_load_calls = 0
        self.exists_calls = 0

    def load_completed_horizon_index(self) -> CompletedHorizonIndex:
        self.index_load_calls += 1
        index = CompletedHorizonIndex()
        for evaluation in self.saved:
            if evaluation.horizon_business_days is not None:
                index.record_business_horizon(
                    evaluation.recommendation_id, evaluation.horizon_business_days
                )
            if evaluation.horizon_calendar_days is not None:
                index.record_calendar_horizon(
                    evaluation.recommendation_id, evaluation.horizon_calendar_days
                )
        return index

    def exists_for_horizon(self, recommendation_id: str, horizon_business_days: int) -> bool:
        self.exists_calls += 1
        return False

    def exists_for_calendar_horizon(
        self, recommendation_id: str, horizon_calendar_days: int
    ) -> bool:
        self.exists_calls += 1
        return False

    def save(self, evaluation: EvaluationResult) -> None:
        self.saved.append(evaluation)


class _CountingMarketDataProvider:
    """下位provider呼び出し回数を数える。既定では要求範囲外のバーも返し、
    サービス/キャッシュ側が期間を正しく絞り込んでいるかを検証できるようにする。"""

    def __init__(
        self,
        bars: list[PriceBar] | None = None,
        benchmark_bars: list[PriceBar] | None = None,
        bars_by_stock: dict[str, list[PriceBar]] | None = None,
    ) -> None:
        self._bars = bars if bars is not None else _daily_bars(dt.date(2026, 6, 1), 120)
        self._benchmark_bars = (
            benchmark_bars if benchmark_bars is not None else _daily_bars(dt.date(2026, 6, 1), 120)
        )
        self._bars_by_stock = bars_by_stock or {}
        self.stock_calls: list[tuple[str, dt.date, dt.date]] = []
        self.benchmark_calls: list[tuple[str, dt.date, dt.date]] = []

    def get_latest_price(self, stock_code: str) -> PriceSnapshot | None:
        return None

    def get_average_trading_value(self, stock_code: str, business_days: int) -> Decimal | None:
        return None

    def get_price_history(
        self, stock_code: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        self.stock_calls.append((stock_code, start, end))
        bars = self._bars_by_stock.get(stock_code, self._bars)
        return PriceHistory(symbol=stock_code, bars=list(bars), source=_SOURCE)

    def get_benchmark_price_history(
        self, symbol: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        self.benchmark_calls.append((symbol, start, end))
        return PriceHistory(symbol=symbol, bars=list(self._benchmark_bars), source=_SOURCE)


class _FakeLambdaContext:
    """`get_remaining_time_in_millis()`の戻り値を注入する(実時間を待たない)。"""

    def __init__(self, remaining_values: list[int]) -> None:
        self._values = list(remaining_values)
        self.calls = 0

    def get_remaining_time_in_millis(self) -> int:
        self.calls += 1
        if len(self._values) > 1:
            return self._values.pop(0)
        return self._values[0]


def _build_service(
    config: AppConfig,
    calendar: BusinessCalendar,
    recommendations: list[Recommendation],
    provider: _CountingMarketDataProvider | None = None,
) -> tuple[
    RecommendationEvaluationService,
    _CountingRecommendationRepository,
    _CountingEvaluationRepository,
    _CountingMarketDataProvider,
]:
    recommendation_repo = _CountingRecommendationRepository(recommendations)
    evaluation_repo = _CountingEvaluationRepository()
    market_data = provider or _CountingMarketDataProvider()
    service = RecommendationEvaluationService(
        market_data_provider=market_data,  # type: ignore[arg-type]
        config=config,
        business_calendar=calendar,
        recommendation_repository=recommendation_repo,  # type: ignore[arg-type]
        evaluation_repository=evaluation_repo,  # type: ignore[arg-type]
    )
    return service, recommendation_repo, evaluation_repo, market_data


# --- 原因1: N+1 フルScanの除去 -------------------------------------------------


def test_exists_for_horizon_is_never_called_during_evaluation_run(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    """評価ループ内でexists_for_*(=1回あたりテーブル全件走査)を呼ばないこと。

    これが本番タイムアウトの時間支配項だった。呼び出し回数0をmechanicalに固定する。
    """
    service, _, evaluation_repo, _ = _build_service(
        config, calendar, [_make_recommendation(f"rec-{i}") for i in range(20)]
    )

    service.run_due_evaluations_single_pass(_NOW)

    assert evaluation_repo.exists_calls == 0


def test_completed_horizon_index_is_loaded_exactly_once_per_run(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    """evaluation_resultsの全件走査はrunあたり1回に限られること。"""
    service, _, evaluation_repo, _ = _build_service(
        config, calendar, [_make_recommendation(f"rec-{i}") for i in range(20)]
    )

    service.run_due_evaluations_single_pass(_NOW)

    assert evaluation_repo.index_load_calls == 1


# --- 原因2: streaming と 1パス化 -----------------------------------------------


def test_recommendations_are_streamed_once_for_both_horizon_axes(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    """1 runにつきRecommendationのコレクション走査は1回。

    従来は営業日評価と暦日評価がそれぞれ`list_all()`を呼び、約118MBのテーブルを
    2回フルScanしていた。
    """
    service, recommendation_repo, _, _ = _build_service(
        config, calendar, [_make_recommendation(f"rec-{i}") for i in range(10)]
    )

    outcome = service.run_due_evaluations_single_pass(_NOW)

    assert recommendation_repo.iter_all_calls == 1
    # 1パスでも両軸が評価されている(片方が飢餓状態にならない)
    assert outcome.summary.business_evaluated_count > 0
    assert outcome.summary.calendar_evaluated_count > 0


def test_service_never_materializes_the_whole_recommendation_collection(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    """`list_all()`を持たないリポジトリダブルでも動作すること。

    サービスが全件materializeへ戻った場合、`list_all`が存在しないため
    AttributeErrorで即座に失敗する。
    """
    recommendation_repo = _CountingRecommendationRepository(
        [_make_recommendation(f"rec-{i}") for i in range(5)]
    )
    assert not hasattr(recommendation_repo, "list_all")
    service = RecommendationEvaluationService(
        market_data_provider=_CountingMarketDataProvider(),  # type: ignore[arg-type]
        config=config,
        business_calendar=calendar,
        recommendation_repository=recommendation_repo,  # type: ignore[arg-type]
        evaluation_repository=_CountingEvaluationRepository(),  # type: ignore[arg-type]
    )

    outcome = service.run_due_evaluations_single_pass(_NOW)

    assert outcome.summary.recommendations_scanned == 5


def test_scales_to_six_thousand_recommendations_with_single_scan(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    """本番規模(6,000件)でも走査は1回で、本体を再取得するのは対象分だけであること。

    走査(Phase 1)と評価(Phase 2)を分離し、6,000件を走査しても
    ピークメモリの支配項である「Recommendation本体の保持」が
    dueな分だけに限られることを確認する。
    """
    not_due = _clone_many(5950, "new", _NOW)
    due = _clone_many(50, "old", dt.datetime(2026, 7, 1, tzinfo=dt.UTC))
    service, recommendation_repo, evaluation_repo, _ = _build_service(
        config, calendar, not_due + due
    )

    outcome = service.run_due_evaluations_single_pass(_NOW)

    assert recommendation_repo.iter_all_calls == 1
    assert evaluation_repo.index_load_calls == 1
    assert outcome.summary.recommendations_scanned == 6000
    assert outcome.summary.pending_recommendation_count == 50
    # 6,000件走査しても本体を再取得したのは対象の50件のみ(1 chunkで収まる)
    assert recommendation_repo.get_many_calls == 1
    assert len(recommendation_repo.fetched_ids) == 50
    assert all(rec_id.startswith("old-") for rec_id in recommendation_repo.fetched_ids)


def test_large_pending_backlog_fetches_only_one_chunk_before_budget_stop(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    """大量backlogでもRecommendation本体は必要なchunk分しか取得しないこと。

    Phase 2で全pending分を一度に取得すると、1件約20KBのRecommendationが
    再びメモリを圧迫する。予算切れまでに取得したchunkが1つだけであることを
    確認し、取得量が「pending件数」ではなく「実際に処理した分」に比例することを固定する。
    """
    recommendations = _clone_many(6000, "rec", dt.datetime(2026, 7, 1, tzinfo=dt.UTC))
    service, recommendation_repo, _, _ = _build_service(config, calendar, recommendations)
    context = _FakeLambdaContext([900_000, 900_000, 1_000])

    outcome = service.run_due_evaluations_single_pass(
        _NOW, budget=TimeBudget(source=context, reserve_ms=60_000)
    )

    assert outcome.summary.budget_exhausted is True
    assert outcome.summary.pending_recommendation_count == 6000
    assert recommendation_repo.get_many_calls == 1
    assert len(recommendation_repo.fetched_ids) == 100  # _FETCH_CHUNK_SIZE
    assert outcome.summary.backlog_remaining > 0


def test_handles_empty_collection(config: AppConfig, calendar: BusinessCalendar) -> None:
    service, _, _, market_data = _build_service(config, calendar, [])

    outcome = service.run_due_evaluations_single_pass(_NOW)

    assert outcome.evaluated == []
    assert outcome.summary.recommendations_scanned == 0
    assert outcome.summary.due_count == 0
    assert outcome.summary.pending_count == 0
    assert outcome.summary.backlog_remaining == 0
    assert market_data.stock_calls == []


# --- 原因3: run scope cache ---------------------------------------------------


def test_benchmark_history_is_fetched_once_per_run(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    """TOPIXの取得はrunあたり1回に有界であること(従来は評価1件ごとに取得)。"""
    service, _, _, market_data = _build_service(
        config, calendar, [_make_recommendation(f"rec-{i}") for i in range(15)]
    )

    outcome = service.run_due_evaluations_single_pass(_NOW)

    assert outcome.summary.evaluated_count > 15  # 1推奨あたり複数horizonを評価している
    assert len(market_data.benchmark_calls) == 1
    assert {call[0] for call in market_data.benchmark_calls} == {_BENCHMARK}


def test_same_stock_history_is_fetched_once_per_run(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    """同一銘柄の複数評価で株価履歴の取得回数が有界であること。"""
    recommendations = [
        _make_recommendation(
            f"rec-{i}", recommended_at=dt.datetime(2026, 7, 1, tzinfo=dt.UTC) + dt.timedelta(days=i)
        )
        for i in range(12)
    ]
    service, _, _, market_data = _build_service(config, calendar, recommendations)

    outcome = service.run_due_evaluations_single_pass(_NOW)

    assert outcome.summary.evaluated_count > 12
    # 全推奨が同一銘柄。dueが古い順に処理されるため取得範囲は広がらず1回で済む。
    assert len(market_data.stock_calls) == 1
    assert market_data.stock_calls[0][0] == _STOCK_CODE


def test_distinct_stocks_are_fetched_once_each(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    recommendations = [
        _make_recommendation(f"rec-{i}", stock_code=f"90{i:02d}") for i in range(6)
    ]
    service, _, _, market_data = _build_service(config, calendar, recommendations)

    service.run_due_evaluations_single_pass(_NOW)

    assert len(market_data.stock_calls) == 6
    assert len({call[0] for call in market_data.stock_calls}) == 6


def test_cache_never_uses_bars_after_the_evaluation_date(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    """look-ahead bias防止: 評価基準日より未来のバーを混入させないこと。

    providerには要求範囲外(評価基準日より後)の極端な高値・終値を返させ、
    それが評価結果へ現れないことを確認する。キャッシュは範囲を広げて取得するため、
    sliceが正しく効いていないと未来のバーで評価してしまう。
    """
    recommended_at = dt.datetime(2026, 8, 3, tzinfo=dt.UTC)
    horizon_1_date = calendar.add_business_days(recommended_at.date(), 1)
    bars = [
        _bar(recommended_at.date(), "2200"),
        _bar(horizon_1_date, "2300"),
        # 評価基準日より後。混入すれば price_at_evaluation / max_gain_pct が跳ねる。
        _bar(horizon_1_date + dt.timedelta(days=1), "9999", high="9999"),
        _bar(horizon_1_date + dt.timedelta(days=2), "9999", high="9999"),
    ]
    provider = _CountingMarketDataProvider(bars=bars, benchmark_bars=bars)
    service, _, _, _ = _build_service(
        config,
        calendar,
        [_make_recommendation("rec-1", recommended_at=recommended_at)],
        provider=provider,
    )

    outcome = service.run_due_evaluations_single_pass(_NOW)

    horizon_1 = [r for r in outcome.evaluated if r.horizon_business_days == 1]
    assert len(horizon_1) == 1
    result = horizon_1[0]
    assert result.evaluation_date == horizon_1_date
    assert result.price_at_evaluation == Decimal("2300")
    assert result.max_gain_pct is not None
    assert result.max_gain_pct < 100.0  # 9999(+354%)が混入していない


def test_provider_failure_propagates_and_is_not_cached(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    """取得失敗を「データ無し」へ潰さないこと(Issue #59 Phase B2の契約を維持)。"""

    class _FailingProvider(_CountingMarketDataProvider):
        def get_price_history(
            self, stock_code: str, start: dt.date, end: dt.date
        ) -> PriceHistory | None:
            self.stock_calls.append((stock_code, start, end))
            raise RuntimeError("provider is down")

    provider = _FailingProvider()
    service, _, evaluation_repo, _ = _build_service(
        config, calendar, [_make_recommendation("rec-1")], provider=provider
    )

    with pytest.raises(RuntimeError, match="provider is down"):
        service.run_due_evaluations_single_pass(_NOW)

    assert evaluation_repo.saved == []


# --- 時間予算 -----------------------------------------------------------------


def test_time_budget_stops_before_timeout_and_reports_summary(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    """残時間が尽きたら殺される前に自主的に正常終了し、summaryを返すこと。"""
    recommendations = [
        _make_recommendation(
            f"rec-{i:03d}",
            recommended_at=dt.datetime(2026, 7, 1, tzinfo=dt.UTC) + dt.timedelta(days=i),
        )
        for i in range(20)
    ]
    service, _, _, _ = _build_service(config, calendar, recommendations)
    # chunk判定1回 + Recommendation 3件分は余裕あり、4件目の判定で予算切れにする。
    context = _FakeLambdaContext([900_000, 900_000, 900_000, 900_000, 1_000])

    outcome = service.run_due_evaluations_single_pass(
        _NOW, budget=TimeBudget(source=context, reserve_ms=60_000)
    )

    assert outcome.summary.budget_exhausted is True
    assert outcome.summary.pending_recommendation_count == 20
    assert outcome.summary.pending_count == outcome.summary.due_count
    assert 0 < outcome.summary.backlog_remaining < outcome.summary.pending_count
    assert outcome.summary.evaluated_count > 0
    assert outcome.summary.duration_ms >= 0


def test_next_run_processes_the_remaining_backlog(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    """予算切れで残した分を、次のrunが処理できること(取りこぼさない)。"""
    recommendations = [
        _make_recommendation(
            f"rec-{i:03d}",
            recommended_at=dt.datetime(2026, 7, 1, tzinfo=dt.UTC) + dt.timedelta(days=i),
        )
        for i in range(20)
    ]
    service, _, evaluation_repo, _ = _build_service(config, calendar, recommendations)
    context = _FakeLambdaContext([900_000, 900_000, 900_000, 900_000, 1_000])

    first = service.run_due_evaluations_single_pass(
        _NOW, budget=TimeBudget(source=context, reserve_ms=60_000)
    )
    assert first.summary.budget_exhausted is True
    saved_after_first = len(evaluation_repo.saved)

    second = service.run_due_evaluations_single_pass(_NOW)

    assert second.summary.budget_exhausted is False
    assert second.summary.backlog_remaining == 0
    assert len(evaluation_repo.saved) > saved_after_first
    # 1回目と2回目で同じ(recommendation_id, horizon)を二重保存していない
    keys = [
        (e.recommendation_id, e.horizon_business_days, e.horizon_calendar_days)
        for e in evaluation_repo.saved
    ]
    assert len(keys) == len(set(keys))

    third = service.run_due_evaluations_single_pass(_NOW)
    assert third.summary.evaluated_count == 0


def test_budget_finishes_the_recommendation_it_started(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    """予算判定はRecommendation単位で行い、着手した1件は全horizonを終えること。"""
    recommendations = [
        _make_recommendation(
            f"rec-{i:03d}",
            recommended_at=dt.datetime(2026, 7, 1, tzinfo=dt.UTC) + dt.timedelta(days=i),
        )
        for i in range(10)
    ]
    service, _, evaluation_repo, _ = _build_service(config, calendar, recommendations)
    context = _FakeLambdaContext([900_000, 900_000, 1_000])

    outcome = service.run_due_evaluations_single_pass(
        _NOW, budget=TimeBudget(source=context, reserve_ms=60_000)
    )

    assert outcome.summary.budget_exhausted is True
    evaluated_ids = {e.recommendation_id for e in evaluation_repo.saved}
    assert len(evaluated_ids) == 1
    started = next(iter(evaluated_ids))
    # 着手した1件については、dueだったhorizonがすべて評価されている
    unlimited = service.run_due_evaluations_single_pass(_NOW)
    assert all(e.recommendation_id != started for e in unlimited.evaluated)


def test_no_budget_source_means_unlimited(config: AppConfig, calendar: BusinessCalendar) -> None:
    """CLI・ローカル実行(残時間の概念が無い)では打ち切らないこと。"""
    service, _, _, _ = _build_service(
        config, calendar, [_make_recommendation(f"rec-{i}") for i in range(5)]
    )

    outcome = service.run_due_evaluations_single_pass(_NOW, budget=TimeBudget())

    assert outcome.summary.budget_exhausted is False
    assert outcome.summary.backlog_remaining == 0


# --- backlog recovery ---------------------------------------------------------


def test_backlog_is_processed_oldest_due_first(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    """未評価horizonが同条件なら、古い推奨から順に消化すること。"""
    recommendations = [
        _make_recommendation("rec-new", recommended_at=dt.datetime(2026, 8, 10, tzinfo=dt.UTC)),
        _make_recommendation("rec-old", recommended_at=dt.datetime(2026, 7, 1, tzinfo=dt.UTC)),
        _make_recommendation("rec-mid", recommended_at=dt.datetime(2026, 7, 20, tzinfo=dt.UTC)),
    ]
    service, _, evaluation_repo, _ = _build_service(config, calendar, recommendations)
    context = _FakeLambdaContext([900_000, 900_000, 1_000])

    service.run_due_evaluations_single_pass(
        _NOW, budget=TimeBudget(source=context, reserve_ms=60_000)
    )

    assert {e.recommendation_id for e in evaluation_repo.saved} == {"rec-old"}


def test_order_uses_oldest_pending_horizon_not_oldest_recommendation(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    """**推奨日順ではなく、最も古い未処理horizonを持つ推奨から**処理すること。

    backlog recoveryでは両者が食い違う。

      A: 推奨日はBより古いが、1/5/20日horizonは評価済みで
         未評価は60日horizonのみ(その評価基準日は最近)
      B: 推奨日はAより新しいが、1日horizonが未評価
         (その評価基準日はAの60日horizonより古い)

    先に処理すべきはB。推奨日で並べるとAが先になってしまう。
    """
    a_at = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
    b_at = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    rec_a = _make_recommendation("rec-a", recommended_at=a_at)
    rec_b = _make_recommendation("rec-b", recommended_at=b_at)

    a_pending_date = calendar.add_business_days(a_at.date(), 60)
    b_pending_date = calendar.add_business_days(b_at.date(), 1)
    # 前提: 推奨日は A < B、未処理horizonの評価基準日は B < A(交差している)
    assert a_at < b_at
    assert b_pending_date < a_pending_date

    service, _, evaluation_repo, _ = _build_service(config, calendar, [rec_a, rec_b])
    # Aは60日horizonだけを未評価として残す(暦日7日も評価済みにしておく)
    _seed_evaluated(evaluation_repo, "rec-a", business=(1, 5, 20), calendar_days=7)
    context = _FakeLambdaContext([900_000, 900_000, 1_000])

    service.run_due_evaluations_single_pass(
        _NOW, budget=TimeBudget(source=context, reserve_ms=60_000)
    )

    processed = {e.recommendation_id for e in evaluation_repo.saved if e.label_evidence != "seed"}
    assert processed == {"rec-b"}


def test_order_considers_calendar_horizon_as_oldest_pending(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    """最古の未処理horizonが**暦日軸**の場合も、それが順序に反映されること。

      E: 推奨日は新しいが、未評価は暦日7日horizonのみ(基準日は古い)
      F: 推奨日は古いが、未評価は60日営業日horizonのみ(基準日は新しい)

    先に処理すべきはE。営業日軸だけを見ているとFが先になってしまう。
    """
    e_at = dt.datetime(2026, 8, 5, tzinfo=dt.UTC)
    f_at = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
    rec_e = _make_recommendation("rec-e", recommended_at=e_at)
    rec_f = _make_recommendation("rec-f", recommended_at=f_at)

    e_pending_date = e_at.date() + dt.timedelta(days=7)  # 暦日horizon
    f_pending_date = calendar.add_business_days(f_at.date(), 60)
    assert f_at < e_at
    assert e_pending_date < f_pending_date

    service, _, evaluation_repo, _ = _build_service(config, calendar, [rec_e, rec_f])
    _seed_evaluated(evaluation_repo, "rec-e", business=(1, 5))  # 20日はまだ未到来
    _seed_evaluated(evaluation_repo, "rec-f", business=(1, 5, 20), calendar_days=7)
    context = _FakeLambdaContext([900_000, 900_000, 1_000])

    service.run_due_evaluations_single_pass(
        _NOW, budget=TimeBudget(source=context, reserve_ms=60_000)
    )

    processed = [e for e in evaluation_repo.saved if e.label_evidence != "seed"]
    assert {e.recommendation_id for e in processed} == {"rec-e"}
    assert [e.horizon_calendar_days for e in processed] == [7]


def test_recommendation_is_completed_atomically_within_budget(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    """順序変更後も「着手した1件はdueな全horizonを完了する」契約が維持されること。"""
    rec = _make_recommendation("rec-old", recommended_at=dt.datetime(2026, 7, 1, tzinfo=dt.UTC))
    other = _make_recommendation("rec-new", recommended_at=dt.datetime(2026, 8, 20, tzinfo=dt.UTC))
    service, _, evaluation_repo, _ = _build_service(config, calendar, [rec, other])
    context = _FakeLambdaContext([900_000, 900_000, 1_000])

    outcome = service.run_due_evaluations_single_pass(
        _NOW, budget=TimeBudget(source=context, reserve_ms=60_000)
    )

    assert outcome.summary.budget_exhausted is True
    saved = [e for e in evaluation_repo.saved if e.recommendation_id == "rec-old"]
    # 着手した rec-old は営業日1/5/20 と暦日7 をすべて評価し終えている
    assert sorted(e.horizon_business_days for e in saved if e.horizon_business_days) == [1, 5, 20]
    assert [e.horizon_calendar_days for e in saved if e.horizon_calendar_days] == [7]
    assert all(e.recommendation_id == "rec-old" for e in evaluation_repo.saved)


def test_old_due_horizons_are_not_skipped(config: AppConfig, calendar: BusinessCalendar) -> None:
    """何日遅れても、到来済みhorizonは全て評価対象に残ること。"""
    very_old = _make_recommendation(
        "rec-old", recommended_at=dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
    )
    service, _, evaluation_repo, _ = _build_service(config, calendar, [very_old])

    outcome = service.run_due_evaluations_single_pass(_NOW)

    horizons = sorted(
        e.horizon_business_days
        for e in evaluation_repo.saved
        if e.horizon_business_days is not None
    )
    assert horizons == [1, 5, 20]  # 60/120/250は未到来
    assert outcome.summary.backlog_remaining == 0


# --- 既存semanticsの維持 ------------------------------------------------------


def test_single_pass_matches_separate_axis_runs(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    """1パス実行と、営業日/暦日を別々に実行した場合で結果が一致すること。"""
    recommendations = [_make_recommendation(f"rec-{i}") for i in range(5)]

    combined_service, _, combined_repo, _ = _build_service(config, calendar, recommendations)
    combined_service.run_due_evaluations_single_pass(_NOW)

    split_service, _, split_repo, _ = _build_service(config, calendar, recommendations)
    split_service.run_due_evaluations(_NOW)
    split_service.run_due_calendar_evaluations(_NOW)

    def _keys(saved: list[EvaluationResult]) -> set[tuple[str, int | None, int | None]]:
        return {
            (e.recommendation_id, e.horizon_business_days, e.horizon_calendar_days) for e in saved
        }

    assert _keys(combined_repo.saved) == _keys(split_repo.saved)
    assert len(combined_repo.saved) == len(split_repo.saved)


def test_already_evaluated_horizons_are_not_re_evaluated(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    service, _, evaluation_repo, _ = _build_service(
        config, calendar, [_make_recommendation(f"rec-{i}") for i in range(4)]
    )

    first = service.run_due_evaluations_single_pass(_NOW)
    second = service.run_due_evaluations_single_pass(_NOW)

    assert first.summary.evaluated_count > 0
    assert second.summary.evaluated_count == 0
    assert second.summary.already_evaluated_count == first.summary.due_count
    assert len(evaluation_repo.saved) == first.summary.evaluated_count


def test_summary_counts_are_internally_consistent(
    config: AppConfig, calendar: BusinessCalendar
) -> None:
    service, _, _, _ = _build_service(
        config, calendar, [_make_recommendation(f"rec-{i}") for i in range(8)]
    )

    summary = service.run_due_evaluations_single_pass(_NOW).summary

    # due / already_evaluated / pending / backlog はすべてhorizon単位で整合する
    assert summary.pending_count == summary.due_count - summary.already_evaluated_count
    assert summary.backlog_remaining == summary.pending_count - summary.evaluated_count
    assert summary.evaluated_count == (
        summary.business_evaluated_count + summary.calendar_evaluated_count
    )
    assert summary.skipped_due_to_data_error_count == (
        summary.business_skipped_count + summary.calendar_skipped_count
    )
    # 予算無制限・欠損なしなら pending は evaluated と skipped で説明しきれる
    assert summary.pending_count == (
        summary.evaluated_count + summary.skipped_due_to_data_error_count
    )
    assert summary.missing_recommendation_count == 0
    # Recommendation単位の件数はhorizon単位より必ず小さい(1件が複数horizonを持つため)
    assert 0 < summary.pending_recommendation_count < summary.pending_count
