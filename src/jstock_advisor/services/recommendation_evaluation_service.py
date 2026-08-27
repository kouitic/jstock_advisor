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
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.evaluation_rules import determine_evaluation_label
from jstock_advisor.domain.jst import evaluation_date_jst, require_timezone_aware, to_jst
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.interfaces.market_data import MarketDataProvider
from jstock_advisor.interfaces.types import PriceBar

DEFAULT_BENCHMARK_SYMBOL = "TOPIX"
# 振り返り機能改修(週次改善レビュー)で使うJST暦日ベースの既定ホライズン。
# config/review_improvement.yamlのevaluation_horizon_daysと一致させること。
_CALENDAR_HORIZON_DAYS = 7


@dataclass(frozen=True)
class EvaluationRunOutcome:
    evaluated: list[EvaluationResult] = field(default_factory=list)
    skipped_due_to_data_error: list[tuple[str, int, str]] = field(default_factory=list)


def _latest_bar_on_or_before(bars: list[PriceBar], target: dt.date) -> PriceBar | None:
    candidates = [b for b in bars if b.date <= target]
    if not candidates:
        return None
    return max(candidates, key=lambda b: b.date)


def _return_pct(base: Decimal, current: Decimal) -> float:
    return float((current - base) / base * 100)


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

    def run_due_evaluations(self, now: dt.datetime) -> EvaluationRunOutcome:
        outcome = EvaluationRunOutcome()
        for recommendation in self._recommendations.list_all():
            self._evaluate_due_horizons(recommendation, now, outcome)
        return outcome

    def run_due_calendar_evaluations(
        self, now: dt.datetime, horizon_days: int = _CALENDAR_HORIZON_DAYS
    ) -> EvaluationRunOutcome:
        """振り返り機能改修: JST暦日ベースの定点評価(既定7暦日後)を実行する。

        既存の営業日ベース評価(run_due_evaluations)とは別軸のホライズンであり、
        週次改善レビュー(weekly_improvement_review_service)の分析対象データを
        作る目的で全RecommendationTypeを対象に実行する。JST境界バグ(now.date()を
        UTC-aware datetimeへ直接呼ぶと深夜0時〜9時の間に前日扱いされる不具合、
        決算日修正で確立済みの原則)を避けるため、domain.jstのto_jst/
        evaluation_date_jst経由でのみ暦日を扱う。
        """
        require_timezone_aware(now)
        today_jst = evaluation_date_jst(now)
        outcome = EvaluationRunOutcome()
        for recommendation in self._recommendations.list_all():
            self._evaluate_due_calendar_horizon(
                recommendation, horizon_days, now, today_jst, outcome
            )
        return outcome

    def _horizons_for(self, recommendation_type: RecommendationType) -> list[int]:
        horizons_cfg = self._config.schedule.evaluation_horizons_business_days
        specific = horizons_cfg.get(recommendation_type.value, [])
        common = horizons_cfg.get("all_types_common", [])
        return sorted(set(specific) | set(common))

    def _evaluate_due_horizons(
        self, recommendation: Recommendation, now: dt.datetime, outcome: EvaluationRunOutcome
    ) -> None:
        # Issue #23(2026-08-28): 「評価を実施してよい日に達したか」の当日判定は
        # JST暦日(JST calendar date)で行う。UTC暦日(now.date())だとJST 00:00〜
        # 08:59の実行(reconciler等の再実行)で前日扱いとなり、本来当日実施すべき
        # 評価が1日遅延する(run_due_calendar_evaluations()側は同一不具合を
        # docstring明記の上で修正済みで、営業日ベース側だけが未修正だった)。
        today = evaluation_date_jst(now)
        # start_date(評価期間の起点)は従来どおりUTC暦日のまま【意図的に変更しない】。
        # _evaluate_one()のコメントのとおり「営業日評価はUTC暦日、暦日評価はJST
        # 暦日」という呼び出し側基準が文書化された既存設計であり、ここをJST化
        # するとhorizon評価日・max_gain/max_drawdown/ベンチマークwindowが変わる
        # 別仕様変更になるため、Issue #23のスコープ外とする(今回修正するのは
        # 実施可否判定(today)の基準のみ)。
        start_date = recommendation.recommended_at.date()
        for horizon in self._horizons_for(recommendation.recommendation_type):
            evaluation_date = self._calendar.add_business_days(start_date, horizon)
            if evaluation_date > today:
                continue
            if self._evaluations.exists_for_horizon(recommendation.recommendation_id, horizon):
                continue

            result = self._evaluate_one(
                recommendation, start_date, evaluation_date, now, horizon_business_days=horizon
            )
            if result is None:
                outcome.skipped_due_to_data_error.append(
                    (
                        recommendation.stock_code,
                        horizon,
                        "評価時点の株価データが取得できませんでした",
                    )
                )
                continue
            self._evaluations.save(result)
            outcome.evaluated.append(result)

    def _evaluate_due_calendar_horizon(
        self,
        recommendation: Recommendation,
        horizon_days: int,
        now: dt.datetime,
        today_jst: dt.date,
        outcome: EvaluationRunOutcome,
    ) -> None:
        recommendation_date_jst = to_jst(recommendation.recommended_at).date()
        target_evaluation_date = recommendation_date_jst + dt.timedelta(days=horizon_days)
        if target_evaluation_date > today_jst:
            return
        if self._evaluations.exists_for_calendar_horizon(
            recommendation.recommendation_id, horizon_days
        ):
            return

        result = self._evaluate_one(
            recommendation,
            recommendation_date_jst,
            target_evaluation_date,
            now,
            horizon_calendar_days=horizon_days,
        )
        if result is None:
            outcome.skipped_due_to_data_error.append(
                (
                    recommendation.stock_code,
                    horizon_days,
                    "評価時点の株価データが取得できませんでした",
                )
            )
            return
        self._evaluations.save(result)
        outcome.evaluated.append(result)

    def _evaluate_one(
        self,
        recommendation: Recommendation,
        evaluation_start_date: dt.date,
        evaluation_date: dt.date,
        now: dt.datetime,
        *,
        horizon_business_days: int | None = None,
        horizon_calendar_days: int | None = None,
    ) -> EvaluationResult | None:
        # evaluation_start_dateは呼び出し側が計算基準(営業日評価はUTC暦日、暦日評価は
        # JST暦日)に応じて算出済みの値をそのまま渡す。ここでrecommended_atから
        # 独自に日付を導出しない(呼び出し側ごとに基準が異なるタイムゾーンバグを防ぐ)。
        start = evaluation_start_date
        history = self._market_data.get_price_history(
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
            start, evaluation_date, price_return_pct
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
        self, start: dt.date, evaluation_date: dt.date, price_return_pct: float
    ) -> tuple[float | None, float | None]:
        benchmark_history = self._market_data.get_benchmark_price_history(
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
