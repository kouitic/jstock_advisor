"""判定精度向上機能Phase D: Environment Composite Score(市場+セクターの
統合的外部環境)。

Marketを必須バックボーンとし、Sectorが評価可能なら加重平均、評価不能
(NOT_EVALUATED/NOT_APPLICABLE)ならMarketのみで評価を継続する(Sectorを
0点として混ぜない)。Sector欠損時はconfidenceの上限をsector_missing_
confidence_capへキャップする。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from jstock_advisor.config.models import EnvironmentCompositeConfig
from jstock_advisor.domain.entities.enums import ConfidenceLevel, EnvironmentEvaluationState
from jstock_advisor.domain.entities.environment import EnvironmentResult
from jstock_advisor.domain.entities.market_environment import MarketEnvironmentResult
from jstock_advisor.domain.entities.sector_environment import SectorEnvironmentResult
from jstock_advisor.domain.signals._environment_shared import (
    cap_confidence,
    clamp_score,
    weaker_confidence,
)
from jstock_advisor.domain.signals.market_environment import category_from_score

REASON_MARKET_UNAVAILABLE_FOR_COMPOSITE = "MARKET_UNAVAILABLE_FOR_COMPOSITE"
REASON_SECTOR_UNAVAILABLE_FOR_COMPOSITE = "SECTOR_UNAVAILABLE_FOR_COMPOSITE"


def evaluate_environment(
    market: MarketEnvironmentResult,
    sector: SectorEnvironmentResult,
    now: dt.datetime,
    config: EnvironmentCompositeConfig,
) -> EnvironmentResult:
    if market.score is None or market.confidence is None:
        return EnvironmentResult(
            state=EnvironmentEvaluationState.NOT_EVALUATED,
            reason_codes=(REASON_MARKET_UNAVAILABLE_FOR_COMPOSITE,),
            evaluated_at=now,
            model_version=config.model_version,
        )

    sector_available = sector.score is not None and sector.confidence is not None
    reason_codes: list[str] = []

    if sector_available:
        assert sector.score is not None and sector.confidence is not None  # noqa: S101
        market_weight = config.composite_weights.market
        sector_weight = config.composite_weights.sector
        score = clamp_score(market.score * market_weight + sector.score * sector_weight)
        confidence = weaker_confidence(market.confidence, sector.confidence)
        coverage = market.coverage * market_weight + sector.coverage * sector_weight
    else:
        reason_codes.append(REASON_SECTOR_UNAVAILABLE_FOR_COMPOSITE)
        market_weight = 1.0
        sector_weight = None
        score = market.score
        confidence = cap_confidence(
            market.confidence, ConfidenceLevel(config.sector_missing_confidence_cap)
        )
        coverage = market.coverage

    category = category_from_score(score, config.category_thresholds)

    return EnvironmentResult(
        state=EnvironmentEvaluationState.EVALUATED,
        score=score,
        category=category,
        confidence=confidence,
        coverage=coverage,
        sector_available=sector_available,
        market_weight_used=market_weight,
        sector_weight_used=sector_weight,
        reason_codes=tuple(reason_codes),
        evaluated_at=now,
        model_version=config.model_version,
    )


def environment_result_to_metrics(
    result: EnvironmentResult,
    market: MarketEnvironmentResult,
    sector: SectorEnvironmentResult,
) -> dict[str, Any]:
    """EnvironmentResultを、Recommendation.environment_metrics(延いては
    DecisionSnapshot.environment_metrics)へ保存する監査用dictへ変換する。
    market/sectorの個別score/confidence/coverageも監査用に保存する。"""
    return {
        "state": result.state.value,
        "category": result.category.value if result.category is not None else None,
        "score": result.score,
        "market_score": market.score,
        "market_confidence": market.confidence.value if market.confidence is not None else None,
        "market_coverage": market.coverage,
        "sector_score": sector.score,
        "sector_confidence": sector.confidence.value if sector.confidence is not None else None,
        "sector_coverage": sector.coverage,
        "sector_available": result.sector_available,
        "market_weight_used": result.market_weight_used,
        "sector_weight_used": result.sector_weight_used,
        "model_version": result.model_version,
    }


def environment_config_values(config: EnvironmentCompositeConfig) -> dict[str, object]:
    return {
        "model_version": config.model_version,
        "composite_weights": config.composite_weights.model_dump(),
        "sector_missing_confidence_cap": config.sector_missing_confidence_cap,
        "category_thresholds": config.category_thresholds.model_dump(),
    }
