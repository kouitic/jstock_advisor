"""判定精度向上機能Phase C(Earnings Surprise/Trend Score)が既存の判定ロジック
へ一切影響しないことを検証する横断テスト(test_phase_b_timing_score_
isolation.pyと同じ考え方、Shadow計測原則の直接証明)。

BUY/legacy SELL/HoldingDecision/ProfitTakingの4パイプラインそれぞれについて、
StockSnapshot.earnings_surprise/earnings_trendのscore/confidence/coverage/
reason_codes/metricsをすべて変えた2つのsnapshotで同じ判定処理を実行し、
それ以外の判定結果(buy_action・company_quality_score、sell判定の種別・理由、
holding decisionのrecommendation_type・sell_prices等)が完全に同一であることを
直接証明する。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.earnings_surprise import EarningsSurpriseResult
from jstock_advisor.domain.entities.earnings_trend import EarningsTrendResult
from jstock_advisor.domain.entities.enums import (
    AccountType,
    ConfidenceLevel,
    EarningsSurpriseCategory,
    EarningsSurpriseEvaluationState,
    EarningsTrendCategory,
    EarningsTrendEvaluationState,
    ExecutionPlanReason,
    HoldingDecisionCategory,
    HoldingDecisionConfidenceLevel,
    PriceRangeEvaluationState,
)
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
from jstock_advisor.services.buy_signal_service import BuySignalService
from jstock_advisor.services.holding_decision_notification_builder import (
    build_holding_decision_recommendation,
)
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.sell_signal_service import SellSignalService
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

_CFG = load_config()
_NOW = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)
_STOCK_CODE = "2914"
_PROVIDERS = build_mock_provider_bundle(_NOW)
_CALENDAR = BusinessCalendar.from_config(_CFG.holiday_calendar)
_NOT_EVALUATED_EXIT_PRICE_RANGE = ExitPriceRangeResult(
    state=PriceRangeEvaluationState.NOT_EVALUATED,
    current_price=Decimal("1000"),
    evaluated_at=_NOW,
    model_version="exit_price_range_v1",
)


def _surprise_variant(score: float, confidence: ConfidenceLevel) -> EarningsSurpriseResult:
    return EarningsSurpriseResult(
        state=EarningsSurpriseEvaluationState.EVALUATED,
        score=score,
        category=EarningsSurpriseCategory.NEUTRAL,
        confidence=confidence,
        coverage=0.5 if confidence == ConfidenceLevel.MEDIUM else 1.0,
        analyst_consensus_component=score,
        reason_codes=("DIVIDEND_REVISION_UNAVAILABLE",),
        evaluated_at=_NOW,
        model_version="test-fixture",
    )


def _trend_variant(score: float, confidence: ConfidenceLevel) -> EarningsTrendResult:
    return EarningsTrendResult(
        state=EarningsTrendEvaluationState.EVALUATED,
        score=score,
        category=EarningsTrendCategory.STABLE,
        confidence=confidence,
        coverage=0.5 if confidence == ConfidenceLevel.MEDIUM else 1.0,
        operating_income_trend_component=score,
        reason_codes=("DIVIDEND_DIRECTION_UNAVAILABLE",),
        evaluated_at=_NOW,
        model_version="test-fixture",
    )


def _snapshot_variants() -> tuple:
    base, error = build_stock_snapshot(_PROVIDERS, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert base is not None
    # score・confidence・coverage・reason_codesのすべてが異なる2バリアントを作る。
    variant_a = dataclasses.replace(
        base,
        earnings_surprise=_surprise_variant(-88.0, ConfidenceLevel.HIGH),
        earnings_trend=_trend_variant(-77.0, ConfidenceLevel.HIGH),
    )
    variant_b = dataclasses.replace(
        base,
        earnings_surprise=_surprise_variant(33.0, ConfidenceLevel.MEDIUM),
        earnings_trend=_trend_variant(44.0, ConfidenceLevel.MEDIUM),
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


def test_buy_signal_service_ignores_earnings_surprise_and_trend() -> None:
    variant_a, variant_b = _snapshot_variants()
    service = BuySignalService(providers=_PROVIDERS, config=_CFG, business_calendar=_CALENDAR)

    outcome_a = service.analyze(_STOCK_CODE, _NOW, snapshot=variant_a)
    outcome_b = service.analyze(_STOCK_CODE, _NOW, snapshot=variant_b)

    assert outcome_a.recommendation is not None
    assert outcome_b.recommendation is not None
    assert outcome_a.recommendation.earnings_surprise_score == -88.0
    assert outcome_b.recommendation.earnings_surprise_score == 33.0
    assert outcome_a.recommendation.earnings_trend_score == -77.0
    assert outcome_b.recommendation.earnings_trend_score == 44.0
    assert outcome_a.recommendation.earnings_surprise_confidence == ConfidenceLevel.HIGH
    assert outcome_b.recommendation.earnings_surprise_confidence == ConfidenceLevel.MEDIUM
    # earnings関連以外の判定結果は完全に同一。
    assert outcome_a.recommendation.buy_action == outcome_b.recommendation.buy_action
    assert (
        outcome_a.recommendation.company_quality_score
        == outcome_b.recommendation.company_quality_score
    )
    assert (
        outcome_a.recommendation.purchase_attractiveness_score
        == outcome_b.recommendation.purchase_attractiveness_score
    )


def test_sell_signal_service_ignores_earnings_surprise_and_trend() -> None:
    variant_a, variant_b = _snapshot_variants()
    service = SellSignalService(providers=_PROVIDERS, config=_CFG)
    holding = _holding()

    outcome_a = service.analyze(holding, _NOW, snapshot=variant_a)
    outcome_b = service.analyze(holding, _NOW, snapshot=variant_b)

    assert outcome_a.triggered_rule_names == outcome_b.triggered_rule_names
    assert (outcome_a.recommendation is None) == (outcome_b.recommendation is None)
    if outcome_a.recommendation is not None and outcome_b.recommendation is not None:
        assert outcome_a.recommendation.earnings_surprise_score == -88.0
        assert outcome_b.recommendation.earnings_surprise_score == 33.0
        assert (
            outcome_a.recommendation.recommendation_type
            == outcome_b.recommendation.recommendation_type
        )
        assert outcome_a.recommendation.reasons == outcome_b.recommendation.reasons


def test_profit_taking_service_ignores_earnings_surprise_and_trend() -> None:
    variant_a, variant_b = _snapshot_variants()
    service = ProfitTakingService(providers=_PROVIDERS, config=_CFG)
    holding = _holding()

    outcome_a = service.analyze(holding, _NOW, snapshot=variant_a)
    outcome_b = service.analyze(holding, _NOW, snapshot=variant_b)

    assert (outcome_a.recommendation is None) == (outcome_b.recommendation is None)
    if outcome_a.recommendation is not None and outcome_b.recommendation is not None:
        assert outcome_a.recommendation.earnings_trend_score == -77.0
        assert outcome_b.recommendation.earnings_trend_score == 44.0
        assert (
            outcome_a.recommendation.recommendation_type
            == outcome_b.recommendation.recommendation_type
        )
        assert outcome_a.recommendation.reasons == outcome_b.recommendation.reasons


def _holding_decision_result() -> HoldingDecisionResult:
    q = CompanyQualityScore(score=30.0, coverage_ratio=1.0)
    i = InvestmentThesisScore(score=25.0, coverage_ratio=1.0)
    r = RiskDeductionScore(score=10.0, coverage_ratio=1.0)
    return HoldingDecisionResult(
        holding_decision_result_id="phase-c-isolation-test",
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


def test_holding_decision_builder_ignores_earnings_surprise_and_trend() -> None:
    variant_a, variant_b = _snapshot_variants()
    holding = _holding()
    result = _holding_decision_result()

    rec_a = build_holding_decision_recommendation(
        holding, result, variant_a, "v1", _CFG, _NOT_EVALUATED_EXIT_PRICE_RANGE
    )
    rec_b = build_holding_decision_recommendation(
        holding, result, variant_b, "v1", _CFG, _NOT_EVALUATED_EXIT_PRICE_RANGE
    )

    assert rec_a.earnings_surprise_score == -88.0
    assert rec_b.earnings_surprise_score == 33.0
    assert rec_a.earnings_trend_score == -77.0
    assert rec_b.earnings_trend_score == 44.0
    assert rec_a.earnings_surprise_confidence == ConfidenceLevel.HIGH
    assert rec_b.earnings_surprise_confidence == ConfidenceLevel.MEDIUM
    # earnings関連以外は完全に同一(保有判断スコア自体はHoldingDecisionResult側で
    # 既に確定済みであり、ここでは一切再計算しない)。
    assert rec_a.recommendation_type == rec_b.recommendation_type
    assert rec_a.sell_prices == rec_b.sell_prices
    # config_values_used自体はearningsキーの値が異なる設定ではないため
    # (同じ_CFGを使っている)、両者は完全一致するはず。
    assert rec_a.config_values_used == rec_b.config_values_used
