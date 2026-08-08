"""services/decision_evaluation_service.pyのテスト(判定精度向上機能Phase A)。

RecommendationEvaluationService._evaluate_one()を計算カーネルとして再利用する
設計のため、既存の営業日/暦日ホライズン評価ロジック自体は再テストしない
(test_recommendation_evaluation_service.pyで検証済み)。ここではdecision_id
ベースの紐付け・冪等性・未来データ非参照(既存ロジックの再利用による保証)を
検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig, DecisionEvaluationConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.common import (
    BuyPriceLevels,
    DataSourceReference,
    PriceWithRationale,
)
from jstock_advisor.domain.entities.decision_snapshot import (
    DECISION_SNAPSHOT_MODEL_VERSION,
    DecisionSnapshot,
)
from jstock_advisor.domain.entities.enums import ConfidenceLevel, DecisionType, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.interfaces.types import PriceBar, PriceHistory, PriceSnapshot
from jstock_advisor.services.decision_evaluation_service import DecisionEvaluationService

_STOCK_CODE = "2914"
_SOURCE = DataSourceReference(
    provider="test-fixture", fetched_at=dt.datetime(2026, 8, 8, tzinfo=dt.UTC)
)


def _config(horizons_business_days: list[int]) -> AppConfig:
    base = load_config()
    return base.model_copy(
        update={
            "decision_evaluation": DecisionEvaluationConfig(
                horizons_business_days=horizons_business_days
            )
        }
    )


def _calendar(config: AppConfig) -> BusinessCalendar:
    return BusinessCalendar.from_config(config.holiday_calendar)


class _FakeMarketDataProvider:
    """テスト用のダブル。get_price_historyは要求されたstart/end範囲に関わらず
    保持している全バーを返す(_evaluate_one()側のperiod_bars絞り込みが正しく
    機能しているかを判別できるようにするため)。"""

    def __init__(self, bars: list[PriceBar]) -> None:
        self._bars = bars

    def get_latest_price(self, stock_code: str) -> PriceSnapshot | None:
        return None

    def get_price_history(
        self, stock_code: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        return PriceHistory(symbol=stock_code, bars=self._bars, source=_SOURCE)

    def get_average_trading_value(self, stock_code: str, business_days: int) -> Decimal | None:
        return None

    def get_benchmark_price_history(
        self, symbol: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        return None


def _bar(date: dt.date, *, high: str, low: str, close: str) -> PriceBar:
    return PriceBar(
        date=date, open=Decimal(close), high=Decimal(high), low=Decimal(low),
        close=Decimal(close), volume=1000,
    )


def _recommendation(recommended_at: dt.datetime) -> Recommendation:
    return Recommendation(
        recommendation_id="rec-1",
        stock_code=_STOCK_CODE,
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


def _decision(recommended_at: dt.datetime, decision_id: str = "dec-1") -> DecisionSnapshot:
    return DecisionSnapshot(
        decision_id=decision_id,
        decision_type=DecisionType.BUY,
        stock_code=_STOCK_CODE,
        evaluated_at=recommended_at,
        evaluation_date_jst=recommended_at.date(),
        recommendation_id="rec-1",
        existing_action=RecommendationType.BUY,
        market_price=Decimal("2200"),
        rule_version="v1-mvp",
        model_version=DECISION_SNAPSHOT_MODEL_VERSION,
        data_fetched_at=recommended_at,
    )


def _build_service(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar, bars: list[PriceBar]
) -> tuple[
    DecisionEvaluationService, DecisionSnapshotRepository, RecommendationRepository,
    EvaluationResultRepository,
]:
    decision_repo = DecisionSnapshotRepository(store_dir=tmp_path)
    recommendation_repo = RecommendationRepository(store_dir=tmp_path)
    evaluation_repo = EvaluationResultRepository(store_dir=tmp_path)
    service = DecisionEvaluationService(
        market_data_provider=_FakeMarketDataProvider(bars),
        config=config,
        business_calendar=calendar,
        decision_repository=decision_repo,
        recommendation_repository=recommendation_repo,
        evaluation_repository=evaluation_repo,
    )
    return service, decision_repo, recommendation_repo, evaluation_repo


def test_run_due_decision_evaluations_computes_price_return(tmp_path: Path) -> None:
    config = _config([5])
    calendar = _calendar(config)
    recommended_at = dt.datetime(2026, 7, 20, 3, 0, tzinfo=dt.UTC)
    start_date = recommended_at.date()
    evaluation_date = calendar.add_business_days(start_date, 5)
    now = dt.datetime.combine(evaluation_date, dt.time(3, 0), tzinfo=dt.UTC)

    bars = [
        _bar(start_date, high="2200", low="2180", close="2200"),
        _bar(evaluation_date, high="2350", low="2300", close="2310"),
    ]
    service, decision_repo, recommendation_repo, evaluation_repo = _build_service(
        tmp_path, config, calendar, bars
    )
    recommendation_repo.save(_recommendation(recommended_at))
    decision_repo.save(_decision(recommended_at))

    outcome = service.run_due_decision_evaluations(now)

    assert len(outcome.evaluated) == 1
    result = outcome.evaluated[0]
    assert result.decision_id == "dec-1"
    assert result.recommendation_id == "rec-1"
    assert result.horizon_business_days == 5
    assert result.price_return_pct is not None
    saved = evaluation_repo.list_all()
    assert len(saved) == 1
    assert saved[0].decision_id == "dec-1"


def test_run_due_decision_evaluations_is_idempotent(tmp_path: Path) -> None:
    config = _config([5])
    calendar = _calendar(config)
    recommended_at = dt.datetime(2026, 7, 20, 3, 0, tzinfo=dt.UTC)
    start_date = recommended_at.date()
    evaluation_date = calendar.add_business_days(start_date, 5)
    now = dt.datetime.combine(evaluation_date, dt.time(3, 0), tzinfo=dt.UTC)

    bars = [_bar(evaluation_date, high="2300", low="2200", close="2250")]
    service, decision_repo, recommendation_repo, _ = _build_service(
        tmp_path, config, calendar, bars
    )
    recommendation_repo.save(_recommendation(recommended_at))
    decision_repo.save(_decision(recommended_at))

    first = service.run_due_decision_evaluations(now)
    second = service.run_due_decision_evaluations(now)

    assert len(first.evaluated) == 1
    assert second.evaluated == []


def test_run_due_decision_evaluations_skips_not_yet_due(tmp_path: Path) -> None:
    config = _config([250])
    calendar = _calendar(config)
    recommended_at = dt.datetime(2026, 7, 20, 3, 0, tzinfo=dt.UTC)
    now = dt.datetime(2026, 7, 21, 3, 0, tzinfo=dt.UTC)  # 250営業日には遠く及ばない

    service, decision_repo, recommendation_repo, _ = _build_service(tmp_path, config, calendar, [])
    recommendation_repo.save(_recommendation(recommended_at))
    decision_repo.save(_decision(recommended_at))

    outcome = service.run_due_decision_evaluations(now)
    assert outcome.evaluated == []


def test_run_due_decision_evaluations_horizon_boundary_250_days(tmp_path: Path) -> None:
    """horizon=250営業日ちょうど到達した場合に評価されること(境界値)。"""
    config = _config([250])
    calendar = _calendar(config)
    recommended_at = dt.datetime(2026, 1, 5, 3, 0, tzinfo=dt.UTC)
    start_date = recommended_at.date()
    evaluation_date = calendar.add_business_days(start_date, 250)
    now = dt.datetime.combine(evaluation_date, dt.time(3, 0), tzinfo=dt.UTC)

    bars = [_bar(evaluation_date, high="2300", low="2200", close="2250")]
    service, decision_repo, recommendation_repo, _ = _build_service(
        tmp_path, config, calendar, bars
    )
    recommendation_repo.save(_recommendation(recommended_at))
    decision_repo.save(_decision(recommended_at))

    outcome = service.run_due_decision_evaluations(now)

    assert len(outcome.evaluated) == 1
    assert outcome.evaluated[0].horizon_business_days == 250
    assert outcome.evaluated[0].evaluation_date == evaluation_date


def test_run_due_decision_evaluations_never_uses_bars_after_evaluation_date(tmp_path: Path) -> None:
    """evaluation_dateより未来の株価バーが計算結果に混入しないこと(既存
    _evaluate_one()の再利用による保証、6節参照)。"""
    config = _config([5])
    calendar = _calendar(config)
    recommended_at = dt.datetime(2026, 7, 20, 3, 0, tzinfo=dt.UTC)
    start_date = recommended_at.date()
    evaluation_date = calendar.add_business_days(start_date, 5)
    now = dt.datetime.combine(evaluation_date, dt.time(3, 0), tzinfo=dt.UTC)
    future_date = evaluation_date + dt.timedelta(days=30)

    bars = [
        _bar(start_date, high="2200", low="2190", close="2200"),
        _bar(evaluation_date, high="2260", low="2240", close="2250"),
        # evaluation_dateより未来の極端な値。含まれてしまえばmax_gain_pctが跳ね上がる。
        _bar(future_date, high="99999", low="1", close="2250"),
    ]
    service, decision_repo, recommendation_repo, _ = _build_service(
        tmp_path, config, calendar, bars
    )
    recommendation_repo.save(_recommendation(recommended_at))
    decision_repo.save(_decision(recommended_at))

    outcome = service.run_due_decision_evaluations(now)

    assert len(outcome.evaluated) == 1
    result = outcome.evaluated[0]
    assert result.max_gain_pct is not None
    assert result.max_gain_pct < 10.0  # 未来バーの99999が混入していれば桁違いに大きくなる


def test_run_due_decision_evaluations_skips_decision_with_missing_recommendation(
    tmp_path: Path,
) -> None:
    """join欠損(recommendation_idに対応するRecommendationが存在しない)場合は
    推測補完せずスキップすること。"""
    config = _config([5])
    calendar = _calendar(config)
    recommended_at = dt.datetime(2026, 7, 20, 3, 0, tzinfo=dt.UTC)
    now = dt.datetime(2026, 8, 1, 3, 0, tzinfo=dt.UTC)

    service, decision_repo, _recommendation_repo, evaluation_repo = _build_service(
        tmp_path, config, calendar, []
    )
    decision_repo.save(_decision(recommended_at))  # recommendation_repoには保存しない

    outcome = service.run_due_decision_evaluations(now)

    assert outcome.evaluated == []
    assert evaluation_repo.list_all() == []
