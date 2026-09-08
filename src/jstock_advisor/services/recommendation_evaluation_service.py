"""推奨の定点評価サービス(要求仕様29〜36節)。

推奨(Recommendation)ごとに、config/schedule.yamlのevaluation_horizons_business_daysで
定義された営業日数が経過した時点の株価実績を計測し、EvaluationResultとして保存する。
未経過のホライズンはスキップし、既に評価済みのホライズンは再評価しない。

振り返り機能改修(週次改善レビュー)で、営業日ベースとは別軸のJST暦日ベース
ホライズン(既定7暦日、run_due_calendar_evaluations)を追加した。EvaluationResultの
horizon_business_days/horizon_calendar_daysはどちらか一方のみが設定される。

配当・手数料等を含む正確な総合リターン(total_return_pct)の算出には、期間中の
配当受取実績の追跡が必要となるため、MVPでは株価ベースのリターンのみを算出する
(total_return_amount/total_return_pctはNoneのまま。要求仕様12節「推測で補完しない」
原則により、根拠のない値は入れない)。

## Issue #113: リソース枯渇の解消(2026-08-31)

本番で定点評価Lambdaが毎回タイムアウトし、2026-08-31時点で処理能力が0件/日へ
悪化していた。原因は単一ではなく次の3点の合成であったため、3点すべてを是正する。

1. **時間の支配項**: `exists_for_horizon()`を評価ループの内側で呼んでおり、
   dueな(recommendation, horizon)の組ごとにevaluation_resultsのフルテーブルScanが
   発生していた(1実行あたり最大9,663回 = 約270万RCU)。
   → run開始時に`load_completed_horizon_index()`で**1回だけ**索引を作る。
2. **メモリの支配項**: `list_all()`で全Recommendation(本番5,943件・約118MB)を
   materializeし、しかも営業日/暦日で2回走査していた(pydantic保持で約527MBとなり
   Lambdaの512MBを超える)。
   → `iter_all()`によるストリーミングと、**1 runにつきRecommendation 1パス**へ変更。
3. **外部I/O**: 評価1件ごとに株価とTOPIXを取り直していた。
   → `RunScopedMarketDataCache`でrunスコープのメモ化を行う
   (look-ahead biasを作らないことは同モジュールのdocstringを参照)。

加えて、Lambdaのタイムアウトをbacklog処理の打ち切り機構として使わないよう、
`TimeBudget`により**残時間に余裕を残して自主的に正常終了**し、
必ずrun summaryを出力する。backlogは「古い評価をskipせず」
「dueが古いものから順に」消化する。
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.evaluation_rules import determine_evaluation_label
from jstock_advisor.domain.jst import evaluation_date_jst, require_timezone_aware, to_jst
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    CompletedHorizonIndex,
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.interfaces.market_data import MarketDataProvider
from jstock_advisor.interfaces.types import PriceBar, PriceHistory
from jstock_advisor.services.run_scoped_market_data import RunScopedMarketDataCache

logger = logging.getLogger(__name__)

DEFAULT_BENCHMARK_SYMBOL = "TOPIX"
# 振り返り機能改修(週次改善レビュー)で使うJST暦日ベースの既定ホライズン。
# config/review_improvement.yamlのevaluation_horizon_daysと一致させること。
_CALENDAR_HORIZON_DAYS = 7

# Issue #113: 対象Recommendationの再取得はBatchGetItem(最大100件/リクエスト)で行う。
# 1件約20KBあるため、まとめて取り過ぎるとピークメモリが跳ねる。
_FETCH_CHUNK_SIZE = 100

# Issue #113: 進捗ログを出す間隔(Recommendation件数)。本番でSTART〜REPORT間の
# アプリログが0行だったため「どこまで進んだか」を追跡できなかった。
_PROGRESS_LOG_INTERVAL = 500

# Issue #113: 残時間がこれを下回ったら新しいRecommendationの評価を始めない。
# 1件のRecommendationは最大6horizon × (株価 + ベンチマーク)の取得を行うため、
# 「開始した1件を必ず最後まで終える」ための余裕として十分な幅を取る。
DEFAULT_BUDGET_RESERVE_MS = 60_000


class RemainingTimeSource(Protocol):
    """残実行時間(ミリ秒)を返すもの。AWS Lambdaのcontextがそのまま適合する。"""

    def get_remaining_time_in_millis(self) -> int: ...


@dataclass(frozen=True)
class TimeBudget:
    """Lambdaのタイムアウトで殺される前に自主的に切り上げるための時間予算。

    `source`がNone(CLI・テスト等)の場合は無制限として扱う。
    **タイムアウトそのものをbacklog処理の打ち切り機構として使わない**ことが目的で、
    予算切れ時は処理中のRecommendationを最後まで終えてから正常終了する。
    """

    source: RemainingTimeSource | None = None
    reserve_ms: int = DEFAULT_BUDGET_RESERVE_MS

    def exhausted(self) -> bool:
        if self.source is None:
            return False
        return self.source.get_remaining_time_in_millis() <= self.reserve_ms


@dataclass(frozen=True)
class EvaluationRunSummary:
    """run summary(Issue #113の可観測性要件)。

    「一度も成功していない」ことに11日以上気づけなかったため、
    部分実行でも必ずこのsummaryを出力できるようにする。
    """

    # due_count / already_evaluated_count / pending_count / backlog_remaining は
    # すべて**(recommendation, horizon)の組**を単位とする。
    # pending_recommendation_count だけがRecommendation件数を単位とする。
    due_count: int = 0
    already_evaluated_count: int = 0
    pending_count: int = 0
    pending_recommendation_count: int = 0
    evaluated_count: int = 0
    skipped_due_to_data_error_count: int = 0
    business_evaluated_count: int = 0
    calendar_evaluated_count: int = 0
    business_skipped_count: int = 0
    calendar_skipped_count: int = 0
    backlog_remaining: int = 0
    budget_exhausted: bool = False
    recommendations_scanned: int = 0
    missing_recommendation_count: int = 0
    provider_call_count: int = 0
    duration_ms: int = 0


@dataclass(frozen=True)
class EvaluationRunOutcome:
    evaluated: list[EvaluationResult] = field(default_factory=list)
    skipped_due_to_data_error: list[tuple[str, int, str]] = field(default_factory=list)
    # Issue #113で追加。既存の呼び出し側(CLI・テスト)は上2つだけを参照するため、
    # 既定値付きで後方互換を保つ。
    summary: EvaluationRunSummary = field(default_factory=EvaluationRunSummary)


@dataclass
class _RunAccumulator:
    """run中に積み上げる結果。軸(営業日/暦日)ごとの内訳も保持する。"""

    evaluated: list[EvaluationResult] = field(default_factory=list)
    skipped: list[tuple[str, int, str]] = field(default_factory=list)
    business_evaluated: int = 0
    calendar_evaluated: int = 0
    business_skipped: int = 0
    calendar_skipped: int = 0


@dataclass(frozen=True)
class _PendingWork:
    """1 Recommendationについて「dueかつ未評価」のホライズンだけを保持する軽量な作業単位。

    Recommendation本体(1件約20KB)は保持しない。本番6,000件規模でも
    この一覧のメモリは数MB以下に収まる。
    """

    recommendation_id: str
    recommended_at: dt.datetime
    # (horizon, evaluation_date)。評価日はPhase 1で算出済みの値をそのまま使い、
    # Phase 2で再計算しない(BusinessCalendar.add_business_days()は
    # count日ぶんの逐日走査であり、再計算はそのままCPUコストになる)。
    business_horizons: tuple[tuple[int, dt.date], ...]
    calendar_horizon: tuple[int, dt.date] | None

    @property
    def oldest_pending_evaluation_date(self) -> dt.date:
        """このRecommendationが持つ**未評価**horizonの中で最も古い評価基準日。

        backlogの消化順に使う。`recommended_at`(推奨日)で並べると
        「古い推奨から」にはなるが「**最も古い未処理horizonから**」にはならない。
        両者は backlog recovery 中に食い違う:

          推奨A: 推奨日は古いが 1/5/20日horizonは評価済みで、
                 未評価は60日horizonのみ(その評価基準日は最近)
          推奨B: 推奨日は新しいが 1日horizonが未評価
                 (その評価基準日はAの60日horizonより古い)

        この場合に先に処理すべきはBである。営業日軸と暦日軸の**両方**を
        対象に最小値を取る(暦日horizonだけが未評価というケースがあるため)。

        `_PendingWork`は「dueかつ未評価のhorizonが1つ以上ある」場合にのみ
        生成されるため、候補は必ず1つ以上ある。
        """
        dates = [evaluation_date for _, evaluation_date in self.business_horizons]
        if self.calendar_horizon is not None:
            dates.append(self.calendar_horizon[1])
        return min(dates)


def _latest_bar_on_or_before(bars: list[PriceBar], target: dt.date) -> PriceBar | None:
    candidates = [b for b in bars if b.date <= target]
    if not candidates:
        return None
    return max(candidates, key=lambda b: b.date)


def _return_pct(base: Decimal, current: Decimal) -> float:
    return float((current - base) / base * 100)


def _chunked(values: Sequence[_PendingWork], size: int) -> Iterator[Sequence[_PendingWork]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


class RecommendationEvaluationService:
    def __init__(
        self,
        market_data_provider: MarketDataProvider,
        config: AppConfig,
        business_calendar: BusinessCalendar,
        recommendation_repository: RecommendationRepository | None = None,
        evaluation_repository: EvaluationResultRepository | None = None,
        benchmark_symbol: str = DEFAULT_BENCHMARK_SYMBOL,
    ) -> None:
        self._market_data = market_data_provider
        self._config = config
        self._calendar = business_calendar
        self._recommendations = recommendation_repository or RecommendationRepository()
        self._evaluations = evaluation_repository or EvaluationResultRepository()
        self._benchmark_symbol = benchmark_symbol

    # --- 公開エントリポイント --------------------------------------------

    def run_due_evaluations(
        self, now: dt.datetime, budget: TimeBudget | None = None
    ) -> EvaluationRunOutcome:
        """営業日ベースホライズンのみを評価する(既存API・CLI互換)。

        Lambdaからは`run_due_evaluations_single_pass()`を使うこと
        (営業日と暦日でRecommendationを2回走査しないため)。
        """
        return self._run(
            now,
            include_business=True,
            calendar_horizon_days=None,
            budget=budget or TimeBudget(),
        )

    def run_due_calendar_evaluations(
        self,
        now: dt.datetime,
        horizon_days: int = _CALENDAR_HORIZON_DAYS,
        budget: TimeBudget | None = None,
    ) -> EvaluationRunOutcome:
        """振り返り機能改修: JST暦日ベースの定点評価(既定7暦日後)を実行する。

        既存の営業日ベース評価(run_due_evaluations)とは別軸のホライズンであり、
        週次改善レビュー(weekly_improvement_review_service)の分析対象データを
        作る目的で全RecommendationTypeを対象に実行する。JST境界バグ(now.date()を
        UTC-aware datetimeへ直接呼ぶと深夜0時〜9時の間に前日扱いされる不具合、
        決算日修正で確立済みの原則)を避けるため、domain.jstのto_jst/
        evaluation_date_jst経由でのみ暦日を扱う。
        """
        return self._run(
            now,
            include_business=False,
            calendar_horizon_days=horizon_days,
            budget=budget or TimeBudget(),
        )

    def run_due_evaluations_single_pass(
        self,
        now: dt.datetime,
        calendar_horizon_days: int = _CALENDAR_HORIZON_DAYS,
        budget: TimeBudget | None = None,
    ) -> EvaluationRunOutcome:
        """営業日ホライズンと暦日ホライズンを**Recommendationの1パス**で処理する。

        Issue #113: 従来はハンドラが`run_due_evaluations()`と
        `run_due_calendar_evaluations()`を順に呼び、1回のLambda実行で
        `jstock-recommendations`(約118MB)を**2回**フルScanしていた。
        さらに暦日評価が後段にあったため、前段のコスト増大に伴って
        暦日評価へ到達しなくなり、2026-08-13以降1件も生成されていなかった
        (12.4節の週次改善レビューが入力ゼロで空回りしていた直接原因)。

        本メソッドは1回の走査で両軸を収集するため、どちらか一方だけが
        飢餓状態になることが構造的に起こらない。
        """
        return self._run(
            now,
            include_business=True,
            calendar_horizon_days=calendar_horizon_days,
            budget=budget or TimeBudget(),
        )

    # --- 実行本体 ----------------------------------------------------------

    def _run(
        self,
        now: dt.datetime,
        *,
        include_business: bool,
        calendar_horizon_days: int | None,
        budget: TimeBudget,
    ) -> EvaluationRunOutcome:
        require_timezone_aware(now)
        started_at = time.monotonic()
        today_jst = evaluation_date_jst(now)

        # Phase 0: 評価済み索引をrun開始時に1回だけ構築する(Issue #113 原因1)。
        index = self._evaluations.load_completed_horizon_index()

        # Phase 1: Recommendationを1回だけストリーミング走査し、
        # 「dueかつ未評価」の軽量な作業一覧を作る(本体は保持しない。原因2)。
        pending, scanned, due_count, already_evaluated = self._collect_pending_work(
            today_jst,
            index,
            include_business=include_business,
            calendar_horizon_days=calendar_horizon_days,
        )
        # backlogは「最も古い未処理horizonを持つRecommendation」から消化する
        # (古い評価をskipしない)。**推奨日順ではない**——推奨日が古くても
        # 未評価horizonの評価基準日が新しいことがあり、両者は
        # backlog recovery中に食い違う(_PendingWork.oldest_pending_evaluation_date参照)。
        # 同着は recommended_at → recommendation_id で決定的に解決する。
        pending.sort(
            key=lambda work: (
                work.oldest_pending_evaluation_date,
                work.recommended_at,
                work.recommendation_id,
            )
        )

        logger.info(
            "evaluation scan done: scanned=%d due_horizons=%d already_evaluated=%d "
            "pending_horizons=%d pending_recommendations=%d elapsed_ms=%d",
            scanned,
            due_count,
            already_evaluated,
            due_count - already_evaluated,
            len(pending),
            int((time.monotonic() - started_at) * 1000),
        )

        # Phase 2: 予算の許す範囲で古い順に評価する。
        market_data = RunScopedMarketDataCache(self._market_data, upper_bound=today_jst)
        acc = _RunAccumulator()
        missing = 0
        processed = 0
        budget_exhausted = False

        for chunk in _chunked(pending, _FETCH_CHUNK_SIZE):
            if budget.exhausted():
                budget_exhausted = True
                break
            recommendations = self._recommendations.get_many(
                [work.recommendation_id for work in chunk]
            )
            for work in chunk:
                # 予算判定はRecommendation単位で行う。開始した1件は
                # 全horizonを終えてから次の判定へ進む(中途半端な状態で切らない)。
                if budget.exhausted():
                    budget_exhausted = True
                    break
                recommendation = recommendations.get(work.recommendation_id)
                if recommendation is None:
                    # Phase 1とPhase 2の間に削除された等。取得失敗と区別して記録する。
                    missing += 1
                    continue
                self._evaluate_pending_work(work, recommendation, now, index, market_data, acc)
                processed += 1
                if processed % _PROGRESS_LOG_INTERVAL == 0:
                    logger.info(
                        "evaluation progress: processed=%d/%d evaluated=%d skipped=%d "
                        "provider_calls=%d elapsed_ms=%d",
                        processed,
                        len(pending),
                        len(acc.evaluated),
                        len(acc.skipped),
                        market_data.provider_call_count,
                        int((time.monotonic() - started_at) * 1000),
                    )
            if budget_exhausted:
                break

        summary = EvaluationRunSummary(
            due_count=due_count,
            already_evaluated_count=already_evaluated,
            pending_count=due_count - already_evaluated,
            pending_recommendation_count=len(pending),
            evaluated_count=len(acc.evaluated),
            skipped_due_to_data_error_count=len(acc.skipped),
            business_evaluated_count=acc.business_evaluated,
            calendar_evaluated_count=acc.calendar_evaluated,
            business_skipped_count=acc.business_skipped,
            calendar_skipped_count=acc.calendar_skipped,
            backlog_remaining=(due_count - already_evaluated) - len(acc.evaluated),
            budget_exhausted=budget_exhausted,
            recommendations_scanned=scanned,
            missing_recommendation_count=missing,
            provider_call_count=market_data.provider_call_count,
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        return EvaluationRunOutcome(
            evaluated=acc.evaluated, skipped_due_to_data_error=acc.skipped, summary=summary
        )

    def _collect_pending_work(
        self,
        today_jst: dt.date,
        index: CompletedHorizonIndex,
        *,
        include_business: bool,
        calendar_horizon_days: int | None,
    ) -> tuple[list[_PendingWork], int, int, int]:
        """Recommendationを1回ストリーミング走査し、dueかつ未評価の作業一覧を作る。

        戻り値: (pending, scanned, due_count, already_evaluated_count)
        """
        pending: list[_PendingWork] = []
        scanned = 0
        due_count = 0
        already_evaluated = 0

        for recommendation in self._recommendations.iter_all():
            scanned += 1
            business_horizons: list[tuple[int, dt.date]] = []
            calendar_horizon: tuple[int, dt.date] | None = None

            if include_business:
                for horizon, evaluation_date in self._due_business_horizons(
                    recommendation, today_jst
                ):
                    due_count += 1
                    if index.has_business_horizon(recommendation.recommendation_id, horizon):
                        already_evaluated += 1
                    else:
                        business_horizons.append((horizon, evaluation_date))

            if calendar_horizon_days is not None:
                target = self._calendar_evaluation_date(recommendation, calendar_horizon_days)
                if target <= today_jst:
                    due_count += 1
                    if index.has_calendar_horizon(
                        recommendation.recommendation_id, calendar_horizon_days
                    ):
                        already_evaluated += 1
                    else:
                        calendar_horizon = (calendar_horizon_days, target)

            if business_horizons or calendar_horizon is not None:
                pending.append(
                    _PendingWork(
                        recommendation_id=recommendation.recommendation_id,
                        recommended_at=recommendation.recommended_at,
                        business_horizons=tuple(business_horizons),
                        calendar_horizon=calendar_horizon,
                    )
                )
            # ここでrecommendationへの参照を捨てる(ページ単位で解放させるため、
            # Recommendation本体をpendingへ持ち越さない)。

        return pending, scanned, due_count, already_evaluated

    def _evaluate_pending_work(
        self,
        work: _PendingWork,
        recommendation: Recommendation,
        now: dt.datetime,
        index: CompletedHorizonIndex,
        market_data: RunScopedMarketDataCache,
        acc: _RunAccumulator,
    ) -> None:
        # start_date(評価期間の起点)は従来どおりUTC暦日のまま【意図的に変更しない】。
        # 「営業日評価はUTC暦日、暦日評価はJST暦日」という呼び出し側基準が
        # 文書化された既存設計であり、ここをJST化するとhorizon評価日・
        # max_gain/max_drawdown・ベンチマークwindowが変わる別仕様変更になる。
        start_date = recommendation.recommended_at.date()
        for horizon, evaluation_date in work.business_horizons:
            result = self._evaluate_one(
                recommendation,
                start_date,
                evaluation_date,
                now,
                market_data,
                horizon_business_days=horizon,
            )
            if result is None:
                acc.skipped.append(
                    (
                        recommendation.stock_code,
                        horizon,
                        "評価時点の株価データが取得できませんでした",
                    )
                )
                acc.business_skipped += 1
                continue
            self._evaluations.save(result)
            index.record_business_horizon(recommendation.recommendation_id, horizon)
            acc.evaluated.append(result)
            acc.business_evaluated += 1

        if work.calendar_horizon is None:
            return
        horizon_days, target_evaluation_date = work.calendar_horizon
        recommendation_date_jst = to_jst(recommendation.recommended_at).date()
        result = self._evaluate_one(
            recommendation,
            recommendation_date_jst,
            target_evaluation_date,
            now,
            market_data,
            horizon_calendar_days=horizon_days,
        )
        if result is None:
            acc.skipped.append(
                (
                    recommendation.stock_code,
                    horizon_days,
                    "評価時点の株価データが取得できませんでした",
                )
            )
            acc.calendar_skipped += 1
            return
        self._evaluations.save(result)
        index.record_calendar_horizon(recommendation.recommendation_id, horizon_days)
        acc.evaluated.append(result)
        acc.calendar_evaluated += 1

    # --- due判定 -----------------------------------------------------------

    def _horizons_for(self, recommendation_type: RecommendationType) -> list[int]:
        horizons_cfg = self._config.schedule.evaluation_horizons_business_days
        specific = horizons_cfg.get(recommendation_type.value, [])
        common = horizons_cfg.get("all_types_common", [])
        return sorted(set(specific) | set(common))

    def _due_business_horizons(
        self, recommendation: Recommendation, today_jst: dt.date
    ) -> list[tuple[int, dt.date]]:
        """到来済みの営業日horizonと、その評価基準日を返す。

        Issue #23(2026-08-28): 「評価を実施してよい日に達したか」の当日判定は
        JST暦日(JST calendar date)で行う。UTC暦日(now.date())だとJST 00:00〜
        08:59の実行(reconciler等の再実行)で前日扱いとなり、本来当日実施すべき
        評価が1日遅延する。

        Issue #113: `add_business_days()`はcount日ぶんの逐日走査であるため、
        horizonごとに起点から計算し直すとhorizon集合{1,5,20,60,120,250}で
        1推奨あたり約640日ぶんの走査になる(本番6,000件では数百万回)。
        (1) 直前のhorizonからの**差分だけ**進める
        (2) 未到来のhorizonが1つ見つかったら**打ち切る**
        の2点で削減する。(2)は`add_business_days()`がcountについて
        単調非減少であることに基づく(より長いhorizonの評価日が
        より早くなることはない)。
        """
        start_date = recommendation.recommended_at.date()
        due: list[tuple[int, dt.date]] = []
        cursor_date = start_date
        cursor_horizon = 0
        for horizon in self._horizons_for(recommendation.recommendation_type):
            cursor_date = self._calendar.add_business_days(cursor_date, horizon - cursor_horizon)
            cursor_horizon = horizon
            if cursor_date > today_jst:
                break
            due.append((horizon, cursor_date))
        return due

    @staticmethod
    def _calendar_evaluation_date(recommendation: Recommendation, horizon_days: int) -> dt.date:
        recommendation_date_jst = to_jst(recommendation.recommended_at).date()
        return recommendation_date_jst + dt.timedelta(days=horizon_days)

    # --- 評価1件 -----------------------------------------------------------

    def _evaluate_one(
        self,
        recommendation: Recommendation,
        evaluation_start_date: dt.date,
        evaluation_date: dt.date,
        now: dt.datetime,
        market_data: RunScopedMarketDataCache,
        *,
        horizon_business_days: int | None = None,
        horizon_calendar_days: int | None = None,
    ) -> EvaluationResult | None:
        # evaluation_start_dateは呼び出し側が計算基準(営業日評価はUTC暦日、暦日評価は
        # JST暦日)に応じて算出済みの値をそのまま渡す。ここでrecommended_atから
        # 独自に日付を導出しない(呼び出し側ごとに基準が異なるタイムゾーンバグを防ぐ)。
        start = evaluation_start_date
        history = market_data.get_price_history(
            recommendation.stock_code, start, evaluation_date
        )
        evaluation_bar = (
            None if history is None else _latest_bar_on_or_before(history.bars, evaluation_date)
        )
        if history is None or evaluation_bar is None:
            return None

        base_price = recommendation.price_at_recommendation
        price_at_evaluation = evaluation_bar.close
        price_return_pct = _return_pct(base_price, price_at_evaluation)

        period_bars = [b for b in history.bars if start <= b.date <= evaluation_date]
        max_gain_pct = (
            max(_return_pct(base_price, b.high) for b in period_bars) if period_bars else None
        )
        max_drawdown_pct = (
            min(_return_pct(base_price, b.low) for b in period_bars) if period_bars else None
        )

        reached_tentative, reached_standard, reached_aggressive, business_days_to_reach = (
            self._compute_buy_price_reach(recommendation, period_bars, start)
        )

        buy_price_based_return_pct = None
        if recommendation.buy_prices is not None and recommendation.buy_prices.standard is not None:
            buy_price_based_return_pct = _return_pct(
                recommendation.buy_prices.standard.price, price_at_evaluation
            )

        benchmark_return_pct, excess_return_pct = self._compute_benchmark_returns(
            start, evaluation_date, price_return_pct, market_data
        )

        label, evidence = determine_evaluation_label(
            recommendation.recommendation_type,
            price_return_pct,
            excess_return_pct,
            max_drawdown_pct,
            self._config.evaluation,
        )

        return EvaluationResult(
            evaluation_id=str(uuid.uuid4()),
            recommendation_id=recommendation.recommendation_id,
            horizon_business_days=horizon_business_days,
            horizon_calendar_days=horizon_calendar_days,
            evaluated_at=now,
            evaluation_date=evaluation_date,
            price_at_evaluation=price_at_evaluation,
            price_return_pct=price_return_pct,
            buy_price_based_return_pct=buy_price_based_return_pct,
            max_gain_pct=max_gain_pct,
            max_drawdown_pct=max_drawdown_pct,
            reached_tentative_buy_price=reached_tentative,
            reached_standard_buy_price=reached_standard,
            reached_aggressive_buy_price=reached_aggressive,
            business_days_to_reach_price=business_days_to_reach,
            benchmark_symbol=self._benchmark_symbol if benchmark_return_pct is not None else None,
            benchmark_return_pct=benchmark_return_pct,
            excess_return_pct=excess_return_pct,
            evaluation_label=label,
            label_evidence=evidence,
        )

    def _compute_buy_price_reach(
        self, recommendation: Recommendation, period_bars: list[PriceBar], start: dt.date
    ) -> tuple[bool | None, bool | None, bool | None, int | None]:
        buy_prices = recommendation.buy_prices
        if buy_prices is None or not period_bars:
            return None, None, None, None

        sorted_bars = sorted(period_bars, key=lambda b: b.date)

        def _reached(price: Decimal | None) -> bool | None:
            if price is None:
                return None
            return any(b.low <= price for b in sorted_bars)

        def _first_reach_business_days(price: Decimal | None) -> int | None:
            if price is None:
                return None
            for bar in sorted_bars:
                if bar.low <= price:
                    return self._calendar.business_days_between(start, bar.date)
            return None

        standard_price = buy_prices.standard.price if buy_prices.standard else None
        return (
            _reached(buy_prices.entry.price if buy_prices.entry else None),
            _reached(standard_price),
            _reached(buy_prices.strong.price if buy_prices.strong else None),
            _first_reach_business_days(standard_price),
        )

    def _compute_benchmark_returns(
        self,
        start: dt.date,
        evaluation_date: dt.date,
        price_return_pct: float,
        market_data: RunScopedMarketDataCache,
    ) -> tuple[float | None, float | None]:
        benchmark_history: PriceHistory | None = market_data.get_benchmark_price_history(
            self._benchmark_symbol, start, evaluation_date
        )
        if benchmark_history is None:
            return None, None
        start_bar = _latest_bar_on_or_before(benchmark_history.bars, start)
        end_bar = _latest_bar_on_or_before(benchmark_history.bars, evaluation_date)
        if start_bar is None or end_bar is None:
            return None, None
        benchmark_return_pct = _return_pct(start_bar.close, end_bar.close)
        return benchmark_return_pct, price_return_pct - benchmark_return_pct
