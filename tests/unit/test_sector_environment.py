"""domain/signals/sector_environment.pyのテスト(判定精度向上機能Phase D:
Sector Environment Score)。

sector_etf_map未登録時のNOT_APPLICABLE、trend_structure/medium_term_return/
relative_strength(セクター vs TOPIX)3成分の算出、SectorEnvironmentResultの
Entity不変条件を検証する。

コードレビュー対応(2026-08): NOT_APPLICABLE(業種自体が評価対象外)と
NOT_EVALUATED(mapping済みだがデータ不足/取得失敗)を明確に区別すること、
bar staleness判定がBusinessCalendarによる営業日カウントで行われること、
min_bars_return_60dが60d return/relative_strength_60dの実際の評価条件と
して機能することを直接検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import (
    EnvironmentCategoryThresholds,
    SectorEnvironmentComponentWeights,
    SectorEnvironmentConfig,
    TrendClassificationScoreConfig,
)
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EnvironmentCategory,
    SectorEnvironmentEvaluationState,
)
from jstock_advisor.domain.entities.sector_environment import SectorEnvironmentResult
from jstock_advisor.domain.signals.sector_environment import (
    REASON_RELATIVE_STRENGTH_UNAVAILABLE,
    REASON_SECTOR_DATA_UNAVAILABLE,
    REASON_SECTOR_ETF_NOT_MAPPED,
    evaluate_sector_environment,
    sector_environment_config_values,
    sector_environment_result_to_metrics,
)
from jstock_advisor.interfaces.types import PriceBar

_NOW = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)
_CALENDAR = BusinessCalendar.from_config(load_config().holiday_calendar)


def _config(**overrides: object) -> SectorEnvironmentConfig:
    defaults: dict[str, object] = dict(
        model_version="sector_environment_v1",
        component_weights=SectorEnvironmentComponentWeights(
            trend_structure=0.3, medium_term_return=0.3, relative_strength=0.4
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
        relative_strength_scale_pct=10.0,
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
    return SectorEnvironmentConfig.model_validate(defaults)


_CONFIG = _config()


def _bars(
    n: int,
    *,
    start_price: Decimal = Decimal("1000"),
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


# --- sector mapping有無(NOT_APPLICABLE vs NOT_EVALUATED) --------------------


def test_not_applicable_when_no_etf_mapped() -> None:
    """A. sector_etf_symbol is None → NOT_APPLICABLE(業種自体が対象外)。"""
    topix_bars = _bars(120)
    result = evaluate_sector_environment(
        None, topix_bars, None, topix_bars[-1].date, _NOW, _CONFIG, _CALENDAR
    )
    assert result.state == SectorEnvironmentEvaluationState.NOT_APPLICABLE
    assert result.score is None
    assert result.category is None
    assert result.confidence is None
    assert result.coverage == 0.0
    assert result.reason_codes == (REASON_SECTOR_ETF_NOT_MAPPED,)


def test_not_evaluated_when_mapped_but_sector_bars_empty() -> None:
    """B. sector_etf_symbol is not None かつ sector_barsが空 → NOT_EVALUATED
    (評価対象ではあるがデータ取得できず今回は評価不能)。"""
    topix_bars = _bars(120)
    result = evaluate_sector_environment(
        [], topix_bars, "SECTOR_ETF", topix_bars[-1].date, _NOW, _CONFIG, _CALENDAR
    )
    assert result.state == SectorEnvironmentEvaluationState.NOT_EVALUATED
    assert result.sector_etf_symbol == "SECTOR_ETF"
    assert result.score is None
    assert result.category is None
    assert result.confidence is None
    assert result.coverage == 0.0
    assert result.reason_codes == (REASON_SECTOR_DATA_UNAVAILABLE,)


def test_not_evaluated_when_mapped_but_all_bars_are_future() -> None:
    """C. sector_etf_symbolあり・sector_barsあり、ただしfuture bar除外後に
    effective_sector_barsが空 → NOT_EVALUATED/SECTOR_DATA_UNAVAILABLE。"""
    topix_bars = _bars(120)
    future_only_bars = _bars(10, start_price=Decimal("500"), start_date=dt.date(2027, 1, 1))
    as_of_date = dt.date(2026, 1, 1)  # future_only_barsは全てこれより未来
    result = evaluate_sector_environment(
        future_only_bars, topix_bars, "SECTOR_ETF", as_of_date, _NOW, _CONFIG, _CALENDAR
    )
    assert result.state == SectorEnvironmentEvaluationState.NOT_EVALUATED
    assert result.score is None
    assert result.category is None
    assert result.confidence is None
    assert result.coverage == 0.0
    assert REASON_SECTOR_DATA_UNAVAILABLE in result.reason_codes


def test_evaluated_when_etf_mapped_and_sufficient_bars() -> None:
    topix_bars = _bars(120)
    sector_bars = _bars(120, start_price=Decimal("500"))
    result = evaluate_sector_environment(
        sector_bars, topix_bars, "SECTOR_ETF", topix_bars[-1].date, _NOW, _CONFIG, _CALENDAR
    )
    assert result.state == SectorEnvironmentEvaluationState.EVALUATED
    assert result.sector_etf_symbol == "SECTOR_ETF"
    assert result.score is not None


# --- sector bars不足 ------------------------------------------------------


def test_sector_bars_insufficient_reduces_coverage() -> None:
    topix_bars = _bars(120)
    sector_bars = _bars(10, start_price=Decimal("500"))
    result = evaluate_sector_environment(
        sector_bars, topix_bars, "SECTOR_ETF", topix_bars[-1].date, _NOW, _CONFIG, _CALENDAR
    )
    assert result.coverage < 1.0


# --- min_bars_return_60dが実際の評価条件として使われること -------------------


def test_min_bars_return_60d_excludes_only_60d_return_and_relative_strength() -> None:
    topix_bars = _bars(65, daily_pct=Decimal("0.002"))
    sector_bars = _bars(65, start_price=Decimal("500"), daily_pct=Decimal("0.001"))
    strict_config = _config(min_bars_return_60d=100)  # 65本では60d側を満たせない
    result = evaluate_sector_environment(
        sector_bars, topix_bars, "SECTOR_ETF", topix_bars[-1].date, _NOW, strict_config, _CALENDAR
    )
    assert result.return_20d_pct is not None
    assert result.return_60d_pct is None
    assert result.medium_term_return_component is not None  # 20dのみで部分評価継続
    assert result.relative_strength_20d_pct is not None
    assert result.relative_strength_60d_pct is None


# --- relative strength: 市場vsセクター ------------------------------------


def test_relative_strength_negative_when_sector_underperforms() -> None:
    topix_bars = _bars(120, daily_pct=Decimal("0.005"))
    sector_bars = _bars(120, start_price=Decimal("500"), daily_pct=Decimal("0.001"))
    result = evaluate_sector_environment(
        sector_bars, topix_bars, "SECTOR_ETF", topix_bars[-1].date, _NOW, _CONFIG, _CALENDAR
    )
    assert result.relative_strength_20d_pct is not None
    assert result.relative_strength_20d_pct < 0
    assert result.relative_strength_component is not None
    assert result.relative_strength_component < 0


def test_relative_strength_positive_when_market_down_sector_relatively_strong() -> None:
    topix_bars = _bars(120, daily_pct=Decimal("-0.005"))
    sector_bars = _bars(120, start_price=Decimal("500"), daily_pct=Decimal("-0.001"))
    result = evaluate_sector_environment(
        sector_bars, topix_bars, "SECTOR_ETF", topix_bars[-1].date, _NOW, _CONFIG, _CALENDAR
    )
    assert result.relative_strength_20d_pct is not None
    assert result.relative_strength_20d_pct > 0
    assert result.relative_strength_component is not None
    assert result.relative_strength_component > 0


def test_relative_strength_unavailable_when_topix_bars_insufficient() -> None:
    topix_bars = _bars(5)
    sector_bars = _bars(120, start_price=Decimal("500"))
    result = evaluate_sector_environment(
        sector_bars, topix_bars, "SECTOR_ETF", sector_bars[-1].date, _NOW, _CONFIG, _CALENDAR
    )
    assert result.relative_strength_component is None
    assert REASON_RELATIVE_STRENGTH_UNAVAILABLE in result.reason_codes


# --- future bar除外 --------------------------------------------------------


def test_future_bars_are_excluded_from_computation() -> None:
    topix_bars = _bars(120)
    sector_bars = _bars(120, start_price=Decimal("500"))
    as_of_date = sector_bars[-10].date
    result_with_future = evaluate_sector_environment(
        sector_bars, topix_bars, "SECTOR_ETF", as_of_date, _NOW, _CONFIG, _CALENDAR
    )
    result_without_future = evaluate_sector_environment(
        sector_bars[:-9], topix_bars[:-9], "SECTOR_ETF", as_of_date, _NOW, _CONFIG, _CALENDAR
    )
    assert result_with_future.future_bars_filtered is True
    assert result_with_future.bars_used == result_without_future.bars_used
    assert result_with_future.score == result_without_future.score


# --- staleness(営業日ベース、Marketと同じ判定になること) --------------------


def test_bars_stale_matches_market_environment_business_day_logic() -> None:
    topix_bars = _bars(120)
    sector_bars = _bars(120, start_price=Decimal("500"))
    as_of_date = _CALENDAR.add_business_days(sector_bars[-1].date, 10)  # 閾値5を超過
    result = evaluate_sector_environment(
        sector_bars, topix_bars, "SECTOR_ETF", as_of_date, _NOW, _CONFIG, _CALENDAR
    )
    assert result.bars_stale is True
    assert result.confidence in (ConfidenceLevel.LOW, ConfidenceLevel.MEDIUM)


# --- coverage/confidence/category -----------------------------------------


def test_confidence_high_when_full_coverage() -> None:
    topix_bars = _bars(120)
    sector_bars = _bars(120, start_price=Decimal("500"))
    result = evaluate_sector_environment(
        sector_bars, topix_bars, "SECTOR_ETF", topix_bars[-1].date, _NOW, _CONFIG, _CALENDAR
    )
    assert result.confidence == ConfidenceLevel.HIGH


def test_category_reflects_score_sign() -> None:
    topix_bars = _bars(120, daily_pct=Decimal("-0.01"))
    sector_bars = _bars(120, start_price=Decimal("500"), daily_pct=Decimal("-0.03"))
    result = evaluate_sector_environment(
        sector_bars, topix_bars, "SECTOR_ETF", topix_bars[-1].date, _NOW, _CONFIG, _CALENDAR
    )
    assert result.score is not None
    if result.score <= -60.0:
        assert result.category == EnvironmentCategory.STRONG_HEADWIND


# --- Entity不変条件 ---------------------------------------------------------


def test_entity_rejects_evaluated_without_sector_etf_symbol() -> None:
    with pytest.raises(ValidationError):
        SectorEnvironmentResult(
            state=SectorEnvironmentEvaluationState.EVALUATED,
            sector_etf_symbol=None,
            score=10.0,
            category=EnvironmentCategory.NEUTRAL,
            confidence=ConfidenceLevel.HIGH,
            evaluated_at=_NOW,
            model_version="sector_environment_v1",
        )


def test_entity_rejects_not_applicable_with_nonnull_score() -> None:
    with pytest.raises(ValidationError):
        SectorEnvironmentResult(
            state=SectorEnvironmentEvaluationState.NOT_APPLICABLE,
            score=10.0,
            evaluated_at=_NOW,
            model_version="sector_environment_v1",
        )


def test_entity_accepts_not_applicable_with_sector_etf_symbol_none() -> None:
    result = SectorEnvironmentResult(
        state=SectorEnvironmentEvaluationState.NOT_APPLICABLE,
        sector_etf_symbol=None,
        evaluated_at=_NOW,
        model_version="sector_environment_v1",
    )
    assert result.score is None


def test_entity_accepts_not_evaluated_with_sector_etf_symbol_set() -> None:
    """NOT_EVALUATED(mapping済みだがデータ不足)ではsector_etf_symbolが
    非Noneのままであってよい(NOT_APPLICABLEとは異なり、対象業種自体は
    特定できているため)。"""
    result = SectorEnvironmentResult(
        state=SectorEnvironmentEvaluationState.NOT_EVALUATED,
        sector_etf_symbol="SECTOR_ETF",
        evaluated_at=_NOW,
        model_version="sector_environment_v1",
    )
    assert result.score is None
    assert result.sector_etf_symbol == "SECTOR_ETF"


# --- metrics / config_values ------------------------------------------------


def test_result_to_metrics_includes_sector_etf_symbol() -> None:
    topix_bars = _bars(120)
    sector_bars = _bars(120, start_price=Decimal("500"))
    result = evaluate_sector_environment(
        sector_bars, topix_bars, "SECTOR_ETF", topix_bars[-1].date, _NOW, _CONFIG, _CALENDAR
    )
    metrics = sector_environment_result_to_metrics(result)
    assert metrics["sector_etf_symbol"] == "SECTOR_ETF"
    assert metrics["model_version"] == "sector_environment_v1"


def test_config_values_includes_relative_strength_scale() -> None:
    values = sector_environment_config_values(_CONFIG)
    assert values["relative_strength_scale_pct"] == 10.0
    assert values["min_bars_return_60d"] == 61
