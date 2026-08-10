"""判定精度向上機能Phase D(Market/Sector Environment Shadow)が既存の判定
ロジックへ一切影響しないことを検証する横断テスト(test_phase_b3_entry_exit_
price_isolation.pyと同じ考え方、Shadow計測原則の直接証明)。

BUY/legacy SELL/HoldingDecision/ProfitTakingの4パイプラインについて、
StockSnapshot.market_environment/sector_environment/environmentのscore/
category/confidenceを極端に変えた2通りの実行結果を比較し、それ以外の判定
結果(buy_action・company_quality_score、sell判定の種別・理由・sell_prices、
holding decisionのrecommendation_type・sell_prices、profit_takingの
recommendation_type等)が完全に同一であることを直接証明する。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import (
    AccountType,
    ConfidenceLevel,
    EnvironmentCategory,
    EnvironmentEvaluationState,
    ExecutionPlanReason,
    HoldingDecisionCategory,
    HoldingDecisionConfidenceLevel,
    MarketEnvironmentEvaluationState,
    PriceRangeEvaluationState,
    SectorEnvironmentEvaluationState,
)
from jstock_advisor.domain.entities.environment import EnvironmentResult
from jstock_advisor.domain.entities.exit_price_range import ExitPriceRangeResult
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holding_decision import (
    CompanyQualityScore,
    ComponentCoverage,
    HoldingDecisionHardGate,
    HoldingDecisionResult,
    InvestmentThesisScore,
    RiskDeductionScore,
)
from jstock_advisor.domain.entities.market_environment import MarketEnvironmentResult
from jstock_advisor.domain.entities.sector_environment import SectorEnvironmentResult
from jstock_advisor.services.buy_signal_service import BuySignalService
from jstock_advisor.services.holding_decision_notification_builder import (
    build_holding_decision_recommendation,
)
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.sell_signal_service import SellSignalService
from jstock_advisor.services.stock_snapshot_service import StockSnapshot, build_stock_snapshot

_CFG = load_config()
_NOW = dt.datetime(2026, 8, 9, tzinfo=dt.UTC)
_STOCK_CODE = "2914"
_PROVIDERS = build_mock_provider_bundle(_NOW)
_CALENDAR = BusinessCalendar.from_config(_CFG.holiday_calendar)

_NOT_EVALUATED_EXIT_PRICE_RANGE = ExitPriceRangeResult(
    state=PriceRangeEvaluationState.NOT_EVALUATED,
    current_price=Decimal("1000"),
    evaluated_at=_NOW,
    model_version="exit_price_range_v1",
)


def _market_environment_variant(score: float) -> MarketEnvironmentResult:
    return MarketEnvironmentResult(
        state=MarketEnvironmentEvaluationState.EVALUATED,
        score=score,
        category=EnvironmentCategory.STRONG_TAILWIND
        if score > 0
        else EnvironmentCategory.STRONG_HEADWIND,
        confidence=ConfidenceLevel.HIGH,
        coverage=1.0,
        evaluated_at=_NOW,
        model_version="test-fixture",
    )


def _sector_environment_variant(score: float) -> SectorEnvironmentResult:
    return SectorEnvironmentResult(
        state=SectorEnvironmentEvaluationState.EVALUATED,
        sector_etf_symbol="TEST_ETF",
        score=score,
        category=EnvironmentCategory.STRONG_TAILWIND
        if score > 0
        else EnvironmentCategory.STRONG_HEADWIND,
        confidence=ConfidenceLevel.HIGH,
        coverage=1.0,
        evaluated_at=_NOW,
        model_version="test-fixture",
    )


def _environment_variant(score: float) -> EnvironmentResult:
    return EnvironmentResult(
        state=EnvironmentEvaluationState.EVALUATED,
        score=score,
        category=EnvironmentCategory.STRONG_TAILWIND
        if score > 0
        else EnvironmentCategory.STRONG_HEADWIND,
        confidence=ConfidenceLevel.HIGH,
        coverage=1.0,
        sector_available=True,
        market_weight_used=0.6,
        sector_weight_used=0.4,
        evaluated_at=_NOW,
        model_version="test-fixture",
    )


def _snapshot_environment_variants() -> tuple[StockSnapshot, StockSnapshot]:
    base, error = build_stock_snapshot(_PROVIDERS, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert base is not None
    variant_a = dataclasses.replace(
        base,
        market_environment=_market_environment_variant(90.0),
        sector_environment=_sector_environment_variant(90.0),
        environment=_environment_variant(90.0),
    )
    variant_b = dataclasses.replace(
        base,
        market_environment=_market_environment_variant(-90.0),
        sector_environment=_sector_environment_variant(-90.0),
        environment=_environment_variant(-90.0),
    )
    return variant_a, variant_b


def _holding() -> Holding:
    return Holding(
        stock_code=_STOCK_CODE,
        stock_name="x",
        shares=100,
        average_purchase_price=Decimal("1000"),
        total_purchase_amount=Decimal("100000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


# --- BUY isolation -----------------------------------------------------------


def test_buy_signal_service_ignores_environment() -> None:
    variant_a, variant_b = _snapshot_environment_variants()
    service = BuySignalService(providers=_PROVIDERS, config=_CFG, business_calendar=_CALENDAR)

    outcome_a = service.analyze(_STOCK_CODE, _NOW, snapshot=variant_a)
    outcome_b = service.analyze(_STOCK_CODE, _NOW, snapshot=variant_b)

    assert outcome_a.recommendation is not None
    assert outcome_b.recommendation is not None
    assert outcome_a.recommendation.market_score == 90.0
    assert outcome_b.recommendation.market_score == -90.0
    # Market/Sector/Environment以外の判定結果は完全に同一。
    assert outcome_a.recommendation.buy_action == outcome_b.recommendation.buy_action
    assert (
        outcome_a.recommendation.company_quality_score
        == outcome_b.recommendation.company_quality_score
    )
    assert outcome_a.recommendation.entry_buy_price == outcome_b.recommendation.entry_buy_price
    assert outcome_a.recommendation.buy_prices == outcome_b.recommendation.buy_prices
    assert outcome_a.recommendation.reasons == outcome_b.recommendation.reasons


# --- SELL(legacy) isolation ---------------------------------------------------


def test_sell_signal_service_ignores_environment() -> None:
    variant_a, variant_b = _snapshot_environment_variants()
    service = SellSignalService(providers=_PROVIDERS, config=_CFG)
    holding = _holding()

    outcome_a = service.analyze(holding, _NOW, snapshot=variant_a)
    outcome_b = service.analyze(holding, _NOW, snapshot=variant_b)

    assert outcome_a.triggered_rule_names == outcome_b.triggered_rule_names
    assert (outcome_a.recommendation is None) == (outcome_b.recommendation is None)
    if outcome_a.recommendation is not None and outcome_b.recommendation is not None:
        assert outcome_a.recommendation.environment_score == 90.0
        assert outcome_b.recommendation.environment_score == -90.0
        assert (
            outcome_a.recommendation.recommendation_type
            == outcome_b.recommendation.recommendation_type
        )
        assert outcome_a.recommendation.sell_prices == outcome_b.recommendation.sell_prices
        assert outcome_a.recommendation.reasons == outcome_b.recommendation.reasons


# --- ProfitTaking isolation ---------------------------------------------------


def test_profit_taking_service_ignores_environment() -> None:
    variant_a, variant_b = _snapshot_environment_variants()
    service = ProfitTakingService(providers=_PROVIDERS, config=_CFG)
    holding = _holding()

    outcome_a = service.analyze(holding, _NOW, snapshot=variant_a)
    outcome_b = service.analyze(holding, _NOW, snapshot=variant_b)

    assert (outcome_a.recommendation is None) == (outcome_b.recommendation is None)
    if outcome_a.recommendation is not None and outcome_b.recommendation is not None:
        assert outcome_a.recommendation.sector_score == 90.0
        assert outcome_b.recommendation.sector_score == -90.0
        assert (
            outcome_a.recommendation.recommendation_type
            == outcome_b.recommendation.recommendation_type
        )
        assert outcome_a.recommendation.sell_prices == outcome_b.recommendation.sell_prices


# --- HoldingDecision isolation -------------------------------------------------


def _holding_decision_result() -> HoldingDecisionResult:
    q = CompanyQualityScore(score=30.0, coverage_ratio=1.0)
    i = InvestmentThesisScore(score=25.0, coverage_ratio=1.0)
    r = RiskDeductionScore(score=10.0, coverage_ratio=1.0)
    return HoldingDecisionResult(
        holding_decision_result_id="phase-d-isolation-test",
        holding_id=_STOCK_CODE,
        stock_code=_STOCK_CODE,
        evaluated_at=_NOW,
        company_quality=q,
        investment_thesis=i,
        risk_deduction=r,
        base_score=45.0,
        hard_gate=HoldingDecisionHardGate(triggered=False),
        final_score=45.0,
        display_value=45,
        category=HoldingDecisionCategory.SELL_CONSIDERATION,
        coverage=ComponentCoverage(
            overall=1.0, company_quality=1.0, investment_thesis=1.0, risk_deduction=1.0
        ),
        confidence=HoldingDecisionConfidenceLevel.HIGH,
        should_notify=True,
        scoring_model_version=1,
        runtime_config_version=1,
        execution_plan_reason=ExecutionPlanReason.NORMAL_ACTIVE,
    )


def test_holding_decision_builder_ignores_environment() -> None:
    variant_a, variant_b = _snapshot_environment_variants()
    holding = _holding()
    result = _holding_decision_result()

    rec_a = build_holding_decision_recommendation(
        holding, result, variant_a, "v1", _CFG, _NOT_EVALUATED_EXIT_PRICE_RANGE
    )
    rec_b = build_holding_decision_recommendation(
        holding, result, variant_b, "v1", _CFG, _NOT_EVALUATED_EXIT_PRICE_RANGE
    )

    assert rec_a.environment_score == 90.0
    assert rec_b.environment_score == -90.0
    # 保有判断スコア自体はHoldingDecisionResult側で既に確定済みであり、
    # Environmentによって一切変わらない。
    assert rec_a.recommendation_type == rec_b.recommendation_type
    assert rec_a.sell_prices == rec_b.sell_prices


# --- LINE通知本文への非参照(recommended_action_summary/next_review_conditions) --


def test_environment_does_not_leak_into_notification_text() -> None:
    variant_a, variant_b = _snapshot_environment_variants()
    service = BuySignalService(providers=_PROVIDERS, config=_CFG, business_calendar=_CALENDAR)

    outcome_a = service.analyze(_STOCK_CODE, _NOW, snapshot=variant_a)
    outcome_b = service.analyze(_STOCK_CODE, _NOW, snapshot=variant_b)

    assert outcome_a.recommendation is not None
    assert outcome_b.recommendation is not None
    assert (
        outcome_a.recommendation.recommended_action_summary
        == outcome_b.recommendation.recommended_action_summary
    )
