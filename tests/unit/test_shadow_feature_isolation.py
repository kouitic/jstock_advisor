"""判定精度向上機能(Phase A〜D: DecisionSnapshot/Historical Valuation/Timing/
Earnings Surprise・Trend/Entry・Exit Price Range/Market・Sector・Environment)が
既存の判定ロジックへ一切影響しないことを検証する横断テスト(Shadow計測原則の
直接証明)。

旧test_phase_a_isolation.py・test_phase_b_historical_valuation_isolation.py・
test_phase_b_timing_score_isolation.py・test_phase_b3_entry_exit_price_
isolation.py・test_phase_c_earnings_isolation.py・test_phase_d_environment_
isolation.pyを統合(テストコード削減対応2026-08)。

BUY/legacy SELL/ProfitTaking/HoldingDecisionの4パイプラインそれぞれについて、
StockSnapshotの対象フィールド(score/confidence/coverage/reason_codes/metrics)
をすべて変えた2つのsnapshotで同じ判定処理を実行し、それ以外の判定結果
(buy_action・company_quality_score、sell判定の種別・理由、holding decisionの
recommendation_type・sell_prices等)が完全に同一であることを直接証明する。

統合方針: historical_valuation/timing_score/earnings/entry_price_range/
environmentの5機能×4パイプライン=20ケースは、機能名の違いだけで本質的に
同一のArrange/Act/Assert構造であるため、`@pytest.mark.parametrize`+feature別
mutator関数へ統合する(実行されるテストケース数は統合前後で27件のまま不変)。
一方、以下の非定型7ケースは無理に共通化せず個別関数のまま維持する:
- Exit Price Range系3件(config差し替え型・holding builder直接引数型)
- average_purchase_price部分不変性検証1件(SELLのみ、完全不変ではなく
  「partial_low/high/strongは不変・downside/exit_reviewのみ変化」という
  部分不変性を検証する特殊ケース)
- environment通知本文非漏洩1件(BUYのみの追加観点)
- Phase A(DecisionSnapshot存在有無)2件(BUY/SELL/PT/Holdingの4パイプライン
  構造そのものに乗らない、RecommendationEvaluationService/PerformanceMetrics
  Service向けの別軸のテスト)
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.decision_snapshot import (
    DECISION_SNAPSHOT_MODEL_VERSION,
    DecisionSnapshot,
    build_decision_id,
)
from jstock_advisor.domain.entities.earnings_surprise import EarningsSurpriseResult
from jstock_advisor.domain.entities.earnings_trend import EarningsTrendResult
from jstock_advisor.domain.entities.entry_price_range import EntryPriceRangeResult
from jstock_advisor.domain.entities.enums import (
    AccountType,
    ConfidenceLevel,
    DecisionType,
    EarningsSurpriseCategory,
    EarningsSurpriseEvaluationState,
    EarningsTrendCategory,
    EarningsTrendEvaluationState,
    EnvironmentCategory,
    EnvironmentEvaluationState,
    ExecutionPlanReason,
    HistoricalValuationCategory,
    HistoricalValuationEvaluationState,
    HoldingDecisionCategory,
    HoldingDecisionConfidenceLevel,
    MarketEnvironmentEvaluationState,
    PriceRangeEvaluationState,
    RecommendationType,
    SectorEnvironmentEvaluationState,
    TimingScoreCategory,
    TimingScoreEvaluationState,
    ValuationBasis,
)
from jstock_advisor.domain.entities.environment import EnvironmentResult
from jstock_advisor.domain.entities.exit_price_range import ExitPriceRangeResult
from jstock_advisor.domain.entities.historical_valuation import HistoricalValuationResult
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
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.sector_environment import SectorEnvironmentResult
from jstock_advisor.domain.entities.timing_score import TimingScoreResult
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.services.buy_signal_service import BuySignalService
from jstock_advisor.services.holding_decision_notification_builder import (
    build_holding_decision_recommendation,
)
from jstock_advisor.services.performance_metrics_service import PerformanceMetricsService
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.recommendation_evaluation_service import (
    RecommendationEvaluationService,
)
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


def _base_snapshot() -> StockSnapshot:
    base, error = build_stock_snapshot(_PROVIDERS, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert base is not None
    return base


def _holding() -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, _STOCK_CODE),
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


def _holding_decision_result() -> HoldingDecisionResult:
    q = CompanyQualityScore(score=30.0, coverage_ratio=1.0)
    i = InvestmentThesisScore(score=25.0, coverage_ratio=1.0)
    r = RiskDeductionScore(score=10.0, coverage_ratio=1.0)
    return HoldingDecisionResult(
        holding_decision_result_id="shadow-feature-isolation-test",
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


# --- 定型5機能: feature別mutator(base_snapshot -> (variant_a, variant_b)) ---


def _historical_valuation_result(
    score: float, confidence: ConfidenceLevel
) -> HistoricalValuationResult:
    return HistoricalValuationResult(
        state=HistoricalValuationEvaluationState.EVALUATED,
        score=score,
        category=HistoricalValuationCategory.NORMAL,
        confidence=confidence,
        coverage=0.5 if confidence == ConfidenceLevel.MEDIUM else 1.0,
        per_score=score,
        per_percentile=0.3,
        current_per=Decimal("15"),
        current_per_basis=ValuationBasis.TRAILING,
        per_data_count_raw=4,
        per_data_count_used=4,
        reason_codes=("PBR_INSUFFICIENT_DATA_OR_BASIS_MISMATCH",),
        evaluated_at=_NOW,
        model_version="test-fixture",
    )


def _historical_valuation_variants(base: StockSnapshot) -> tuple[StockSnapshot, StockSnapshot]:
    variant_a = dataclasses.replace(
        base, historical_valuation=_historical_valuation_result(-88.0, ConfidenceLevel.HIGH)
    )
    variant_b = dataclasses.replace(
        base, historical_valuation=_historical_valuation_result(37.0, ConfidenceLevel.MEDIUM)
    )
    return variant_a, variant_b


def _timing_score_result(score: float, confidence: ConfidenceLevel) -> TimingScoreResult:
    return TimingScoreResult(
        state=TimingScoreEvaluationState.EVALUATED,
        score=score,
        category=TimingScoreCategory.NEUTRAL,
        confidence=confidence,
        coverage=0.5 if confidence == ConfidenceLevel.MEDIUM else 1.0,
        trend_quality_component=score,
        reason_codes=("MACD_UNAVAILABLE",),
        evaluated_at=_NOW,
        model_version="test-fixture",
    )


def _timing_score_variants(base: StockSnapshot) -> tuple[StockSnapshot, StockSnapshot]:
    variant_a = dataclasses.replace(base, timing=_timing_score_result(-77.0, ConfidenceLevel.HIGH))
    variant_b = dataclasses.replace(
        base, timing=_timing_score_result(41.0, ConfidenceLevel.MEDIUM)
    )
    return variant_a, variant_b


def _earnings_surprise_result(score: float, confidence: ConfidenceLevel) -> EarningsSurpriseResult:
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


def _earnings_trend_result(score: float, confidence: ConfidenceLevel) -> EarningsTrendResult:
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


def _earnings_variants(base: StockSnapshot) -> tuple[StockSnapshot, StockSnapshot]:
    variant_a = dataclasses.replace(
        base,
        earnings_surprise=_earnings_surprise_result(-88.0, ConfidenceLevel.HIGH),
        earnings_trend=_earnings_trend_result(-77.0, ConfidenceLevel.HIGH),
    )
    variant_b = dataclasses.replace(
        base,
        earnings_surprise=_earnings_surprise_result(33.0, ConfidenceLevel.MEDIUM),
        earnings_trend=_earnings_trend_result(44.0, ConfidenceLevel.MEDIUM),
    )
    return variant_a, variant_b


def _entry_price_range_result(starter_price: Decimal) -> EntryPriceRangeResult:
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


def _entry_price_range_variants(base: StockSnapshot) -> tuple[StockSnapshot, StockSnapshot]:
    variant_a = dataclasses.replace(
        base, entry_price_range=_entry_price_range_result(Decimal("1100"))
    )
    variant_b = dataclasses.replace(
        base, entry_price_range=_entry_price_range_result(Decimal("900"))
    )
    return variant_a, variant_b


def _environment_market_result(score: float) -> MarketEnvironmentResult:
    return MarketEnvironmentResult(
        state=MarketEnvironmentEvaluationState.EVALUATED,
        score=score,
        category=(
            EnvironmentCategory.STRONG_TAILWIND
            if score > 0
            else EnvironmentCategory.STRONG_HEADWIND
        ),
        confidence=ConfidenceLevel.HIGH,
        coverage=1.0,
        evaluated_at=_NOW,
        model_version="test-fixture",
    )


def _environment_sector_result(score: float) -> SectorEnvironmentResult:
    return SectorEnvironmentResult(
        state=SectorEnvironmentEvaluationState.EVALUATED,
        sector_etf_symbol="TEST_ETF",
        score=score,
        category=(
            EnvironmentCategory.STRONG_TAILWIND
            if score > 0
            else EnvironmentCategory.STRONG_HEADWIND
        ),
        confidence=ConfidenceLevel.HIGH,
        coverage=1.0,
        evaluated_at=_NOW,
        model_version="test-fixture",
    )


def _environment_result(score: float) -> EnvironmentResult:
    return EnvironmentResult(
        state=EnvironmentEvaluationState.EVALUATED,
        score=score,
        category=(
            EnvironmentCategory.STRONG_TAILWIND
            if score > 0
            else EnvironmentCategory.STRONG_HEADWIND
        ),
        confidence=ConfidenceLevel.HIGH,
        coverage=1.0,
        sector_available=True,
        market_weight_used=0.6,
        sector_weight_used=0.4,
        evaluated_at=_NOW,
        model_version="test-fixture",
    )


def _environment_variants(base: StockSnapshot) -> tuple[StockSnapshot, StockSnapshot]:
    variant_a = dataclasses.replace(
        base,
        market_environment=_environment_market_result(90.0),
        sector_environment=_environment_sector_result(90.0),
        environment=_environment_result(90.0),
    )
    variant_b = dataclasses.replace(
        base,
        market_environment=_environment_market_result(-90.0),
        sector_environment=_environment_sector_result(-90.0),
        environment=_environment_result(-90.0),
    )
    return variant_a, variant_b


_Mutator = Callable[[StockSnapshot], tuple[StockSnapshot, StockSnapshot]]

FEATURE_MUTATORS: dict[str, _Mutator] = {
    "historical_valuation": _historical_valuation_variants,
    "timing_score": _timing_score_variants,
    "earnings": _earnings_variants,
    "entry_price_range": _entry_price_range_variants,
    "environment": _environment_variants,
}
_FEATURE_IDS = list(FEATURE_MUTATORS.keys())

# (feature, service) -> ((検証フィールド名, variant_a期待値, variant_b期待値), ...)。
# environmentのみサービスごとにフィールド名が異なる(BUY=market_score、
# SELL/HoldingDecision=environment_score、ProfitTaking=sector_score)ため、
# 単純な文字列テンプレートではなく個別テーブルとして持つ(旧6ファイルの
# assert対象を1件も欠かさず転記)。
_PRIMARY_FIELDS: dict[tuple[str, str], tuple[tuple[str, object, object], ...]] = {
    ("historical_valuation", "buy"): (
        ("historical_valuation_score", -88.0, 37.0),
        ("historical_valuation_confidence", ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM),
        ("historical_valuation_coverage", 1.0, 0.5),
    ),
    ("historical_valuation", "sell"): (("historical_valuation_score", -88.0, 37.0),),
    ("historical_valuation", "profit_taking"): (("historical_valuation_score", -88.0, 37.0),),
    ("historical_valuation", "holding"): (
        ("historical_valuation_score", -88.0, 37.0),
        ("historical_valuation_confidence", ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM),
    ),
    ("timing_score", "buy"): (
        ("timing_score", -77.0, 41.0),
        ("timing_confidence", ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM),
        ("timing_coverage", 1.0, 0.5),
    ),
    ("timing_score", "sell"): (("timing_score", -77.0, 41.0),),
    ("timing_score", "profit_taking"): (("timing_score", -77.0, 41.0),),
    ("timing_score", "holding"): (
        ("timing_score", -77.0, 41.0),
        ("timing_confidence", ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM),
    ),
    ("earnings", "buy"): (
        ("earnings_surprise_score", -88.0, 33.0),
        ("earnings_trend_score", -77.0, 44.0),
        ("earnings_surprise_confidence", ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM),
    ),
    ("earnings", "sell"): (("earnings_surprise_score", -88.0, 33.0),),
    ("earnings", "profit_taking"): (("earnings_trend_score", -77.0, 44.0),),
    ("earnings", "holding"): (
        ("earnings_surprise_score", -88.0, 33.0),
        ("earnings_trend_score", -77.0, 44.0),
        ("earnings_surprise_confidence", ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM),
    ),
    ("entry_price_range", "buy"): (
        ("entry_price_range_starter_price", Decimal("1100"), Decimal("900")),
    ),
    ("entry_price_range", "sell"): (
        ("entry_price_range_starter_price", Decimal("1100"), Decimal("900")),
    ),
    ("entry_price_range", "profit_taking"): (
        ("entry_price_range_starter_price", Decimal("1100"), Decimal("900")),
    ),
    ("entry_price_range", "holding"): (
        ("entry_price_range_starter_price", Decimal("1100"), Decimal("900")),
    ),
    ("environment", "buy"): (("market_score", 90.0, -90.0),),
    ("environment", "sell"): (("environment_score", 90.0, -90.0),),
    ("environment", "profit_taking"): (("sector_score", 90.0, -90.0),),
    ("environment", "holding"): (("environment_score", 90.0, -90.0),),
}

# feature別の追加不変フィールド(各サービスで常に確認するbuy_action/company_
# quality_score・recommendation_type等の基本セット以外に、旧ファイルが
# 個別に確認していたフィールド)。
_BUY_EXTRA_INVARIANTS: dict[str, tuple[str, ...]] = {
    "historical_valuation": ("purchase_attractiveness_score",),
    "timing_score": ("purchase_attractiveness_score",),
    "earnings": ("purchase_attractiveness_score",),
    "entry_price_range": ("entry_buy_price", "standard_buy_price", "buy_prices"),
    "environment": ("entry_buy_price", "buy_prices", "reasons"),
}
_SELL_EXTRA_INVARIANTS: dict[str, tuple[str, ...]] = {
    "historical_valuation": ("reasons",),
    "timing_score": ("reasons",),
    "earnings": ("reasons",),
    "entry_price_range": ("sell_prices", "reasons"),
    "environment": ("sell_prices", "reasons"),
}
_PROFIT_TAKING_EXTRA_INVARIANTS: dict[str, tuple[str, ...]] = {
    "historical_valuation": ("reasons",),
    "timing_score": ("reasons",),
    "earnings": ("reasons",),
    "entry_price_range": ("sell_prices",),
    "environment": ("sell_prices",),
}
_HOLDING_EXTRA_INVARIANTS: dict[str, tuple[str, ...]] = {
    "historical_valuation": ("config_values_used",),
    "timing_score": ("config_values_used",),
    "earnings": ("config_values_used",),
    "entry_price_range": (),
    "environment": (),
}


@pytest.mark.parametrize("feature", _FEATURE_IDS, ids=_FEATURE_IDS)
def test_buy_signal_ignores_shadow_feature(feature: str) -> None:
    """BUYパイプラインは各Shadow機能のスコア変化を記録するが、buy_action等の
    既存判定結果には一切影響しない(旧5ファイルのBUY系5関数を統合)。"""
    variant_a, variant_b = FEATURE_MUTATORS[feature](_base_snapshot())
    service = BuySignalService(providers=_PROVIDERS, config=_CFG, business_calendar=_CALENDAR)

    outcome_a = service.analyze(_STOCK_CODE, _NOW, snapshot=variant_a)
    outcome_b = service.analyze(_STOCK_CODE, _NOW, snapshot=variant_b)

    assert outcome_a.recommendation is not None
    assert outcome_b.recommendation is not None
    for field, expected_a, expected_b in _PRIMARY_FIELDS[(feature, "buy")]:
        assert getattr(outcome_a.recommendation, field) == expected_a
        assert getattr(outcome_b.recommendation, field) == expected_b
    # feature関連以外の判定結果は完全に同一。
    assert outcome_a.recommendation.buy_action == outcome_b.recommendation.buy_action
    assert (
        outcome_a.recommendation.company_quality_score
        == outcome_b.recommendation.company_quality_score
    )
    for invariant in _BUY_EXTRA_INVARIANTS[feature]:
        assert getattr(outcome_a.recommendation, invariant) == getattr(
            outcome_b.recommendation, invariant
        )


@pytest.mark.parametrize("feature", _FEATURE_IDS, ids=_FEATURE_IDS)
def test_sell_signal_ignores_shadow_feature(feature: str) -> None:
    """legacy SELLパイプラインは各Shadow機能のスコア変化を記録するが、判定種別・
    理由等の既存判定結果には一切影響しない(旧5ファイルのSELL系5関数を統合)。"""
    variant_a, variant_b = FEATURE_MUTATORS[feature](_base_snapshot())
    service = SellSignalService(providers=_PROVIDERS, config=_CFG)
    holding = _holding()

    outcome_a = service.analyze(holding, _NOW, snapshot=variant_a)
    outcome_b = service.analyze(holding, _NOW, snapshot=variant_b)

    assert outcome_a.triggered_rule_names == outcome_b.triggered_rule_names
    assert (outcome_a.recommendation is None) == (outcome_b.recommendation is None)
    if outcome_a.recommendation is not None and outcome_b.recommendation is not None:
        for field, expected_a, expected_b in _PRIMARY_FIELDS[(feature, "sell")]:
            assert getattr(outcome_a.recommendation, field) == expected_a
            assert getattr(outcome_b.recommendation, field) == expected_b
        assert (
            outcome_a.recommendation.recommendation_type
            == outcome_b.recommendation.recommendation_type
        )
        for invariant in _SELL_EXTRA_INVARIANTS[feature]:
            assert getattr(outcome_a.recommendation, invariant) == getattr(
                outcome_b.recommendation, invariant
            )


@pytest.mark.parametrize("feature", _FEATURE_IDS, ids=_FEATURE_IDS)
def test_profit_taking_ignores_shadow_feature(feature: str) -> None:
    """ProfitTakingパイプラインは各Shadow機能のスコア変化を記録するが、判定
    種別等の既存判定結果には一切影響しない(旧5ファイルのProfitTaking系5関数を
    統合)。"""
    variant_a, variant_b = FEATURE_MUTATORS[feature](_base_snapshot())
    service = ProfitTakingService(providers=_PROVIDERS, config=_CFG)
    holding = _holding()

    outcome_a = service.analyze(holding, _NOW, snapshot=variant_a)
    outcome_b = service.analyze(holding, _NOW, snapshot=variant_b)

    assert (outcome_a.recommendation is None) == (outcome_b.recommendation is None)
    if outcome_a.recommendation is not None and outcome_b.recommendation is not None:
        for field, expected_a, expected_b in _PRIMARY_FIELDS[(feature, "profit_taking")]:
            assert getattr(outcome_a.recommendation, field) == expected_a
            assert getattr(outcome_b.recommendation, field) == expected_b
        assert (
            outcome_a.recommendation.recommendation_type
            == outcome_b.recommendation.recommendation_type
        )
        for invariant in _PROFIT_TAKING_EXTRA_INVARIANTS[feature]:
            assert getattr(outcome_a.recommendation, invariant) == getattr(
                outcome_b.recommendation, invariant
            )


@pytest.mark.parametrize("feature", _FEATURE_IDS, ids=_FEATURE_IDS)
def test_holding_decision_builder_ignores_shadow_feature(feature: str) -> None:
    """HoldingDecisionBuilderは各Shadow機能のスコア変化を記録するが、保有判断
    スコア自体はHoldingDecisionResult側で既に確定済みであり、Shadow機能に
    よって一切変わらない(旧5ファイルのHoldingDecision系5関数を統合)。"""
    variant_a, variant_b = FEATURE_MUTATORS[feature](_base_snapshot())
    holding = _holding()
    result = _holding_decision_result()

    rec_a = build_holding_decision_recommendation(
        holding, result, variant_a, "v1", _CFG, _NOT_EVALUATED_EXIT_PRICE_RANGE
    )
    rec_b = build_holding_decision_recommendation(
        holding, result, variant_b, "v1", _CFG, _NOT_EVALUATED_EXIT_PRICE_RANGE
    )

    for field, expected_a, expected_b in _PRIMARY_FIELDS[(feature, "holding")]:
        assert getattr(rec_a, field) == expected_a
        assert getattr(rec_b, field) == expected_b
    assert rec_a.recommendation_type == rec_b.recommendation_type
    assert rec_a.sell_prices == rec_b.sell_prices
    for invariant in _HOLDING_EXTRA_INVARIANTS[feature]:
        assert getattr(rec_a, invariant) == getattr(rec_b, invariant)


# --- 非定型ケース1: Exit Price Range(共通mutatorに乗らない、テストコード
# 削減対応2026-08でも個別関数のまま維持) -------------------------------------


def test_holding_decision_builder_ignores_exit_price_range() -> None:
    """exit_price_range自体はBuilderの外(呼び出し元)で算出されるため、渡す
    値を変えても既存判定結果(recommendation_type/sell_prices)には一切
    影響しないことを確認する(Builderはコピーのみ、算出しない設計)。"""
    base = _base_snapshot()
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


def test_sell_signal_service_ignores_exit_price_range() -> None:
    """entry_exit_price.exit設定を変えてもSELLパイプラインの判定種別・
    sell_prices・理由は変わらない(exit_price_range由来のdownside_review_
    priceだけが変わることで、config差し替えが実際に効いていることも確認)。"""
    base = _base_snapshot()
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
    """entry_exit_price.exit設定を変えてもProfitTakingパイプラインの判定種別・
    sell_pricesは変わらない。"""
    base = _base_snapshot()
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


# --- 非定型ケース2: average_purchase_price部分不変性(SELLのみ、完全不変では
# なく特定フィールドのみ変化する特殊ケース) ------------------------------------


def test_average_purchase_price_only_changes_exit_review_prices_not_recommendation() -> None:
    """partial_low/high・strongはFair Value由来でaverage_purchase_priceに
    依存しないため不変。downside_review/exit_reviewのみ変化する(完全不変
    ではなく部分不変であることの確認、統合しない)。"""
    base = _base_snapshot()
    service = SellSignalService(providers=_PROVIDERS, config=_CFG)

    holding_a = _holding()
    holding_b = holding_a.model_copy(update={"average_purchase_price": Decimal("2000")})

    outcome_a = service.analyze(holding_a, _NOW, snapshot=base)
    outcome_b = service.analyze(holding_b, _NOW, snapshot=base)

    assert (outcome_a.recommendation is None) == (outcome_b.recommendation is None)
    if outcome_a.recommendation is not None and outcome_b.recommendation is not None:
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


# --- 非定型ケース3: environmentが通知本文へ漏洩しないこと(BUYのみの追加観点) --


def test_environment_does_not_leak_into_notification_text() -> None:
    variant_a, variant_b = _environment_variants(_base_snapshot())
    service = BuySignalService(providers=_PROVIDERS, config=_CFG, business_calendar=_CALENDAR)

    outcome_a = service.analyze(_STOCK_CODE, _NOW, snapshot=variant_a)
    outcome_b = service.analyze(_STOCK_CODE, _NOW, snapshot=variant_b)

    assert outcome_a.recommendation is not None
    assert outcome_b.recommendation is not None
    assert (
        outcome_a.recommendation.recommended_action_summary
        == outcome_b.recommendation.recommended_action_summary
    )


# --- 非定型ケース4: Phase A(DecisionSnapshot存在有無、4パイプライン構造には
# 乗らないためBUY/SELL/PT/Holdingとは別軸として個別関数のまま維持) -------------


def _phase_a_make_recommendation(recommendation_id: str = "rec-1") -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=_STOCK_CODE,
        stock_name="テスト銘柄",
        recommended_at=dt.datetime(2024, 1, 4, tzinfo=dt.UTC),
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            standard=PriceWithRationale(price=Decimal("2000"), rationale="x"),
        ),
        price_at_recommendation=Decimal("2200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


def _phase_a_make_decision_snapshot(recommendation: Recommendation) -> DecisionSnapshot:
    return DecisionSnapshot(
        decision_id=build_decision_id(recommendation.recommendation_id),
        decision_type=DecisionType.BUY,
        stock_code=recommendation.stock_code,
        evaluated_at=recommendation.recommended_at,
        evaluation_date_jst=recommendation.recommended_at.date(),
        recommendation_id=recommendation.recommendation_id,
        existing_action=recommendation.recommendation_type,
        market_price=recommendation.price_at_recommendation,
        rule_version=recommendation.rule_version,
        model_version=DECISION_SNAPSHOT_MODEL_VERSION,
    )


def test_decision_snapshot_presence_does_not_change_evaluation_result_count(
    tmp_path: Path,
) -> None:
    """DecisionSnapshotが存在してもrecommendation_evaluation_service.pyが生成する
    EvaluationResultの件数は変わらない(専用のEvaluationResultを新規生成しない)。"""
    config = load_config()
    calendar = BusinessCalendar.from_config(config.holiday_calendar)
    now = dt.datetime(2024, 3, 1, tzinfo=dt.UTC)

    recommendation_repo = RecommendationRepository(store_dir=tmp_path)
    evaluation_repo = EvaluationResultRepository(store_dir=tmp_path)
    decision_repo = DecisionSnapshotRepository(store_dir=tmp_path)
    recommendation = _phase_a_make_recommendation()
    recommendation_repo.save(recommendation)

    service = RecommendationEvaluationService(
        market_data_provider=MockMarketDataProvider(now=now),
        config=config,
        business_calendar=calendar,
        recommendation_repository=recommendation_repo,
        evaluation_repository=evaluation_repo,
    )

    outcome_without_decision = service.run_due_evaluations(now)
    count_without_decision = len(evaluation_repo.list_all())
    assert len(outcome_without_decision.evaluated) == count_without_decision

    # DecisionSnapshotを追加してから、まっさらな状態で再度評価しても件数は同じになる
    # (recommendation_evaluation_service.py自体はDecisionSnapshotの有無を一切見ない)。
    decision_repo.insert_if_absent(_phase_a_make_decision_snapshot(recommendation))
    evaluation_repo_2 = EvaluationResultRepository(store_dir=tmp_path / "with_decision")
    recommendation_repo_2 = RecommendationRepository(store_dir=tmp_path / "with_decision")
    recommendation_repo_2.save(recommendation)
    service_2 = RecommendationEvaluationService(
        market_data_provider=MockMarketDataProvider(now=now),
        config=config,
        business_calendar=calendar,
        recommendation_repository=recommendation_repo_2,
        evaluation_repository=evaluation_repo_2,
    )
    outcome_with_decision = service_2.run_due_evaluations(now)

    assert len(outcome_with_decision.evaluated) == len(outcome_without_decision.evaluated)
    # decision_idフィールド自体が存在しないため、DecisionSnapshotとの紐付けは
    # 一切生じない(EvaluationResultはrecommendation_idのみで冪等性を保つ)。


def test_performance_metrics_unaffected_by_decision_snapshot_presence(tmp_path: Path) -> None:
    """PerformanceMetricsService.summarize()の結果(件数・成功率・平均リターン)は、
    同じ銘柄・同じ評価データに対してDecisionSnapshotが存在してもしなくても
    完全に同一になる(既存の週次改善レビュー・成績集計への影響ゼロ)。"""
    config = load_config()
    calendar = BusinessCalendar.from_config(config.holiday_calendar)
    now = dt.datetime(2024, 3, 1, tzinfo=dt.UTC)

    recommendation_repo = RecommendationRepository(store_dir=tmp_path)
    evaluation_repo = EvaluationResultRepository(store_dir=tmp_path)
    recommendation = _phase_a_make_recommendation()
    recommendation_repo.save(recommendation)

    eval_service = RecommendationEvaluationService(
        market_data_provider=MockMarketDataProvider(now=now),
        config=config,
        business_calendar=calendar,
        recommendation_repository=recommendation_repo,
        evaluation_repository=evaluation_repo,
    )
    eval_service.run_due_evaluations(now)

    metrics_service = PerformanceMetricsService(
        evaluation_repository=evaluation_repo, recommendation_repository=recommendation_repo
    )
    summary_before = metrics_service.summarize(now=now)

    # DecisionSnapshotを追加(既存のevaluation_repo/recommendation_repoには一切触れない)。
    decision_repo = DecisionSnapshotRepository(store_dir=tmp_path)
    decision_repo.insert_if_absent(_phase_a_make_decision_snapshot(recommendation))

    summary_after = metrics_service.summarize(now=now)

    assert summary_before.overall.count == summary_after.overall.count
    assert summary_before.overall.conclusive_count == summary_after.overall.conclusive_count
    assert summary_before.overall.success_rate_pct == summary_after.overall.success_rate_pct
    assert (
        summary_before.overall.avg_price_return_pct == summary_after.overall.avg_price_return_pct
    )
    assert len(summary_before.by_recommendation_type) == len(summary_after.by_recommendation_type)
