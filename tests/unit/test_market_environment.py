"""domain/signals/market_environment.pyのテスト(判定精度向上機能Phase D:
Market Environment Score)。

TOPIX barsのみを使ったtrend_structure/medium_term_return/drawdown3成分の
算出過程と、MarketEnvironmentResultのEntity不変条件(model_validator)の
両方を検証する。

コードレビュー対応(2026-08): bar staleness判定はBusinessCalendarによる
実際の営業日カウントで行われることを直接検証する(暦日差ではない)。
また、min_bars_ma60/min_bars_return_60dが実際の評価条件として機能する
ことを、config値を変えた際にscore/coverageが変化することで直接証明する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import (
    EnvironmentCategoryThresholds,
    MarketEnvironmentComponentWeights,
    MarketEnvironmentConfig,
    TrendClassificationScoreConfig,
)
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EnvironmentCategory,
    MarketEnvironmentEvaluationState,
)
from jstock_advisor.domain.entities.market_environment import MarketEnvironmentResult
from jstock_advisor.domain.signals.market_environment import (
    REASON_COVERAGE_BELOW_MINIMUM,
    REASON_DRAWDOWN_UNAVAILABLE,
    REASON_MARKET_BARS_STALE,
    REASON_MARKET_DATA_UNAVAILABLE,
    REASON_RETURN_UNAVAILABLE,
    REASON_TREND_STRUCTURE_UNAVAILABLE,
    evaluate_market_environment,
    is_bars_stale,
    market_environment_config_values,
    market_environment_result_to_metrics,
)
from jstock_advisor.interfaces.types import PriceBar

_NOW = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)
_CALENDAR = BusinessCalendar.from_config(load_config().holiday_calendar)


def _config(**overrides: object) -> MarketEnvironmentConfig:
    defaults: dict[str, object] = dict(
        model_version="market_environment_v1",
        component_weights=MarketEnvironmentComponentWeights(
            trend_structure=0.4, medium_term_return=0.3, drawdown=0.3
        ),
        trend_classification_score=TrendClassificationScoreConfig(
            strong_uptrend=100.0,
            uptrend=50.0,
            neutral=0.0,
            downtrend=-50.0,
            strong_downtrend=-100.0,
        ),
        ma_slope_lookback_days=5,
        return_score_scale_pct=15.0,
        drawdown_window_days=60,
        drawdown_neutral_threshold_pct=-3.0,
        drawdown_scale_pct=15.0,
        min_bars_ma60=60,
        min_bars_return_60d=61,
        max_bar_staleness_business_days=5,
        min_coverage_required=0.3,
        coverage_high_threshold=0.9,
        coverage_medium_threshold=0.5,
        category_thresholds=EnvironmentCategoryThresholds(
            strong_tailwind=60.0, tailwind=20.0, headwind=-20.0, strong_headwind=-60.0
        ),
    )
    defaults.update(overrides)
    return MarketEnvironmentConfig.model_validate(defaults)


_CONFIG = _config()


def _bars(
    n: int,
    *,
    start_price: Decimal = Decimal("2000"),
    daily_pct: Decimal = Decimal("0.001"),
    start_date: dt.date = dt.date(2026, 1, 1),
) -> list[PriceBar]:
    bars = []
    price = start_price
    for i in range(n):
        price = price * (Decimal("1") + daily_pct)
        bars.append(
            PriceBar(
                date=start_date + dt.timedelta(days=i),
                open=price,
                high=price * Decimal("1.01"),
                low=price * Decimal("0.99"),
                close=price,
                volume=1_000_000,
            )
        )
    return bars


# --- 十分なbars ------------------------------------------------------------


def test_evaluates_with_sufficient_bars() -> None:
    bars = _bars(120)
    result = evaluate_market_environment(bars, bars[-1].date, _NOW, _CONFIG, _CALENDAR)
    assert result.state == MarketEnvironmentEvaluationState.EVALUATED
    assert result.score is not None
    assert result.category is not None
    assert result.confidence is not None
    assert result.coverage == pytest.approx(1.0)
    assert result.trend_classification is not None


# --- MA20のみ評価可能(MA60に足りないbars) ----------------------------------


def test_ma20_only_available_reduces_coverage_and_excludes_trend() -> None:
    bars = _bars(40)  # MA60(60本)未達だがMA20(20本)は算出可能
    result = evaluate_market_environment(bars, bars[-1].date, _NOW, _CONFIG, _CALENDAR)
    # ma20/ma60両方揃わないとtrend_structureはevaluableにならない(境界一貫性)
    assert result.trend_structure_component is None
    assert REASON_TREND_STRUCTURE_UNAVAILABLE in result.reason_codes
    assert result.coverage < 1.0


def test_ma60_available_enables_trend_structure() -> None:
    bars = _bars(70)
    result = evaluate_market_environment(bars, bars[-1].date, _NOW, _CONFIG, _CALENDAR)
    assert result.trend_structure_component is not None
    assert result.ma20 is not None
    assert result.ma60 is not None


# --- min_bars_ma60が実際の評価条件として使われること -------------------------


def test_min_bars_ma60_excludes_trend_even_when_ma_values_computable() -> None:
    """ma20/ma60/slopeの個別計算結果がたまたま得られても、config.min_bars_
    ma60(最低本数条件)を満たさない場合は評価しない(config_values_usedに
    保存した値と実際の評価条件を一致させる、コードレビュー対応)。"""
    bars = _bars(70)  # ma60自体は計算可能(70>=60本)
    baseline = evaluate_market_environment(bars, bars[-1].date, _NOW, _CONFIG, _CALENDAR)
    assert baseline.trend_structure_component is not None

    strict_config = _config(min_bars_ma60=100)  # 70本では満たせない
    result = evaluate_market_environment(bars, bars[-1].date, _NOW, strict_config, _CALENDAR)
    assert result.trend_structure_component is None
    assert REASON_TREND_STRUCTURE_UNAVAILABLE in result.reason_codes
    assert result.coverage < baseline.coverage


# --- 20d/60d return ----------------------------------------------------------


def test_return_component_uses_positive_return_for_uptrend() -> None:
    bars = _bars(120, daily_pct=Decimal("0.002"))
    result = evaluate_market_environment(bars, bars[-1].date, _NOW, _CONFIG, _CALENDAR)
    assert result.return_20d_pct is not None
    assert result.return_20d_pct > 0
    assert result.medium_term_return_component is not None
    assert result.medium_term_return_component > 0


def test_return_component_none_when_insufficient_bars() -> None:
    bars = _bars(10)  # 20d returnにも足りない
    result = evaluate_market_environment(bars, bars[-1].date, _NOW, _CONFIG, _CALENDAR)
    assert result.return_20d_pct is None
    assert result.return_60d_pct is None
    assert result.medium_term_return_component is None
    assert REASON_RETURN_UNAVAILABLE in result.reason_codes


def test_min_bars_return_60d_excludes_only_60d_return() -> None:
    """60dだけ不足している場合は20dのみでmedium_term_return_componentを
    算出し続ける(コードレビュー対応)。"""
    bars = _bars(65, daily_pct=Decimal("0.002"))  # 20d/60d両方とも計算可能な本数
    strict_config = _config(min_bars_return_60d=100)  # 65本では60d側を満たせない
    result = evaluate_market_environment(bars, bars[-1].date, _NOW, strict_config, _CALENDAR)
    assert result.return_20d_pct is not None
    assert result.return_60d_pct is None
    assert result.medium_term_return_component is not None  # 20dのみで部分評価継続


# --- drawdown ----------------------------------------------------------------


def test_drawdown_component_negative_after_pullback() -> None:
    up_bars = _bars(80, daily_pct=Decimal("0.01"))
    down_bars = _bars(
        20,
        start_price=up_bars[-1].close,
        daily_pct=Decimal("-0.02"),
        start_date=up_bars[-1].date + dt.timedelta(days=1),
    )
    bars = up_bars + down_bars
    result = evaluate_market_environment(bars, bars[-1].date, _NOW, _CONFIG, _CALENDAR)
    assert result.drawdown_from_high_pct is not None
    assert result.drawdown_from_high_pct < 0
    assert result.drawdown_component is not None
    assert result.drawdown_component < 0


def test_drawdown_unavailable_reason_when_no_bars() -> None:
    result = evaluate_market_environment([], dt.date(2026, 8, 10), _NOW, _CONFIG, _CALENDAR)
    assert result.drawdown_component is None
    assert REASON_DRAWDOWN_UNAVAILABLE in result.reason_codes
    assert REASON_MARKET_DATA_UNAVAILABLE in result.reason_codes


# --- future bar除外 / point-in-time ------------------------------------------


def test_future_bars_are_excluded_from_computation() -> None:
    bars = _bars(120)
    as_of_date = bars[-10].date  # bars[-10]自身はas_of_date以下なので有効
    future_included = bars  # 末尾9本が未来
    result_with_future = evaluate_market_environment(
        future_included, as_of_date, _NOW, _CONFIG, _CALENDAR
    )
    result_without_future = evaluate_market_environment(
        bars[:-9], as_of_date, _NOW, _CONFIG, _CALENDAR
    )
    assert result_with_future.future_bars_filtered is True
    assert result_with_future.bars_used == result_without_future.bars_used
    assert result_with_future.score == result_without_future.score


# --- staleness(営業日ベース、BusinessCalendar) -------------------------------


def test_is_bars_stale_friday_to_monday_counts_as_one_business_day() -> None:
    friday = dt.date(2026, 8, 7)
    monday = dt.date(2026, 8, 10)
    assert _CALENDAR.business_days_between(friday, monday) == 1
    assert is_bars_stale(friday, monday, 1, _CALENDAR) is False
    assert is_bars_stale(friday, monday, 0, _CALENDAR) is True


def test_is_bars_stale_ignores_weekend_calendar_days() -> None:
    """暦日では3日ずれる金曜→月曜も、営業日では1日しかずれないため、
    暦日ベースの閾値5と営業日ベースの閾値5で判定結果が変わりうることを
    示す(暦日実装では過剰にstale判定される可能性があった不具合の回帰)。"""
    friday = dt.date(2026, 8, 7)
    monday = dt.date(2026, 8, 10)
    calendar_days = (monday - friday).days
    business_days = _CALENDAR.business_days_between(friday, monday)
    assert calendar_days == 3
    assert business_days == 1


def test_is_bars_stale_over_year_end_holidays() -> None:
    """年末年始の休場日を挟む場合、暦日ではなく実際に開いていた営業日数の
    みでカウントすること。"""
    before_year_end = dt.date(2025, 12, 26)
    after_new_year = dt.date(2026, 1, 6)
    business_days = _CALENDAR.business_days_between(before_year_end, after_new_year)
    calendar_days = (after_new_year - before_year_end).days
    assert business_days < calendar_days  # 休場日の分だけ営業日数の方が少ない
    assert is_bars_stale(before_year_end, after_new_year, business_days, _CALENDAR) is False
    assert is_bars_stale(before_year_end, after_new_year, business_days - 1, _CALENDAR) is True


def test_bars_stale_no_cap_when_within_threshold() -> None:
    bars = _bars(120)
    as_of_date = _CALENDAR.add_business_days(bars[-1].date, 1)  # 閾値5以内
    result = evaluate_market_environment(bars, as_of_date, _NOW, _CONFIG, _CALENDAR)
    assert result.bars_stale is False
    assert result.confidence == ConfidenceLevel.HIGH  # capされない


def test_bars_stale_caps_confidence_to_medium() -> None:
    bars = _bars(120)
    as_of_date = _CALENDAR.add_business_days(bars[-1].date, 10)  # 閾値5を超過
    result = evaluate_market_environment(bars, as_of_date, _NOW, _CONFIG, _CALENDAR)
    assert result.bars_stale is True
    assert REASON_MARKET_BARS_STALE in result.reason_codes
    assert result.confidence in (ConfidenceLevel.LOW, ConfidenceLevel.MEDIUM)


# --- min coverage / NOT_EVALUATED --------------------------------------------


def test_not_evaluated_when_no_bars() -> None:
    result = evaluate_market_environment([], dt.date(2026, 8, 10), _NOW, _CONFIG, _CALENDAR)
    assert result.state == MarketEnvironmentEvaluationState.NOT_EVALUATED
    assert result.score is None
    assert result.category is None
    assert result.confidence is None
    assert result.coverage == 0.0


def test_not_evaluated_when_coverage_below_minimum() -> None:
    config = _config(
        min_coverage_required=0.99, coverage_medium_threshold=0.99, coverage_high_threshold=1.0
    )
    bars = _bars(40)  # trend_structure(weight0.4)がevaluableにならず、coverage<0.99
    result = evaluate_market_environment(bars, bars[-1].date, _NOW, config, _CALENDAR)
    assert result.state == MarketEnvironmentEvaluationState.NOT_EVALUATED
    assert REASON_COVERAGE_BELOW_MINIMUM in result.reason_codes


# --- confidence / category境界 -----------------------------------------------


def test_confidence_high_when_full_coverage() -> None:
    bars = _bars(120)
    result = evaluate_market_environment(bars, bars[-1].date, _NOW, _CONFIG, _CALENDAR)
    assert result.confidence == ConfidenceLevel.HIGH


def test_category_strong_tailwind_at_boundary() -> None:
    config = _config(
        category_thresholds=EnvironmentCategoryThresholds(
            strong_tailwind=60.0, tailwind=20.0, headwind=-20.0, strong_headwind=-60.0
        )
    )
    # 強い上昇トレンドを作って高スコアを狙う
    bars = _bars(120, daily_pct=Decimal("0.01"))
    result = evaluate_market_environment(bars, bars[-1].date, _NOW, config, _CALENDAR)
    assert result.score is not None
    if result.score >= 60.0:
        assert result.category == EnvironmentCategory.STRONG_TAILWIND


def test_score_clamped_to_range() -> None:
    bars = _bars(250, daily_pct=Decimal("0.03"))
    result = evaluate_market_environment(bars, bars[-1].date, _NOW, _CONFIG, _CALENDAR)
    assert result.score is not None
    assert -100 <= result.score <= 100


# --- Entity不変条件(model_validator) ------------------------------------


def test_entity_rejects_evaluated_without_score() -> None:
    with pytest.raises(ValidationError):
        MarketEnvironmentResult(
            state=MarketEnvironmentEvaluationState.EVALUATED,
            confidence=ConfidenceLevel.HIGH,
            category=EnvironmentCategory.NEUTRAL,
            evaluated_at=_NOW,
            model_version="market_environment_v1",
        )


def test_entity_rejects_score_out_of_range() -> None:
    with pytest.raises(ValidationError):
        MarketEnvironmentResult(
            state=MarketEnvironmentEvaluationState.EVALUATED,
            score=150.0,
            category=EnvironmentCategory.STRONG_TAILWIND,
            confidence=ConfidenceLevel.HIGH,
            evaluated_at=_NOW,
            model_version="market_environment_v1",
        )


def test_entity_rejects_not_evaluated_with_nonnull_score() -> None:
    with pytest.raises(ValidationError):
        MarketEnvironmentResult(
            state=MarketEnvironmentEvaluationState.NOT_EVALUATED,
            score=10.0,
            evaluated_at=_NOW,
            model_version="market_environment_v1",
        )


def test_entity_accepts_not_evaluated_with_all_none() -> None:
    result = MarketEnvironmentResult(
        state=MarketEnvironmentEvaluationState.NOT_EVALUATED,
        evaluated_at=_NOW,
        model_version="market_environment_v1",
    )
    assert result.score is None
    assert result.category is None
    assert result.confidence is None


# --- metrics / config_values --------------------------------------------


def test_result_to_metrics_includes_model_version_and_category() -> None:
    bars = _bars(120)
    result = evaluate_market_environment(bars, bars[-1].date, _NOW, _CONFIG, _CALENDAR)
    metrics = market_environment_result_to_metrics(result)
    assert metrics["model_version"] == "market_environment_v1"
    assert metrics["category"] == result.category.value if result.category else None
    assert metrics["state"] == "EVALUATED"


def test_config_values_includes_all_weights_and_thresholds() -> None:
    values = market_environment_config_values(_CONFIG)
    assert values["model_version"] == "market_environment_v1"
    assert "component_weights" in values
    assert "category_thresholds" in values
    assert values["min_bars_ma60"] == 60
    assert values["min_bars_return_60d"] == 61
