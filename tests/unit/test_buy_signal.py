import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.scoring.score import ScoreResult, UndervaluationSignals, compute_score
from jstock_advisor.domain.signals.buy_signal import (
    compute_drawdown_from_52w_high_pct,
    compute_recent_price_change_pct,
    compute_undervaluation_signals,
    has_severe_earnings_decline,
    is_earnings_trend_non_decreasing,
    score_areas,
)
from jstock_advisor.interfaces.types import DividendInfo, FinancialSummary, PriceBar

_NOW = dt.datetime(2026, 7, 24, 7, 0, tzinfo=dt.UTC)
_SOURCE = DataSourceReference(provider="test", fetched_at=_NOW)
_CONFIG = load_config()


def _bar(date: dt.date, low: str, high: str, close: str) -> PriceBar:
    return PriceBar(
        date=date,
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=1000,
    )


def test_has_severe_earnings_decline_true() -> None:
    assert has_severe_earnings_decline([Decimal("1000"), Decimal("600")])  # -40%


def test_has_severe_earnings_decline_false_for_mild_decline() -> None:
    assert not has_severe_earnings_decline([Decimal("1000"), Decimal("900")])  # -10%


def test_has_severe_earnings_decline_false_when_insufficient_data() -> None:
    assert not has_severe_earnings_decline([Decimal("1000")])


def test_is_earnings_trend_non_decreasing() -> None:
    assert (
        is_earnings_trend_non_decreasing([Decimal("100"), Decimal("110"), Decimal("120")]) is True
    )
    assert is_earnings_trend_non_decreasing([Decimal("120"), Decimal("100")]) is False
    assert is_earnings_trend_non_decreasing([Decimal("100")]) is None


def test_compute_drawdown_from_52w_high_pct() -> None:
    as_of = dt.date(2026, 7, 24)
    bars = [_bar(dt.date(2026, 1, 1), "900", "1000", "950")]
    drawdown = compute_drawdown_from_52w_high_pct(Decimal("800"), bars, as_of)
    assert drawdown == -20.0


def test_compute_recent_price_change_pct() -> None:
    as_of = dt.date(2026, 7, 24)
    bars = [
        _bar(as_of - dt.timedelta(days=90), "1000", "1000", "1000"),
        _bar(as_of, "800", "800", "800"),
    ]
    change = compute_recent_price_change_pct(bars, as_of, lookback_days=60)
    assert change == -20.0


def test_undervaluation_signals_disabled_when_severe_earnings_decline() -> None:
    signals = compute_undervaluation_signals(
        current_price=Decimal("800"),
        current_per=None,
        historical_per_median=None,
        current_pbr=None,
        historical_pbr_median=None,
        current_dividend_yield_pct=None,
        historical_average_dividend_yield_pct=None,
        drawdown_from_52w_high_pct=-30.0,
        valuation_anchor=Decimal("900"),
        recent_price_change_pct=None,
        earnings_trend_non_decreasing=None,
        severe_earnings_decline=True,
    )
    # 業績悪化中は下落・割安シグナルをFalseに強制する(株価が安いだけで高評価にしない)
    assert signals.drawdown_from_52w_high is False
    assert signals.below_fair_value is False


def test_undervaluation_signals_positive_when_healthy() -> None:
    signals = compute_undervaluation_signals(
        current_price=Decimal("800"),
        current_per=Decimal("10"),
        historical_per_median=Decimal("15"),
        current_pbr=Decimal("1.0"),
        historical_pbr_median=Decimal("1.5"),
        current_dividend_yield_pct=4.0,
        historical_average_dividend_yield_pct=3.0,
        drawdown_from_52w_high_pct=-20.0,
        valuation_anchor=Decimal("900"),
        recent_price_change_pct=-15.0,
        earnings_trend_non_decreasing=True,
        severe_earnings_decline=False,
    )
    assert signals.per_below_median is True
    assert signals.pbr_below_median is True
    assert signals.dividend_yield_above_historical_average is True
    assert signals.drawdown_from_52w_high is True
    assert signals.below_fair_value is True
    assert signals.price_down_despite_stable_earnings is True


def test_undervaluation_signals_below_fair_value_uses_valuation_anchor() -> None:
    signals = compute_undervaluation_signals(
        current_price=Decimal("950"),
        current_per=None,
        historical_per_median=None,
        current_pbr=None,
        historical_pbr_median=None,
        current_dividend_yield_pct=None,
        historical_average_dividend_yield_pct=None,
        drawdown_from_52w_high_pct=None,
        valuation_anchor=Decimal("900"),  # 現在値950 > anchor900 なので割安ではない
        recent_price_change_pct=None,
        earnings_trend_non_decreasing=None,
        severe_earnings_decline=False,
    )
    assert signals.below_fair_value is False


def _score_result(total_yield_pct: float = 5.0) -> ScoreResult:
    financial = FinancialSummary(
        stock_code="8136",
        fiscal_period_end=_NOW.date(),
        equity_ratio_pct=60.0,
        payout_ratio_pct=45.0,
        source=_SOURCE,
    )
    dividend = DividendInfo(stock_code="8136", fiscal_year="2026", source=_SOURCE)
    return compute_score(
        total_yield_pct=total_yield_pct,
        dividend=dividend,
        financial=financial,
        undervaluation_signals=UndervaluationSignals(per_below_median=True),
        benefit_yield_pct=1.0,
        quarterly_operating_incomes=[Decimal("100"), Decimal("110")],
        price_bars=[],
        min_equity_ratio_pct=30.0,
        max_payout_ratio_pct=70.0,
        config=_CONFIG.scoring,
        undervaluation_category_caps=_CONFIG.buy_decision.undervaluation_category_caps,
    )


def test_score_areas_above_returns_strong_components() -> None:
    result = _score_result(total_yield_pct=10.0)
    areas = score_areas(result, _CONFIG.scoring, ratio=0.7, above=True)
    assert any("財務健全性" in a for a in areas)


def test_score_areas_below_returns_weak_components() -> None:
    result = _score_result(total_yield_pct=0.0)
    areas = score_areas(result, _CONFIG.scoring, ratio=0.3, above=False)
    assert any("総合利回り" in a for a in areas)
