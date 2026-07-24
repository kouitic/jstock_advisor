import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.scoring.score import UndervaluationSignals, compute_score
from jstock_advisor.interfaces.types import DividendInfo, FinancialSummary, PriceBar

_NOW = dt.datetime(2026, 7, 24, 7, 0, tzinfo=dt.UTC)
_SOURCE = DataSourceReference(provider="test", fetched_at=_NOW)
_CONFIG = load_config()


def _financial(**overrides: object) -> FinancialSummary:
    base = dict(
        stock_code="8136",
        fiscal_period_end=_NOW.date(),
        equity_ratio_pct=60.0,
        payout_ratio_pct=45.0,
        source=_SOURCE,
    )
    base.update(overrides)
    return FinancialSummary(**base)  # type: ignore[arg-type]


def _dividend(**overrides: object) -> DividendInfo:
    base = dict(stock_code="8136", fiscal_year="2026", source=_SOURCE)
    base.update(overrides)
    return DividendInfo(**base)  # type: ignore[arg-type]


def _bars(closes: list[float]) -> list[PriceBar]:
    return [
        PriceBar(
            date=dt.date(2026, 1, 1) + dt.timedelta(days=i),
            open=Decimal(str(c)),
            high=Decimal(str(c)),
            low=Decimal(str(c)),
            close=Decimal(str(c)),
            volume=1000,
        )
        for i, c in enumerate(closes)
    ]


def test_score_breakdown_sums_to_total() -> None:
    result = compute_score(
        total_yield_pct=5.0,
        dividend=_dividend(
            is_progressive_or_doe_policy=True, consecutive_dividend_increase_years=5
        ),
        financial=_financial(),
        undervaluation_signals=UndervaluationSignals(per_below_median=True, pbr_below_median=True),
        benefit_yield_pct=1.0,
        quarterly_operating_incomes=[Decimal("100"), Decimal("110"), Decimal("120")],
        price_bars=_bars([1000, 1010, 990, 1005, 1000] * 10),
        min_equity_ratio_pct=30.0,
        max_payout_ratio_pct=70.0,
        config=_CONFIG.scoring,
    )
    component_sum = (
        result.breakdown.total_yield_attractiveness
        + result.breakdown.dividend_sustainability
        + result.breakdown.financial_health
        + result.breakdown.undervaluation
        + result.breakdown.shareholder_benefit_value
        + result.breakdown.earnings_stability
        + result.breakdown.price_stability
    )
    assert abs(component_sum - result.breakdown.total) < 0.01
    assert 0 <= result.breakdown.total <= 100


def test_score_is_zero_or_low_for_weak_stock() -> None:
    result = compute_score(
        total_yield_pct=3.5,  # ちょうど下限(0点扱い)
        dividend=_dividend(),
        financial=_financial(equity_ratio_pct=30.0),  # ちょうど下限(0点扱い)
        undervaluation_signals=UndervaluationSignals(),
        benefit_yield_pct=None,
        quarterly_operating_incomes=[],
        price_bars=[],
        min_equity_ratio_pct=30.0,
        max_payout_ratio_pct=70.0,
        config=_CONFIG.scoring,
    )
    assert result.breakdown.total_yield_attractiveness == 0.0
    assert result.breakdown.financial_health == 0.0
    assert result.breakdown.shareholder_benefit_value == 0.0
    assert result.breakdown.undervaluation == 0.0


def test_score_full_marks_for_excellent_stock() -> None:
    result = compute_score(
        total_yield_pct=10.0,
        dividend=_dividend(
            is_progressive_or_doe_policy=True, consecutive_dividend_increase_years=10
        ),
        financial=_financial(equity_ratio_pct=80.0, payout_ratio_pct=0.0),
        undervaluation_signals=UndervaluationSignals(
            per_below_median=True,
            pbr_below_median=True,
            dividend_yield_above_historical_average=True,
            drawdown_from_52w_high=True,
            below_fair_value=True,
            price_down_despite_stable_earnings=True,
        ),
        benefit_yield_pct=5.0,
        quarterly_operating_incomes=[
            Decimal("100"),
            Decimal("110"),
            Decimal("120"),
            Decimal("130"),
        ],
        price_bars=_bars([1000] * 60),  # ボラティリティゼロ
        min_equity_ratio_pct=30.0,
        max_payout_ratio_pct=70.0,
        config=_CONFIG.scoring,
    )
    assert result.breakdown.total == 100.0


def test_formulas_are_populated_for_every_component() -> None:
    result = compute_score(
        total_yield_pct=4.0,
        dividend=_dividend(),
        financial=_financial(),
        undervaluation_signals=UndervaluationSignals(per_below_median=True),
        benefit_yield_pct=1.0,
        quarterly_operating_incomes=[Decimal("100"), Decimal("90")],
        price_bars=_bars([1000, 1010]),
        min_equity_ratio_pct=30.0,
        max_payout_ratio_pct=70.0,
        config=_CONFIG.scoring,
    )
    assert set(result.formulas.keys()) == {
        "total_yield_attractiveness",
        "dividend_sustainability",
        "financial_health",
        "undervaluation",
        "shareholder_benefit_value",
        "earnings_stability",
        "price_stability",
    }
    assert all(result.formulas.values())
