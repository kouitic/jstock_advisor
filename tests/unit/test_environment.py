"""domain/signals/environment.pyのテスト(判定精度向上機能Phase D:
Environment Composite Score)。

Marketを必須バックボーンとしSectorが評価可能なら加重平均、評価不能なら
Marketのみで評価継続する(0点として混ぜない)ことと、EnvironmentResultの
Entity不変条件を検証する。

コードレビュー対応(2026-08): Environment自身のcoverage(Market/Sectorの
coverageから合成した値)がconfig.min_coverage_required未満の場合は
NOT_EVALUATEDとなること、final confidenceがMarket/Sector由来confidenceと
Environment自身のcoverageから導出したconfidenceの弱い方になることを
直接検証する。
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from jstock_advisor.config.models import (
    EnvironmentCategoryThresholds,
    EnvironmentCompositeConfig,
    EnvironmentCompositeWeights,
)
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EnvironmentCategory,
    EnvironmentEvaluationState,
    MarketEnvironmentEvaluationState,
    SectorEnvironmentEvaluationState,
)
from jstock_advisor.domain.entities.environment import EnvironmentResult
from jstock_advisor.domain.entities.market_environment import MarketEnvironmentResult
from jstock_advisor.domain.entities.sector_environment import SectorEnvironmentResult
from jstock_advisor.domain.signals.environment import (
    REASON_COVERAGE_BELOW_MINIMUM,
    REASON_MARKET_UNAVAILABLE_FOR_COMPOSITE,
    REASON_SECTOR_UNAVAILABLE_FOR_COMPOSITE,
    environment_config_values,
    environment_result_to_metrics,
    evaluate_environment,
)

_NOW = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)


def _config(**overrides: object) -> EnvironmentCompositeConfig:
    defaults: dict[str, object] = dict(
        model_version="environment_v1",
        composite_weights=EnvironmentCompositeWeights(market=0.6, sector=0.4),
        sector_missing_confidence_cap="MEDIUM",
        min_coverage_required=0.3,
        coverage_high_threshold=0.9,
        coverage_medium_threshold=0.5,
        category_thresholds=EnvironmentCategoryThresholds(
            strong_tailwind=60.0, tailwind=20.0, headwind=-20.0, strong_headwind=-60.0
        ),
    )
    defaults.update(overrides)
    return EnvironmentCompositeConfig.model_validate(defaults)


_CONFIG = _config()


def _market(
    score: float | None,
    confidence: ConfidenceLevel | None = ConfidenceLevel.HIGH,
    *,
    coverage: float = 1.0,
) -> MarketEnvironmentResult:
    if score is None:
        return MarketEnvironmentResult(
            state=MarketEnvironmentEvaluationState.NOT_EVALUATED,
            evaluated_at=_NOW,
            model_version="market_environment_v1",
        )
    return MarketEnvironmentResult(
        state=MarketEnvironmentEvaluationState.EVALUATED,
        score=score,
        category=EnvironmentCategory.NEUTRAL,
        confidence=confidence,
        coverage=coverage,
        evaluated_at=_NOW,
        model_version="market_environment_v1",
    )


def _sector(
    score: float | None,
    confidence: ConfidenceLevel | None = ConfidenceLevel.HIGH,
    *,
    not_applicable: bool = False,
    coverage: float = 1.0,
) -> SectorEnvironmentResult:
    if score is None:
        state = (
            SectorEnvironmentEvaluationState.NOT_APPLICABLE
            if not_applicable
            else SectorEnvironmentEvaluationState.NOT_EVALUATED
        )
        return SectorEnvironmentResult(
            state=state, evaluated_at=_NOW, model_version="sector_environment_v1"
        )
    return SectorEnvironmentResult(
        state=SectorEnvironmentEvaluationState.EVALUATED,
        sector_etf_symbol="SECTOR_ETF",
        score=score,
        category=EnvironmentCategory.NEUTRAL,
        confidence=confidence,
        coverage=coverage,
        evaluated_at=_NOW,
        model_version="sector_environment_v1",
    )


# --- market+sector両方評価可能 -----------------------------------------------


def test_weighted_average_when_both_evaluated() -> None:
    market = _market(40.0)
    sector = _sector(10.0)
    result = evaluate_environment(market, sector, _NOW, _CONFIG)
    assert result.state == EnvironmentEvaluationState.EVALUATED
    assert result.score == pytest.approx(40.0 * 0.6 + 10.0 * 0.4)
    assert result.sector_available is True
    assert result.market_weight_used == 0.6
    assert result.sector_weight_used == 0.4


def test_confidence_is_weaker_of_market_and_sector() -> None:
    market = _market(40.0, confidence=ConfidenceLevel.HIGH)
    sector = _sector(10.0, confidence=ConfidenceLevel.LOW)
    result = evaluate_environment(market, sector, _NOW, _CONFIG)
    assert result.confidence == ConfidenceLevel.LOW


# --- marketのみ(sector欠損) -------------------------------------------------


def test_market_only_when_sector_not_evaluated() -> None:
    market = _market(40.0)
    sector = _sector(None)
    result = evaluate_environment(market, sector, _NOW, _CONFIG)
    assert result.state == EnvironmentEvaluationState.EVALUATED
    assert result.score == 40.0
    assert result.sector_available is False
    assert result.market_weight_used == 1.0
    assert result.sector_weight_used is None
    assert REASON_SECTOR_UNAVAILABLE_FOR_COMPOSITE in result.reason_codes


def test_market_only_when_sector_not_applicable() -> None:
    market = _market(40.0)
    sector = _sector(None, not_applicable=True)
    result = evaluate_environment(market, sector, _NOW, _CONFIG)
    assert result.score == 40.0
    assert result.sector_available is False


def test_sector_missing_confidence_capped_to_medium() -> None:
    market = _market(40.0, confidence=ConfidenceLevel.HIGH)
    sector = _sector(None)
    result = evaluate_environment(market, sector, _NOW, _CONFIG)
    assert result.confidence == ConfidenceLevel.MEDIUM


def test_sector_missing_does_not_upgrade_low_confidence() -> None:
    market = _market(40.0, confidence=ConfidenceLevel.LOW)
    sector = _sector(None)
    result = evaluate_environment(market, sector, _NOW, _CONFIG)
    assert result.confidence == ConfidenceLevel.LOW


# --- market欠損 --------------------------------------------------------------


def test_not_evaluated_when_market_unavailable() -> None:
    market = _market(None)
    sector = _sector(10.0)
    result = evaluate_environment(market, sector, _NOW, _CONFIG)
    assert result.state == EnvironmentEvaluationState.NOT_EVALUATED
    assert result.score is None
    assert REASON_MARKET_UNAVAILABLE_FOR_COMPOSITE in result.reason_codes


# --- missingを0点扱いしない --------------------------------------------------


def test_sector_missing_score_not_treated_as_zero() -> None:
    market = _market(-40.0)
    sector_missing = _sector(None)
    sector_zero = _sector(0.0)
    result_missing = evaluate_environment(market, sector_missing, _NOW, _CONFIG)
    result_zero = evaluate_environment(market, sector_zero, _NOW, _CONFIG)
    assert result_missing.score == -40.0  # marketのみ
    assert result_zero.score == pytest.approx(-40.0 * 0.6 + 0.0 * 0.4)  # 混合平均
    assert result_missing.score != result_zero.score


# --- score/category境界 ------------------------------------------------------


def test_category_boundary_strong_tailwind() -> None:
    market = _market(100.0)
    sector = _sector(100.0)
    result = evaluate_environment(market, sector, _NOW, _CONFIG)
    assert result.score == 100.0
    assert result.category == EnvironmentCategory.STRONG_TAILWIND


# --- Environment自身のcoverage閾値(コードレビュー対応) ---------------------


def test_weighted_coverage_when_sector_available() -> None:
    market = _market(40.0, coverage=0.8)
    sector = _sector(10.0, coverage=0.6)
    result = evaluate_environment(market, sector, _NOW, _CONFIG)
    assert result.coverage == pytest.approx(0.8 * 0.6 + 0.6 * 0.4)


def test_market_only_coverage_uses_market_coverage_directly() -> None:
    market = _market(40.0, coverage=0.7)
    sector = _sector(None)
    result = evaluate_environment(market, sector, _NOW, _CONFIG)
    assert result.coverage == pytest.approx(0.7)


def test_coverage_high_medium_low_classification() -> None:
    # coverage=1.0 → HIGH(>=coverage_high_threshold=0.9)
    result_high = evaluate_environment(_market(40.0, coverage=1.0), _sector(None), _NOW, _CONFIG)
    assert result_high.coverage == pytest.approx(1.0)
    # market HIGH + sector HIGH の入力でも、coverageがMEDIUM帯(0.5<=x<0.9)なら
    # final confidenceはMEDIUMに引き下げられる(coverage_confidenceが律速)。
    market_medium_cov = _market(40.0, confidence=ConfidenceLevel.HIGH, coverage=0.5)
    sector_medium_cov = _sector(10.0, confidence=ConfidenceLevel.HIGH, coverage=0.5)
    result_medium = evaluate_environment(market_medium_cov, sector_medium_cov, _NOW, _CONFIG)
    assert result_medium.coverage == pytest.approx(0.5)
    assert result_medium.confidence == ConfidenceLevel.MEDIUM
    # coverageがLOW帯(<0.5)でもmin_coverage_required(0.3)以上ならEVALUATED継続、
    # confidenceはLOWまで下がる。
    market_low_cov = _market(40.0, confidence=ConfidenceLevel.HIGH, coverage=0.35)
    sector_low_cov = _sector(10.0, confidence=ConfidenceLevel.HIGH, coverage=0.35)
    result_low = evaluate_environment(market_low_cov, sector_low_cov, _NOW, _CONFIG)
    assert result_low.state == EnvironmentEvaluationState.EVALUATED
    assert result_low.confidence == ConfidenceLevel.LOW


def test_market_high_sector_high_coverage_medium_yields_final_medium() -> None:
    """market HIGH / sector HIGH の入力でも、Environment自身のcoverageが
    MEDIUM帯であれば最終confidenceはMEDIUMになる(3つの信頼度情報のうち
    最も弱いものを採用する設計、コードレビュー対応)。"""
    market = _market(40.0, confidence=ConfidenceLevel.HIGH, coverage=0.6)
    sector = _sector(10.0, confidence=ConfidenceLevel.HIGH, coverage=0.6)
    result = evaluate_environment(market, sector, _NOW, _CONFIG)
    assert result.confidence == ConfidenceLevel.MEDIUM


def test_coverage_below_minimum_environment_not_evaluated() -> None:
    market = _market(40.0, coverage=0.1)
    sector = _sector(None)
    result = evaluate_environment(market, sector, _NOW, _CONFIG)
    assert result.state == EnvironmentEvaluationState.NOT_EVALUATED
    assert result.score is None
    assert result.category is None
    assert result.confidence is None
    assert result.coverage == pytest.approx(0.1)
    assert REASON_COVERAGE_BELOW_MINIMUM in result.reason_codes


def test_coverage_below_minimum_when_sector_available_but_weighted_low() -> None:
    market = _market(40.0, coverage=0.2)
    sector = _sector(10.0, coverage=0.2)
    result = evaluate_environment(market, sector, _NOW, _CONFIG)
    assert result.coverage == pytest.approx(0.2)
    assert result.state == EnvironmentEvaluationState.NOT_EVALUATED


# --- Entity不変条件 -----------------------------------------------------------


def test_entity_rejects_evaluated_without_market_weight() -> None:
    with pytest.raises(ValidationError):
        EnvironmentResult(
            state=EnvironmentEvaluationState.EVALUATED,
            score=10.0,
            category=EnvironmentCategory.NEUTRAL,
            confidence=ConfidenceLevel.HIGH,
            sector_available=False,
            market_weight_used=None,
            evaluated_at=_NOW,
            model_version="environment_v1",
        )


def test_entity_rejects_sector_available_without_sector_weight() -> None:
    with pytest.raises(ValidationError):
        EnvironmentResult(
            state=EnvironmentEvaluationState.EVALUATED,
            score=10.0,
            category=EnvironmentCategory.NEUTRAL,
            confidence=ConfidenceLevel.HIGH,
            sector_available=True,
            market_weight_used=0.6,
            sector_weight_used=None,
            evaluated_at=_NOW,
            model_version="environment_v1",
        )


def test_entity_rejects_not_evaluated_with_sector_available_true() -> None:
    with pytest.raises(ValidationError):
        EnvironmentResult(
            state=EnvironmentEvaluationState.NOT_EVALUATED,
            sector_available=True,
            evaluated_at=_NOW,
            model_version="environment_v1",
        )


def test_entity_accepts_not_evaluated_with_all_none() -> None:
    result = EnvironmentResult(
        state=EnvironmentEvaluationState.NOT_EVALUATED,
        evaluated_at=_NOW,
        model_version="environment_v1",
    )
    assert result.score is None
    assert result.sector_available is False


# --- metrics / config_values -------------------------------------------------


def test_result_to_metrics_includes_market_and_sector_scores() -> None:
    market = _market(40.0)
    sector = _sector(10.0)
    result = evaluate_environment(market, sector, _NOW, _CONFIG)
    metrics = environment_result_to_metrics(result, market, sector)
    assert metrics["market_score"] == 40.0
    assert metrics["sector_score"] == 10.0
    assert metrics["sector_available"] is True


def test_config_values_includes_composite_weights() -> None:
    values = environment_config_values(_CONFIG)
    assert values["composite_weights"] == {"market": 0.6, "sector": 0.4}
    assert values["sector_missing_confidence_cap"] == "MEDIUM"
    assert values["min_coverage_required"] == 0.3
    assert values["coverage_high_threshold"] == 0.9
    assert values["coverage_medium_threshold"] == 0.5
