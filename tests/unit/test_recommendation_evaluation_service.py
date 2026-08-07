import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.services.recommendation_evaluation_service import (
    RecommendationEvaluationService,
)

_STOCK_CODE = "2914"
_RECOMMENDED_AT = dt.datetime(2024, 1, 4, tzinfo=dt.UTC)


@pytest.fixture
def config() -> AppConfig:
    return load_config()


@pytest.fixture
def calendar(config: AppConfig) -> BusinessCalendar:
    return BusinessCalendar.from_config(config.holiday_calendar)


def _make_recommendation(
    recommendation_id: str = "rec-1",
    recommendation_type: RecommendationType = RecommendationType.BUY,
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=_STOCK_CODE,
        stock_name="テスト銘柄",
        recommended_at=_RECOMMENDED_AT,
        recommendation_type=recommendation_type,
        buy_prices=BuyPriceLevels(
            standard=PriceWithRationale(price=Decimal("2000"), rationale="x"),
        ),
        price_at_recommendation=Decimal("2200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


def _build_service(
    tmp_path: Path,
    config: AppConfig,
    calendar: BusinessCalendar,
    now: dt.datetime,
) -> tuple[RecommendationEvaluationService, RecommendationRepository, EvaluationResultRepository]:
    recommendation_repo = RecommendationRepository(store_dir=tmp_path)
    evaluation_repo = EvaluationResultRepository(store_dir=tmp_path)
    service = RecommendationEvaluationService(
        market_data_provider=MockMarketDataProvider(now=now),
        config=config,
        business_calendar=calendar,
        recommendation_repository=recommendation_repo,
        evaluation_repository=evaluation_repo,
    )
    return service, recommendation_repo, evaluation_repo


def test_run_due_evaluations_computes_price_return(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    now = dt.datetime(2024, 3, 1, tzinfo=dt.UTC)
    service, recommendation_repo, evaluation_repo = _build_service(tmp_path, config, calendar, now)
    recommendation_repo.save(_make_recommendation())

    outcome = service.run_due_evaluations(now)

    assert outcome.evaluated
    for result in outcome.evaluated:
        assert result.recommendation_id == "rec-1"
        assert result.price_return_pct is not None
        assert result.evaluation_date <= now.date()
    saved = evaluation_repo.list_by_recommendation("rec-1")
    assert len(saved) == len(outcome.evaluated)


def test_run_due_evaluations_skips_not_yet_due(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    service, recommendation_repo, _ = _build_service(tmp_path, config, calendar, _RECOMMENDED_AT)
    recommendation_repo.save(_make_recommendation())

    outcome = service.run_due_evaluations(_RECOMMENDED_AT)
    assert outcome.evaluated == []


def test_run_due_evaluations_is_idempotent(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    now = dt.datetime(2024, 3, 1, tzinfo=dt.UTC)
    service, recommendation_repo, _ = _build_service(tmp_path, config, calendar, now)
    recommendation_repo.save(_make_recommendation())

    first = service.run_due_evaluations(now)
    second = service.run_due_evaluations(now)

    assert first.evaluated
    assert second.evaluated == []


def test_buy_price_reach_flags_are_computed(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    now = dt.datetime(2024, 6, 1, tzinfo=dt.UTC)
    service, recommendation_repo, _ = _build_service(tmp_path, config, calendar, now)
    recommendation_repo.save(_make_recommendation())

    outcome = service.run_due_evaluations(now)
    assert outcome.evaluated
    for result in outcome.evaluated:
        # 買値到達フラグはbuy_pricesがある推奨では必ずbool/Noneのどちらかで確定している
        assert result.reached_standard_buy_price in (True, False)


def test_data_error_when_recommendation_after_available_history(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    far_future_recommendation = dt.datetime(2030, 1, 4, tzinfo=dt.UTC)
    now = dt.datetime(2030, 6, 1, tzinfo=dt.UTC)
    service, recommendation_repo, _ = _build_service(tmp_path, config, calendar, now)
    recommendation_repo.save(
        _make_recommendation("rec-far-future").model_copy(
            update={"recommended_at": far_future_recommendation}
        )
    )

    outcome = service.run_due_evaluations(now)
    assert outcome.evaluated == []
    assert outcome.skipped_due_to_data_error


# --- 振り返り機能改修: JST暦日ベース評価(run_due_calendar_evaluations) -------------


def test_run_due_calendar_evaluations_computes_price_return_at_7_days(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    recommended_at = dt.datetime(2026, 7, 20, 3, 0, tzinfo=dt.UTC)  # JST 2026-07-20 12:00
    now = dt.datetime(2026, 7, 27, 3, 0, tzinfo=dt.UTC)  # JST 2026-07-27 12:00
    service, recommendation_repo, evaluation_repo = _build_service(tmp_path, config, calendar, now)
    recommendation_repo.save(
        _make_recommendation().model_copy(update={"recommended_at": recommended_at})
    )

    outcome = service.run_due_calendar_evaluations(now)

    assert len(outcome.evaluated) == 1
    result = outcome.evaluated[0]
    assert result.horizon_calendar_days == 7
    assert result.horizon_business_days is None
    assert result.evaluation_date == dt.date(2026, 7, 27)
    assert result.price_return_pct is not None
    saved = evaluation_repo.list_by_recommendation("rec-1")
    assert len(saved) == 1


def test_run_due_calendar_evaluations_skips_not_yet_due(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    recommended_at = dt.datetime(2026, 7, 20, 3, 0, tzinfo=dt.UTC)
    now = dt.datetime(2026, 7, 24, 3, 0, tzinfo=dt.UTC)  # 4暦日後、まだ7暦日未到来
    service, recommendation_repo, _ = _build_service(tmp_path, config, calendar, now)
    recommendation_repo.save(
        _make_recommendation().model_copy(update={"recommended_at": recommended_at})
    )

    outcome = service.run_due_calendar_evaluations(now)
    assert outcome.evaluated == []


def test_run_due_calendar_evaluations_is_idempotent(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    recommended_at = dt.datetime(2026, 7, 20, 3, 0, tzinfo=dt.UTC)
    now = dt.datetime(2026, 7, 27, 3, 0, tzinfo=dt.UTC)
    service, recommendation_repo, _ = _build_service(tmp_path, config, calendar, now)
    recommendation_repo.save(
        _make_recommendation().model_copy(update={"recommended_at": recommended_at})
    )

    first = service.run_due_calendar_evaluations(now)
    second = service.run_due_calendar_evaluations(now)

    assert len(first.evaluated) == 1
    assert second.evaluated == []


def test_run_due_calendar_evaluations_catches_up_after_target_date(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    """評価基準日にデータ取得失敗等で未評価のまま残っても、後日のバッチ実行で
    catch-upされること(重複評価にはならない)。"""
    recommended_at = dt.datetime(2026, 7, 20, 3, 0, tzinfo=dt.UTC)
    not_yet_due = dt.datetime(2026, 7, 24, 3, 0, tzinfo=dt.UTC)
    later = dt.datetime(2026, 7, 29, 3, 0, tzinfo=dt.UTC)
    service, recommendation_repo, _ = _build_service(tmp_path, config, calendar, later)
    recommendation_repo.save(
        _make_recommendation().model_copy(update={"recommended_at": recommended_at})
    )

    first = service.run_due_calendar_evaluations(not_yet_due)
    assert first.evaluated == []

    second = service.run_due_calendar_evaluations(later)
    assert len(second.evaluated) == 1
    assert second.evaluated[0].evaluation_date == dt.date(2026, 7, 27)


def test_run_due_calendar_evaluations_uses_jst_date_boundary_when_due(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    """recommended_at・nowともにUTC深夜帯(JSTでは日付が繰り上がる)の場合でも、
    JST暦日基準で正しくevaluation_dateが算出されること(決算日修正で確立したJST
    境界原則の回帰確認)。naive UTC .date()を直接使う実装だと
    evaluation_date=2026-08-01(誤り)になってしまうが、正しい実装は2026-08-02。"""
    recommended_at = dt.datetime(2026, 7, 25, 23, 30, tzinfo=dt.UTC)  # JST 2026-07-26 08:30
    now = dt.datetime(2026, 8, 1, 20, 0, tzinfo=dt.UTC)  # JST 2026-08-02 05:00
    service, recommendation_repo, _ = _build_service(tmp_path, config, calendar, now)
    recommendation_repo.save(
        _make_recommendation().model_copy(update={"recommended_at": recommended_at})
    )

    outcome = service.run_due_calendar_evaluations(now)

    assert len(outcome.evaluated) == 1
    assert outcome.evaluated[0].evaluation_date == dt.date(2026, 8, 2)


def test_run_due_calendar_evaluations_uses_jst_date_boundary_when_not_yet_due(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    """上記の対となるケース: naive UTC .date()実装なら誤って「到来済み」と
    判定してしまうタイミングでも、正しいJST実装では未到来としてスキップすること。"""
    # JST 2026-07-26 08:30 → target 2026-08-02
    recommended_at = dt.datetime(2026, 7, 25, 23, 30, tzinfo=dt.UTC)
    # JST 2026-08-01 19:00、まだ8/2になっていない
    now = dt.datetime(2026, 8, 1, 10, 0, tzinfo=dt.UTC)
    service, recommendation_repo, _ = _build_service(tmp_path, config, calendar, now)
    recommendation_repo.save(
        _make_recommendation().model_copy(update={"recommended_at": recommended_at})
    )

    outcome = service.run_due_calendar_evaluations(now)
    assert outcome.evaluated == []


def test_run_due_calendar_evaluations_covers_watch_type(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    """WATCH等、既存の営業日ホライズン評価では対象外だった種別も、暦日7日評価では
    全RecommendationTypeが対象になること(週次改善レビューの分析母数を揃えるため)。"""
    recommended_at = dt.datetime(2026, 7, 20, 3, 0, tzinfo=dt.UTC)
    now = dt.datetime(2026, 7, 27, 3, 0, tzinfo=dt.UTC)
    service, recommendation_repo, _ = _build_service(tmp_path, config, calendar, now)
    recommendation_repo.save(
        _make_recommendation(recommendation_type=RecommendationType.WATCH).model_copy(
            update={"recommended_at": recommended_at}
        )
    )

    outcome = service.run_due_calendar_evaluations(now)
    assert len(outcome.evaluated) == 1
    assert outcome.evaluated[0].evaluation_label.value == "INCONCLUSIVE"
