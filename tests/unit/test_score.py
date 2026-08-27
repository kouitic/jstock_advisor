import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.scoring.score import (
    ScoreResult,
    UndervaluationSignals,
    compute_score,
)
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
        undervaluation_category_caps=_CONFIG.buy_decision.undervaluation_category_caps,
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
        undervaluation_category_caps=_CONFIG.buy_decision.undervaluation_category_caps,
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
        undervaluation_category_caps=_CONFIG.buy_decision.undervaluation_category_caps,
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
        undervaluation_category_caps=_CONFIG.buy_decision.undervaluation_category_caps,
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


# --- Issue #22 Phase 3.5(2026-08-28、観測性強化): component_states ----------


def _compute(**overrides: object) -> ScoreResult:
    base: dict[str, object] = dict(
        total_yield_pct=5.0,
        dividend=_dividend(
            is_progressive_or_doe_policy=True, consecutive_dividend_increase_years=5
        ),
        financial=_financial(),
        undervaluation_signals=UndervaluationSignals(
            per_below_median=True,
            pbr_below_median=True,
            dividend_yield_above_historical_average=True,
            drawdown_from_52w_high=True,
            below_fair_value=True,
            price_down_despite_stable_earnings=True,
        ),
        benefit_yield_pct=1.0,
        quarterly_operating_incomes=[Decimal("100"), Decimal("110"), Decimal("120")],
        price_bars=_bars([1000, 1010, 990, 1005, 1000] * 10),
        min_equity_ratio_pct=30.0,
        max_payout_ratio_pct=70.0,
        config=_CONFIG.scoring,
        undervaluation_category_caps=_CONFIG.buy_decision.undervaluation_category_caps,
    )
    base.update(overrides)
    return compute_score(**base)  # type: ignore[arg-type]


def test_component_states_all_evaluated_for_full_data() -> None:
    result = _compute()
    states = result.component_states
    assert set(states.keys()) == {
        "total_yield_attractiveness",
        "dividend_sustainability",
        "financial_health",
        "undervaluation",
        "shareholder_benefit_value",
        "earnings_stability",
        "price_stability",
    }
    for entry in states.values():
        assert entry["state"] == "EVALUATED"
        # v1は全componentを常に分母へ含める(観測記録もその事実を反映)
        assert entry["excluded_from_denominator"] is False


def test_component_states_never_emit_not_applicable() -> None:
    """Phase 3.5の大原則: 保存時に意味を推測しない。v1のスコアリングには
    「明確に評価対象外」と断定できる判定基準が存在しないため、いかなる欠測
    パターンでもNOT_APPLICABLEを生成しない(NOT_EVALUATEDへ倒す)。"""
    result = _compute(
        benefit_yield_pct=None,
        financial=_financial(equity_ratio_pct=None, payout_ratio_pct=None),
        undervaluation_signals=UndervaluationSignals(),
        quarterly_operating_incomes=[],
        price_bars=[],
    )
    for entry in result.component_states.values():
        assert entry["state"] in {"EVALUATED", "NOT_EVALUATED"}
        assert entry["state"] != "NOT_APPLICABLE"


def test_component_states_distinguish_missing_data_with_nonassertive_reasons() -> None:
    result = _compute(
        benefit_yield_pct=None,
        financial=_financial(equity_ratio_pct=None, payout_ratio_pct=None),
        undervaluation_signals=UndervaluationSignals(),
        quarterly_operating_incomes=[Decimal("100")],
        price_bars=[],
    )
    states = result.component_states

    # 優待利回りNone: 「優待制度なし」と断定せず、非断定のUNAVAILABLEとする
    benefit = states["shareholder_benefit_value"]
    assert benefit["state"] == "NOT_EVALUATED"
    assert benefit["reason_codes"] == ["BENEFIT_YIELD_UNAVAILABLE"]
    assert all("NO_BENEFIT" not in code for code in benefit["reason_codes"])

    health = states["financial_health"]
    assert health["state"] == "NOT_EVALUATED"
    assert health["reason_codes"] == ["EQUITY_RATIO_UNAVAILABLE"]

    uv = states["undervaluation"]
    assert uv["state"] == "NOT_EVALUATED"
    assert uv["reason_codes"] == ["NO_UNDERVALUATION_SIGNALS_AVAILABLE"]

    earnings = states["earnings_stability"]
    assert earnings["state"] == "NOT_EVALUATED"
    assert earnings["reason_codes"] == [
        "INSUFFICIENT_QUARTERLY_PERIODS",
        "NEUTRAL_FALLBACK_APPLIED",
    ]

    price = states["price_stability"]
    assert price["state"] == "NOT_EVALUATED"
    assert price["reason_codes"] == [
        "INSUFFICIENT_PRICE_HISTORY",
        "NEUTRAL_FALLBACK_APPLIED",
    ]

    # 配当持続性はv1では常に係数式で評価される(EVALUATED)が、欠測した入力を
    # reason_codesとして残す
    sustainability = states["dividend_sustainability"]
    assert sustainability["state"] == "EVALUATED"
    assert "PAYOUT_RATIO_UNAVAILABLE" in sustainability["reason_codes"]


def test_component_states_do_not_change_v1_score() -> None:
    """component_states(観測専用)の追加が、v1のbreakdown/totalへ一切影響
    しないことの回帰(同一入力でbreakdownが総和と一致し続けること)。"""
    result = _compute()
    b = result.breakdown
    parts = (
        b.total_yield_attractiveness
        + b.dividend_sustainability
        + b.financial_health
        + b.undervaluation
        + b.shareholder_benefit_value
        + b.earnings_stability
        + b.price_stability
    )
    assert b.total == pytest.approx(parts, abs=0.05)
