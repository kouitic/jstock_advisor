"""domain/decision_snapshot_builder.pyのテスト(判定精度向上機能Phase A)。

build_decision_snapshot()が外部I/Oを一切行わない純関数であること、point-in-time
保証(StockSnapshotに既に存在するデータ以外を取り込まない)を検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.decision_snapshot_builder import build_decision_snapshot
from jstock_advisor.domain.entities.classification import StockTypeClassification
from jstock_advisor.domain.entities.common import (
    BuyPriceLevels,
    DataSourceReference,
    PriceWithRationale,
)
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    DecisionType,
    EarningsDateStatus,
    RecommendationType,
    TrendClassification,
)
from jstock_advisor.domain.entities.momentum import MomentumSnapshot
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.valuation import FairValueRange
from jstock_advisor.interfaces.types import DividendInfo, FinancialSummary
from jstock_advisor.services.stock_snapshot_service import StockSnapshot

_SOURCE = DataSourceReference(
    provider="test-fixture", fetched_at=dt.datetime(2026, 8, 8, tzinfo=dt.UTC)
)
_STOCK_CODE = "2914"

_MOMENTUM_PLACEHOLDER = MomentumSnapshot(
    trend_classification=TrendClassification.NEUTRAL, confidence=ConfidenceLevel.LOW
)
_STOCK_TYPE_PLACEHOLDER = StockTypeClassification(
    stock_code=_STOCK_CODE,
    classified_at=dt.datetime(2026, 8, 8, tzinfo=dt.UTC),
    types=[],
    primary_type=None,
    confidence=ConfidenceLevel.LOW,
    classification_basis=[],
    data_sources=[],
)


def _fair_value_range(
    bear: Decimal | None = Decimal("1000"),
    neutral: Decimal | None = Decimal("1200"),
    bull: Decimal | None = Decimal("1400"),
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH,
) -> FairValueRange:
    return FairValueRange(
        bear=bear,
        neutral=neutral,
        bull=bull,
        overall_confidence=confidence,
        methods_used=[],
        methods_excluded=[],
        usable_for_trading_judgment=True,
    )


def _snapshot(
    *,
    current_price: Decimal = Decimal("1150"),
    fair_value_range: FairValueRange | None = None,
    data_fetched_at: dt.datetime = dt.datetime(2026, 8, 8, 3, 0, tzinfo=dt.UTC),
) -> StockSnapshot:
    return StockSnapshot(
        stock_code=_STOCK_CODE,
        current_price=current_price,
        financial=FinancialSummary(stock_code=_STOCK_CODE, source=_SOURCE),
        dividend=DividendInfo(stock_code=_STOCK_CODE, fiscal_year="2026", source=_SOURCE),
        benefit=None,
        bars=[],
        historical_valuations=[],
        avg_trading_value=None,
        disclosures=[],
        next_earnings_date=None,
        earnings_date_status=EarningsDateStatus.UNAVAILABLE,
        earnings_date_raw=None,
        business_days_to_earnings=None,
        dividend_yield_pct=None,
        benefit_yield_pct=None,
        annual_benefit_value=None,
        total_yield_pct=0.0,
        fair_value=None,
        fair_value_methods_used_count=0,
        data_sources=[_SOURCE],
        data_fetched_at=data_fetched_at,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[],
        quarterly_operating_income_periods=[],
        quarterly_operating_cashflow_periods=[],
        severe_earnings_decline=False,
        disclosure_risk_keywords_found=[],
        material_event_keywords_found=[],
        cashflow_decomposition=None,
        stock_type_classification=_STOCK_TYPE_PLACEHOLDER,
        fair_value_range=fair_value_range or _fair_value_range(),
        momentum=_MOMENTUM_PLACEHOLDER,
    )


def _recommendation(
    recommended_at: dt.datetime = dt.datetime(2026, 8, 8, 3, 0, tzinfo=dt.UTC),
    recommendation_type: RecommendationType = RecommendationType.BUY,
) -> Recommendation:
    return Recommendation(
        recommendation_id="rec-1",
        stock_code=_STOCK_CODE,
        stock_name="テスト銘柄",
        recommended_at=recommended_at,
        recommendation_type=recommendation_type,
        buy_prices=BuyPriceLevels(
            standard=PriceWithRationale(price=Decimal("1100"), rationale="x"),
        ),
        price_at_recommendation=Decimal("1150"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


def test_build_decision_snapshot_copies_market_price_and_fair_value() -> None:
    snapshot = _snapshot()
    recommendation = _recommendation()

    decision = build_decision_snapshot(snapshot, recommendation, DecisionType.BUY)

    assert decision.stock_code == _STOCK_CODE
    assert decision.market_price == Decimal("1150")
    assert decision.fair_value_bear == Decimal("1000")
    assert decision.fair_value_neutral == Decimal("1200")
    assert decision.fair_value_bull == Decimal("1400")
    assert decision.fair_value_confidence == ConfidenceLevel.HIGH
    assert decision.recommendation_id == "rec-1"
    assert decision.existing_action == RecommendationType.BUY
    assert decision.rule_version == "v1-mvp"
    assert decision.decision_type == DecisionType.BUY
    # Phase Aではスコア項目は全てNone
    assert decision.timing_score is None
    assert decision.historical_valuation_score is None
    assert decision.earnings_surprise_score is None
    assert decision.earnings_trend_score is None
    assert decision.market_score is None
    assert decision.sector_score is None
    assert decision.environment_score is None


def test_build_decision_snapshot_generates_unique_decision_id() -> None:
    snapshot = _snapshot()
    recommendation = _recommendation()

    first = build_decision_snapshot(snapshot, recommendation, DecisionType.BUY)
    second = build_decision_snapshot(snapshot, recommendation, DecisionType.BUY)

    assert first.decision_id != second.decision_id


def test_build_decision_snapshot_propagates_partial_fair_value_none() -> None:
    """fair_value_rangeの一部(bear等)がNoneの場合、DecisionSnapshot側もNoneのまま
    伝播すること(例外を起こさない、推測で補完しない)。"""
    snapshot = _snapshot(fair_value_range=_fair_value_range(bear=None, neutral=None, bull=None))
    recommendation = _recommendation()

    decision = build_decision_snapshot(snapshot, recommendation, DecisionType.SELL)

    assert decision.fair_value_bear is None
    assert decision.fair_value_neutral is None
    assert decision.fair_value_bull is None


def test_build_decision_snapshot_evaluated_at_matches_recommended_at() -> None:
    """evaluated_atはrecommendation.recommended_atと厳密一致する(point-in-time保証)。"""
    recommended_at = dt.datetime(2026, 8, 8, 3, 0, tzinfo=dt.UTC)
    snapshot = _snapshot()
    recommendation = _recommendation(recommended_at=recommended_at)

    decision = build_decision_snapshot(snapshot, recommendation, DecisionType.BUY)

    assert decision.evaluated_at == recommended_at


def test_build_decision_snapshot_jst_boundary() -> None:
    """UTC深夜帯(JSTでは日付が繰り上がる)でもevaluation_date_jstが正しく
    導出されること(決算日修正で確立したJST境界原則の回帰確認)。"""
    recommended_at = dt.datetime(2026, 8, 7, 20, 0, tzinfo=dt.UTC)  # JST 2026-08-08 05:00
    snapshot = _snapshot()
    recommendation = _recommendation(recommended_at=recommended_at)

    decision = build_decision_snapshot(snapshot, recommendation, DecisionType.BUY)

    assert decision.evaluation_date_jst == dt.date(2026, 8, 8)


def test_build_decision_snapshot_data_fetched_at_matches_snapshot() -> None:
    """data_fetched_atは渡されたStockSnapshot.data_fetched_atと厳密一致する
    (別時点のデータが紛れ込んでいないことの検知手段)。"""
    fetched_at = dt.datetime(2026, 8, 8, 3, 30, tzinfo=dt.UTC)
    snapshot = _snapshot(data_fetched_at=fetched_at)
    recommendation = _recommendation()

    decision = build_decision_snapshot(snapshot, recommendation, DecisionType.BUY)

    assert decision.data_fetched_at == fetched_at
    assert tuple(decision.data_sources) == (_SOURCE,)


def test_build_decision_snapshot_model_version_is_phase_a_constant() -> None:
    from jstock_advisor.domain.entities.decision_snapshot import DECISION_SNAPSHOT_MODEL_VERSION

    snapshot = _snapshot()
    recommendation = _recommendation()

    decision = build_decision_snapshot(snapshot, recommendation, DecisionType.PROFIT_TAKING)

    assert decision.model_version == DECISION_SNAPSHOT_MODEL_VERSION
    assert decision.decision_type == DecisionType.PROFIT_TAKING
