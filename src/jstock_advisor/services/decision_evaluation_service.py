"""判定精度向上機能Phase A: DecisionSnapshot専用の営業日評価。

config.decision_evaluation.horizons_business_days(5/20/60/120/250営業日、既存の
RecommendationType別ホライズンとは完全に別軸)にもとづき、DecisionSnapshotごとに
株価実績を計測しEvaluationResultへ記録する。

価格取得・max_gain/drawdown・買値到達フラグ・ベンチマーク超過リターンの計算は
RecommendationEvaluationService._evaluate_one()をそのまま「計算カーネル」として
再利用し、recommendation_evaluation_service.py自体は一切変更しない(既存の営業日/
暦日ホライズン評価ロジック・冪等性ロジックへの回帰リスクを避けるため)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.interfaces.market_data import MarketDataProvider
from jstock_advisor.services.recommendation_evaluation_service import (
    DEFAULT_BENCHMARK_SYMBOL,
    EvaluationRunOutcome,
    RecommendationEvaluationService,
)


class DecisionEvaluationService:
    def __init__(
        self,
        market_data_provider: MarketDataProvider,
        config: AppConfig,
        business_calendar: BusinessCalendar,
        decision_repository: DecisionSnapshotRepository | None = None,
        recommendation_repository: RecommendationRepository | None = None,
        evaluation_repository: EvaluationResultRepository | None = None,
        benchmark_symbol: str = DEFAULT_BENCHMARK_SYMBOL,
    ) -> None:
        self._config = config
        self._calendar = business_calendar
        self._decisions = decision_repository or DecisionSnapshotRepository()
        self._recommendations = recommendation_repository or RecommendationRepository()
        self._evaluations = evaluation_repository or EvaluationResultRepository()
        # _evaluate_one()を計算カーネルとして再利用するための内部インスタンス
        # (recommendation_evaluation_service.py自体は変更しない)。
        self._recommendation_eval = RecommendationEvaluationService(
            market_data_provider,
            config,
            business_calendar,
            self._recommendations,
            self._evaluations,
            benchmark_symbol,
        )

    def run_due_decision_evaluations(self, now: dt.datetime) -> EvaluationRunOutcome:
        outcome = EvaluationRunOutcome()
        horizons = self._config.decision_evaluation.horizons_business_days
        today = now.date()
        for decision in self._decisions.list_all():
            if decision.recommendation_id is None:
                # Phase A時点では発生しないが、将来recommendation_idを伴わない
                # DecisionSnapshotが導入された場合の安全弁(推測補完しない)。
                continue
            recommendation = self._recommendations.get(decision.recommendation_id)
            if recommendation is None:
                continue

            start_date = recommendation.recommended_at.date()
            for horizon in horizons:
                if self._evaluations.exists_for_decision_horizon(decision.decision_id, horizon):
                    continue
                evaluation_date = self._calendar.add_business_days(start_date, horizon)
                if evaluation_date > today:
                    continue

                result = self._recommendation_eval._evaluate_one(  # noqa: SLF001 - 意図的な内部再利用
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
                result = result.model_copy(update={"decision_id": decision.decision_id})
                self._evaluations.save(result)
                outcome.evaluated.append(result)
        return outcome
