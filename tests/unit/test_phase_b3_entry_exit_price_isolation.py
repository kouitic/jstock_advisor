"""判定精度向上機能次フェーズSTEP2(Entry/Exit Price Range Shadow)が既存の
判定ロジックへ一切影響しないことを検証する横断テスト(test_phase_b_
historical_valuation_isolation.pyと同じ考え方、Shadow計測原則の直接証明)。

BUY/legacy SELL/HoldingDecision/ProfitTakingの4パイプラインについて、
Entry Price Range(StockSnapshot.entry_price_range)・Exit Price Range
(各パイプラインが内部で算出する値、entry_exit_price.exit設定を変えることで
出力を変える)をそれぞれ変えた2通りの実行結果を比較し、それ以外の判定結果
(buy_action・company_quality_score、sell判定の種別・理由、holding decisionの
recommendation_type・sell_prices、profit_takingのrecommendation_type等)が
完全に同一であることを直接証明する。

また、average_purchase_priceの変更がExit Price Rangeのfair value由来の
3価格(partial_low/high・strong)に一切影響しないこと(domain/signals/
exit_price_range.pyのpure関数レベルではtest_exit_price_range.pyで既に
証明済み)を、実際のSellSignalService経由(統合レベル)でも再確認する。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.entry_price_range import EntryPriceRangeResult
from jstock_advisor.domain.entities.enums import (
    AccountType,
    ConfidenceLevel,
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
_NOW = dt.datetime(2026, 8, 9, tzinfo=dt.UTC)
_STOCK_CODE = "2914"
_PROVIDERS = build_mock_provider_bundle(_NOW)
_CALENDAR = BusinessCalendar.from_config(_CFG.holiday_calendar)

# entry_exit_price.exitの設定だけを変えたAppConfig variant(それ以外は_CFGと
# 完全に同一)。ExitPriceRangeの出力だけを変えるための最小限の差分。
_CFG_EXIT_VARIANT = _CFG.model_copy(
    update={
        "entry_exit_price": _CFG.entry_exit_price.model_copy(
            update={
                "exit": _CFG.entry_exit_price.exit.model_copy(
                    update={
                        "loss_tolerance_fraction": 0.25,
                        "review_return_threshold_fraction": 0.30,
                    }
                )
            }
        )
    }
)


def _entry_price_range_variant(starter_price: Decimal) -> EntryPriceRangeResult:
    return EntryPriceRangeResult(
        state=PriceRangeEvaluationState.EVALUATED,
        current_price=Decimal("1000"),
        valuation_ceiling=Decimal("1300"),
        starter_entry_price=starter_price,
        preferred_entry_price=starter_price - Decimal("50"),
        strong_entry_price=starter_price - Decimal("150"),
        max_entry_price=Decimal("1250"),
        confidence=ConfidenceLevel.MEDIUM,
        evaluated_at=_NOW,
        model_version="test-fixture",
    )


_NOT_EVALUATED_EXIT_PRICE_RANGE = ExitPriceRangeResult(
    state=PriceRangeEvaluationState.NOT_EVALUATED,
    current_price=Decimal("1000"),
    evaluated_at=_NOW,
    model_version="exit_price_range_v1",
)


def _snapshot_entry_variants() -> tuple:
    base, error = build_stock_snapshot(_PROVIDERS, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert base is not None
    variant_a = dataclasses.replace(
        base, entry_price_range=_entry_price_range_variant(Decimal("1100"))
    )
    variant_b = dataclasses.replace(
        base, entry_price_range=_entry_price_range_variant(Decimal("900"))
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


# --- Entry Price Range isolation --------------------------------------------


def test_buy_signal_service_ignores_entry_price_range() -> None:
    variant_a, variant_b = _snapshot_entry_variants()
    service = BuySignalService(providers=_PROVIDERS, config=_CFG, business_calendar=_CALENDAR)

    outcome_a = service.analyze(_STOCK_CODE, _NOW, snapshot=variant_a)
    outcome_b = service.analyze(_STOCK_CODE, _NOW, snapshot=variant_b)

    assert outcome_a.recommendation is not None
    assert outcome_b.recommendation is not None
    assert outcome_a.recommendation.entry_price_range_starter_price == Decimal("1100")
    assert outcome_b.recommendation.entry_price_range_starter_price == Decimal("900")
    # Entry Price Range以外の判定結果は完全に同一。
    assert outcome_a.recommendation.buy_action == outcome_b.recommendation.buy_action
    assert (
        outcome_a.recommendation.company_quality_score
        == outcome_b.recommendation.company_quality_score
    )
    assert outcome_a.recommendation.entry_buy_price == outcome_b.recommendation.entry_buy_price
    assert (
        outcome_a.recommendation.standard_buy_price == outcome_b.recommendation.standard_buy_price
    )
    assert outcome_a.recommendation.buy_prices == outcome_b.recommendation.buy_prices


def test_sell_signal_service_ignores_entry_price_range() -> None:
    variant_a, variant_b = _snapshot_entry_variants()
    service = SellSignalService(providers=_PROVIDERS, config=_CFG)
    holding = _holding()

    outcome_a = service.analyze(holding, _NOW, snapshot=variant_a)
    outcome_b = service.analyze(holding, _NOW, snapshot=variant_b)

    assert outcome_a.triggered_rule_names == outcome_b.triggered_rule_names
    assert (outcome_a.recommendation is None) == (outcome_b.recommendation is None)
    if outcome_a.recommendation is not None and outcome_b.recommendation is not None:
        assert outcome_a.recommendation.entry_price_range_starter_price == Decimal("1100")
        assert outcome_b.recommendation.entry_price_range_starter_price == Decimal("900")
        assert (
            outcome_a.recommendation.recommendation_type
            == outcome_b.recommendation.recommendation_type
        )
        assert outcome_a.recommendation.sell_prices == outcome_b.recommendation.sell_prices
        assert outcome_a.recommendation.reasons == outcome_b.recommendation.reasons


def test_profit_taking_service_ignores_entry_price_range() -> None:
    variant_a, variant_b = _snapshot_entry_variants()
    service = ProfitTakingService(providers=_PROVIDERS, config=_CFG)
    holding = _holding()

    outcome_a = service.analyze(holding, _NOW, snapshot=variant_a)
    outcome_b = service.analyze(holding, _NOW, snapshot=variant_b)

    assert (outcome_a.recommendation is None) == (outcome_b.recommendation is None)
    if outcome_a.recommendation is not None and outcome_b.recommendation is not None:
        assert outcome_a.recommendation.entry_price_range_starter_price == Decimal("1100")
        assert outcome_b.recommendation.entry_price_range_starter_price == Decimal("900")
        assert (
            outcome_a.recommendation.recommendation_type
            == outcome_b.recommendation.recommendation_type
        )
        assert outcome_a.recommendation.sell_prices == outcome_b.recommendation.sell_prices


def _holding_decision_result() -> HoldingDecisionResult:
    q = CompanyQualityScore(score=30.0, coverage_ratio=1.0)
    i = InvestmentThesisScore(score=25.0, coverage_ratio=1.0)
    r = RiskDeductionScore(score=10.0, coverage_ratio=1.0)
    return HoldingDecisionResult(
        holding_decision_result_id="phase-b3-isolation-test",
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


def test_holding_decision_builder_ignores_entry_price_range() -> None:
    variant_a, variant_b = _snapshot_entry_variants()
    holding = _holding()
    result = _holding_decision_result()

    rec_a = build_holding_decision_recommendation(
        holding, result, variant_a, "v1", _CFG, _NOT_EVALUATED_EXIT_PRICE_RANGE
    )
    rec_b = build_holding_decision_recommendation(
        holding, result, variant_b, "v1", _CFG, _NOT_EVALUATED_EXIT_PRICE_RANGE
    )

    assert rec_a.entry_price_range_starter_price == Decimal("1100")
    assert rec_b.entry_price_range_starter_price == Decimal("900")
    # 保有判断スコア自体はHoldingDecisionResult側で既に確定済みであり、
    # Entry Price Rangeによって一切変わらない。
    assert rec_a.recommendation_type == rec_b.recommendation_type
    assert rec_a.sell_prices == rec_b.sell_prices


def test_holding_decision_builder_ignores_exit_price_range() -> None:
    """exit_price_range自体はBuilderの外(呼び出し元)で算出されるため、渡す
    値を変えても既存判定結果(recommendation_type/sell_prices)には一切
    影響しないことを確認する(Builderはコピーのみ、算出しない設計)。"""
    base, error = build_stock_snapshot(_PROVIDERS, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert base is not None
    holding = _holding()
    result = _holding_decision_result()

    exit_a = ExitPriceRangeResult(
        state=PriceRangeEvaluationState.EVALUATED,
        current_price=Decimal("1000"),
        neutral_anchor=Decimal("1200"),
        bull_anchor=Decimal("1500"),
        partial_profit_take_low_price=Decimal("1180"),
        partial_profit_take_high_price=Decimal("1220"),
        strong_profit_take_price=Decimal("1500"),
        downside_review_price=Decimal("900"),
        exit_review_price=Decimal("1100"),
        confidence=ConfidenceLevel.HIGH,
        evaluated_at=_NOW,
        model_version="exit_price_range_v1",
    )
    exit_b = ExitPriceRangeResult(
        state=PriceRangeEvaluationState.EVALUATED,
        current_price=Decimal("1000"),
        neutral_anchor=Decimal("1200"),
        bull_anchor=Decimal("1500"),
        partial_profit_take_low_price=Decimal("1150"),
        partial_profit_take_high_price=Decimal("1250"),
        strong_profit_take_price=Decimal("1600"),
        downside_review_price=Decimal("850"),
        exit_review_price=Decimal("1080"),
        confidence=ConfidenceLevel.MEDIUM,
        evaluated_at=_NOW,
        model_version="exit_price_range_v1",
    )

    rec_a = build_holding_decision_recommendation(holding, result, base, "v1", _CFG, exit_a)
    rec_b = build_holding_decision_recommendation(holding, result, base, "v1", _CFG, exit_b)

    assert rec_a.exit_price_range_strong_price == Decimal("1500")
    assert rec_b.exit_price_range_strong_price == Decimal("1600")
    assert rec_a.recommendation_type == rec_b.recommendation_type
    assert rec_a.sell_prices == rec_b.sell_prices


# --- Exit Price Range isolation(entry_exit_price.exit設定を変える) --------


def test_sell_signal_service_ignores_exit_price_range() -> None:
    base, error = build_stock_snapshot(_PROVIDERS, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert base is not None
    holding = _holding()

    service_a = SellSignalService(providers=_PROVIDERS, config=_CFG)
    service_b = SellSignalService(providers=_PROVIDERS, config=_CFG_EXIT_VARIANT)

    outcome_a = service_a.analyze(holding, _NOW, snapshot=base)
    outcome_b = service_b.analyze(holding, _NOW, snapshot=base)

    assert (outcome_a.recommendation is None) == (outcome_b.recommendation is None)
    if outcome_a.recommendation is not None and outcome_b.recommendation is not None:
        assert (
            outcome_a.recommendation.exit_price_range_downside_review_price
            != outcome_b.recommendation.exit_price_range_downside_review_price
        )
        assert (
            outcome_a.recommendation.recommendation_type
            == outcome_b.recommendation.recommendation_type
        )
        assert outcome_a.recommendation.sell_prices == outcome_b.recommendation.sell_prices
        assert outcome_a.recommendation.reasons == outcome_b.recommendation.reasons


def test_profit_taking_service_ignores_exit_price_range() -> None:
    base, error = build_stock_snapshot(_PROVIDERS, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert base is not None
    holding = _holding()

    service_a = ProfitTakingService(providers=_PROVIDERS, config=_CFG)
    service_b = ProfitTakingService(providers=_PROVIDERS, config=_CFG_EXIT_VARIANT)

    outcome_a = service_a.analyze(holding, _NOW, snapshot=base)
    outcome_b = service_b.analyze(holding, _NOW, snapshot=base)

    assert (outcome_a.recommendation is None) == (outcome_b.recommendation is None)
    if outcome_a.recommendation is not None and outcome_b.recommendation is not None:
        assert (
            outcome_a.recommendation.exit_price_range_downside_review_price
            != outcome_b.recommendation.exit_price_range_downside_review_price
        )
        assert (
            outcome_a.recommendation.recommendation_type
            == outcome_b.recommendation.recommendation_type
        )
        assert outcome_a.recommendation.sell_prices == outcome_b.recommendation.sell_prices


# --- average_purchase_price isolation(統合レベル、SellSignalService経由) --


def test_average_purchase_price_only_changes_exit_review_prices_not_recommendation() -> None:
    base, error = build_stock_snapshot(_PROVIDERS, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert base is not None
    service = SellSignalService(providers=_PROVIDERS, config=_CFG)

    holding_a = _holding()
    holding_b = holding_a.model_copy(update={"average_purchase_price": Decimal("2000")})

    outcome_a = service.analyze(holding_a, _NOW, snapshot=base)
    outcome_b = service.analyze(holding_b, _NOW, snapshot=base)

    assert (outcome_a.recommendation is None) == (outcome_b.recommendation is None)
    if outcome_a.recommendation is not None and outcome_b.recommendation is not None:
        # partial_low/high・strongはFair Value由来でaverage_purchase_priceに
        # 依存しないため不変。downside_review/exit_reviewのみ変化する。
        assert (
            outcome_a.recommendation.exit_price_range_partial_low_price
            == outcome_b.recommendation.exit_price_range_partial_low_price
        )
        assert (
            outcome_a.recommendation.exit_price_range_partial_high_price
            == outcome_b.recommendation.exit_price_range_partial_high_price
        )
        assert (
            outcome_a.recommendation.exit_price_range_strong_price
            == outcome_b.recommendation.exit_price_range_strong_price
        )
        if (
            outcome_a.recommendation.exit_price_range_downside_review_price is not None
            or outcome_b.recommendation.exit_price_range_downside_review_price is not None
        ):
            assert (
                outcome_a.recommendation.exit_price_range_downside_review_price
                != outcome_b.recommendation.exit_price_range_downside_review_price
            )
        assert (
            outcome_a.recommendation.recommendation_type
            == outcome_b.recommendation.recommendation_type
        )
