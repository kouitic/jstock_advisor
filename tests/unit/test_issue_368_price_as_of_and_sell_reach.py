"""Issue #368: 通知の価格as-of表示 + SELL側到達判定の追加。

A/B/C  Recommendation.price_as_of_dateの配線(buy_signal_service.py /
       sell_signal_service.py / profit_taking_service.py /
       holding_decision_notification_builder.py)と、通知本文への
       表示ラベル(_price_as_of_label)。
D/E    SELL側の利確目安到達判定(reached_partial_profit_start_price /
       reached_recommended_limit_price / reached_full_profit_consideration_price /
       business_days_to_reach_sell_price)が、BUY側と鏡像の方向
       (high>=price)で正しく判定されること。fixtureは既存のテストファイル
       (test_buy_signal_service.py / test_profit_taking_service.py /
       test_issue_67_recommendation_provenance_transfer.py)の
       架空データ・helperを再利用する(同じsnapshot構築コードを重複させない)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.common import (
    DataSourceReference,
    PriceWithRationale,
    SellPriceLevels,
)
from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.interfaces.types import PriceBar, PriceHistory, PriceSnapshot
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.services import line_notification_service as line_module
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.recommendation_evaluation_service import (
    RecommendationEvaluationService,
)
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot
from tests.unit.test_buy_signal_service import (
    _CALENDAR as _BUY_CALENDAR,
)
from tests.unit.test_buy_signal_service import (
    _CONFIG as _BUY_CONFIG,
)
from tests.unit.test_buy_signal_service import (
    _NIHON_SHINYAKU,
    _build_snapshot,
)
from tests.unit.test_buy_signal_service import (
    _NOW as _BUY_NOW,
)
from tests.unit.test_buy_signal_service import (
    _providers as _buy_providers,
)
from tests.unit.test_buy_signal_service import (
    service_module as buy_signal_service_module,
)
from tests.unit.test_issue_67_recommendation_provenance_transfer import (
    _base_snapshot,
    _holding_recommendation,
    _register_fictional_stock,  # noqa: F401 - autouse fixture
    _sell_recommendation,
)
from tests.unit.test_profit_taking_service import (
    _CONFIG as _PT_CONFIG,
)
from tests.unit.test_profit_taking_service import (
    _NOW as _PT_NOW,
)
from tests.unit.test_profit_taking_service import (
    _STALE_EARNINGS_DATE as _PT_STALE_EARNINGS_DATE,
)
from tests.unit.test_profit_taking_service import (
    _canned_result,
)
from tests.unit.test_profit_taking_service import (
    _holding as _pt_holding,
)
from tests.unit.test_profit_taking_service import (
    _providers as _pt_providers,
)

# --- A/C: price_as_of_dateの配線(Recommendation生成箇所) ------------------------


def test_buy_signal_service_wires_price_as_of_date_from_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _build_snapshot(_NIHON_SHINYAKU, price_as_of_date=dt.date(2026, 8, 3))
    monkeypatch.setattr(
        buy_signal_service_module, "build_stock_snapshot", lambda *a, **kw: (snapshot, None)
    )
    service = buy_signal_service_module.BuySignalService(
        providers=_buy_providers(), config=_BUY_CONFIG, business_calendar=_BUY_CALENDAR
    )

    outcome = service.analyze(_NIHON_SHINYAKU.stock_code, _BUY_NOW, RecommendationType.BUY)

    rec = outcome.recommendation
    assert rec is not None
    assert rec.price_as_of_date == dt.date(2026, 8, 3)


def test_sell_signal_service_wires_price_as_of_date_from_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _base_snapshot()
    rec = _sell_recommendation(monkeypatch, snapshot)
    assert rec.price_as_of_date == snapshot.price_as_of_date


def test_holding_decision_builder_wires_price_as_of_date_from_snapshot() -> None:
    snapshot = _base_snapshot()
    rec = _holding_recommendation(snapshot)
    assert rec.price_as_of_date == snapshot.price_as_of_date


def test_profit_taking_service_wires_price_as_of_date_from_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE),
    )
    providers = _pt_providers(_PT_STALE_EARNINGS_DATE, dt.date(2026, 6, 30))
    service = ProfitTakingService(providers=providers, config=_PT_CONFIG)

    outcome = service.analyze(_pt_holding("2914"), _PT_NOW)

    assert outcome.recommendation is not None
    rec = outcome.recommendation
    expected_snapshot, _ = build_stock_snapshot(providers, "2914", _PT_NOW, _PT_CONFIG)
    assert expected_snapshot is not None
    assert rec.price_as_of_date == expected_snapshot.price_as_of_date


# --- B: 通知本文の as-of ラベル -------------------------------------------------


def _rec_for_label(price_as_of_date: dt.date | None) -> Recommendation:
    return Recommendation(
        recommendation_id="rec-label",
        stock_code="0000",
        stock_name="テスト銘柄",
        recommended_at=dt.datetime(2026, 9, 10, tzinfo=dt.UTC),
        recommendation_type=RecommendationType.BUY,
        price_at_recommendation=Decimal("1192"),
        price_as_of_date=price_as_of_date,
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


def test_price_as_of_label_with_date() -> None:
    rec = _rec_for_label(dt.date(2026, 9, 10))
    assert line_module._price_as_of_label(rec) == "09/10終値"


def test_price_as_of_label_without_date_falls_back_to_generic_label() -> None:
    """旧データ(price_as_of_date無し)は日付を省略し「終値」とだけ表示する
    (#368設計C。現在値と誤読されることを防ぐ最低限の修正は日付が無くても効く)。"""
    rec = _rec_for_label(None)
    assert line_module._price_as_of_label(rec) == "終値"


def test_buy_candidate_message_uses_as_of_label_not_current_value_label() -> None:
    rec = _rec_for_label(dt.date(2026, 9, 10))
    message = line_module._format_buy_candidate_message(rec)
    assert "09/10終値: 1,192円" in message
    assert "現在値: 1,192円" not in message


def test_holding_decision_message_uses_as_of_label_not_current_value_label() -> None:
    rec = _rec_for_label(dt.date(2026, 9, 10)).model_copy(
        update={"recommendation_type": RecommendationType.HOLD}
    )
    message = line_module._format_holding_decision_message(rec)
    assert "09/10終値1,192円" in message
    assert "現在値1,192円" not in message


# --- D/E: SELL側の利確目安到達判定 ----------------------------------------------


@pytest.fixture
def config() -> AppConfig:
    return load_config()


@pytest.fixture
def calendar(config: AppConfig) -> BusinessCalendar:
    return BusinessCalendar.from_config(config.holiday_calendar)


def _bar(date: dt.date, *, high: str, low: str, close: str) -> PriceBar:
    return PriceBar(
        date=date, open=Decimal(close), high=Decimal(high), low=Decimal(low),
        close=Decimal(close), volume=1000,
    )


_FAKE_SOURCE = DataSourceReference(
    provider="fake", fetched_at=dt.datetime(2026, 8, 5, tzinfo=dt.UTC)
)


class _FakeMarketDataProvider:
    def __init__(self, bars: list[PriceBar]) -> None:
        self._bars = bars

    def get_latest_price(self, stock_code: str) -> PriceSnapshot | None:
        return None

    def get_price_history(
        self, stock_code: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        return PriceHistory(symbol=stock_code, bars=self._bars, source=_FAKE_SOURCE)

    def get_benchmark_price_history(
        self, symbol: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        return None


def _make_sell_recommendation(recommended_at: dt.datetime) -> Recommendation:
    return Recommendation(
        recommendation_id="rec-sell-1",
        stock_code="2914",
        stock_name="テスト銘柄",
        recommended_at=recommended_at,
        recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
        sell_prices=SellPriceLevels(
            partial_profit_start_price=PriceWithRationale(price=Decimal("2100"), rationale="x"),
            recommended_limit_price=PriceWithRationale(price=Decimal("2200"), rationale="x"),
            full_profit_consideration_price=PriceWithRationale(
                price=Decimal("2300"), rationale="x"
            ),
        ),
        price_at_recommendation=Decimal("2000"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


def _build_evaluation_service(
    tmp_path: Path,
    config: AppConfig,
    calendar: BusinessCalendar,
    now: dt.datetime,
    market_data_provider: object,
) -> tuple[RecommendationEvaluationService, RecommendationRepository]:
    recommendation_repo = RecommendationRepository(store_dir=tmp_path)
    evaluation_repo = EvaluationResultRepository(store_dir=tmp_path)
    service = RecommendationEvaluationService(
        market_data_provider=market_data_provider,  # type: ignore[arg-type]
        config=config,
        business_calendar=calendar,
        recommendation_repository=recommendation_repo,
        evaluation_repository=evaluation_repo,
    )
    return service, recommendation_repo


def test_sell_price_reach_direction_is_high_not_low(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    """SELL利確側の到達判定はBUY側(low<=price)の鏡像(high>=price)であること。

    recommended_limit_price=2200に対し、barのlow=100(BUY方向の判定基準を誤って
    流用していればreached=Trueになってしまう)・high=2150(2200には届かない)と
    なる価格帯を置く。方向を誤らずhigh>=priceで判定していれば、この bar だけでは
    到達しない(reached=False)ことを確認する(方向を弁別できるmutation-discriminating
    なテスト)。
    """
    recommended_at = dt.datetime(2026, 8, 3, 7, 0, tzinfo=dt.UTC)
    now = dt.datetime(2026, 8, 10, 7, 0, tzinfo=dt.UTC)
    bars = [
        _bar(dt.date(2026, 8, 4), high="2150", low="100", close="2100"),
        _bar(dt.date(2026, 8, 5), high="2160", low="2050", close="2100"),
    ]
    service, recommendation_repo = _build_evaluation_service(
        tmp_path, config, calendar, now, _FakeMarketDataProvider(bars)
    )
    recommendation_repo.save(_make_sell_recommendation(recommended_at))

    outcome = service.run_due_evaluations(now)

    assert outcome.evaluated
    result = next(r for r in outcome.evaluated if r.horizon_business_days == 1)
    assert result.reached_recommended_limit_price is False


def test_sell_price_reach_is_true_when_high_actually_reaches_target(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    """horizon=1の窓は[day-zero, day-zero+1営業日]の2営業日ぶんであるため
    (#368設計R2/N1)、到達させたい高値バーはこの2営業日の範囲内(08/03・08/04)
    に置く必要がある。"""
    recommended_at = dt.datetime(2026, 8, 3, 7, 0, tzinfo=dt.UTC)
    now = dt.datetime(2026, 8, 10, 7, 0, tzinfo=dt.UTC)
    bars = [
        _bar(dt.date(2026, 8, 3), high="2150", low="2050", close="2100"),
        _bar(dt.date(2026, 8, 4), high="2260", low="2200", close="2250"),
    ]
    service, recommendation_repo = _build_evaluation_service(
        tmp_path, config, calendar, now, _FakeMarketDataProvider(bars)
    )
    recommendation_repo.save(_make_sell_recommendation(recommended_at))

    outcome = service.run_due_evaluations(now)

    assert outcome.evaluated
    result = next(r for r in outcome.evaluated if r.horizon_business_days == 1)
    assert result.reached_recommended_limit_price is True
    assert result.reached_partial_profit_start_price is True
    assert result.reached_full_profit_consideration_price is False
    assert result.business_days_to_reach_sell_price == 1


def test_sell_price_reach_is_none_when_recommendation_has_no_sell_prices(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    """BUY専用のRecommendation(sell_prices=None)では、SELL側到達フラグも自然に
    Noneになる(BUY側のreached_*_buy_priceがbuy_prices=Noneで既にNoneになるのと
    同じ既存パターン)。"""
    now = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
    market_data = MockMarketDataProvider(now=now)
    service, recommendation_repo = _build_evaluation_service(
        tmp_path, config, calendar, now, market_data
    )
    recommendation_repo.save(
        Recommendation(
            recommendation_id="rec-buy-only",
            stock_code="2914",
            stock_name="テスト銘柄",
            recommended_at=dt.datetime(2026, 1, 4, tzinfo=dt.UTC),
            recommendation_type=RecommendationType.BUY,
            price_at_recommendation=Decimal("2200"),
            confidence=ConfidenceLevel.HIGH,
            rule_version="v1-mvp",
        )
    )

    outcome = service.run_due_evaluations(now)

    assert outcome.evaluated
    for result in outcome.evaluated:
        assert result.reached_recommended_limit_price is None
        assert result.reached_partial_profit_start_price is None
        assert result.reached_full_profit_consideration_price is None
        assert result.business_days_to_reach_sell_price is None
