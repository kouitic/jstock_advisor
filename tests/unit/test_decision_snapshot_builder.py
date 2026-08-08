"""domain/decision_snapshot_builder.pyのテスト(判定精度向上機能Phase A)。

コードレビュー対応: DecisionSnapshotはRecommendationのみから構築する
(StockSnapshotには依存しない)。市場価格・適正価格はRecommendationに
保存された「最終判断値」をそのままコピーし、Recommendation側に値が無い
場合はNoneのまま(補完しない)。decision_idはrecommendation_idのみから
決定的に生成され(1 Recommendation = 1 DecisionSnapshot、decision_typeは
IDに含めない)、同じ入力なら常に同じIDになること(冪等性)を検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.domain.decision_snapshot_builder import build_decision_snapshot
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.decision_snapshot import (
    DECISION_SNAPSHOT_MODEL_VERSION,
    build_decision_id,
)
from jstock_advisor.domain.entities.enums import ConfidenceLevel, DecisionType, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation

_STOCK_CODE = "2914"


def _recommendation(
    recommendation_id: str = "rec-1",
    recommended_at: dt.datetime = dt.datetime(2026, 8, 8, 3, 0, tzinfo=dt.UTC),
    recommendation_type: RecommendationType = RecommendationType.BUY,
    fair_value_bear: Decimal | None = Decimal("1000"),
    fair_value_neutral: Decimal | None = Decimal("1200"),
    fair_value_bull: Decimal | None = Decimal("1400"),
    fair_value_overall_confidence: ConfidenceLevel | None = ConfidenceLevel.HIGH,
    config_values_used: dict | None = None,
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
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
        fair_value_bear=fair_value_bear,
        fair_value_neutral=fair_value_neutral,
        fair_value_bull=fair_value_bull,
        fair_value_overall_confidence=fair_value_overall_confidence,
        config_values_used=config_values_used or {},
    )


def test_build_decision_snapshot_uses_recommendation_as_source_of_truth() -> None:
    """market_price/fair_value_*はRecommendationの最終判断値をそのまま使う
    (コードレビュー対応、最重要項目)。"""
    recommendation = _recommendation()

    decision = build_decision_snapshot(recommendation, DecisionType.BUY)

    assert decision.stock_code == _STOCK_CODE
    assert decision.market_price == recommendation.price_at_recommendation == Decimal("1150")
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


def test_build_decision_snapshot_does_not_fabricate_missing_recommendation_values() -> None:
    """Recommendationにfair_value_*が保存されていない(旧方式・値未算出等)場合は
    Noneのまま保存し、他の値から補完・推測しない。"""
    recommendation = _recommendation(
        fair_value_bear=None,
        fair_value_neutral=None,
        fair_value_bull=None,
        fair_value_overall_confidence=None,
    )

    decision = build_decision_snapshot(recommendation, DecisionType.SELL)

    assert decision.fair_value_bear is None
    assert decision.fair_value_neutral is None
    assert decision.fair_value_bull is None
    assert decision.fair_value_confidence is None


def test_build_decision_snapshot_copies_config_values_used() -> None:
    recommendation = _recommendation(config_values_used={"threshold": 50.0, "margin": "HIGH"})

    decision = build_decision_snapshot(recommendation, DecisionType.PROFIT_TAKING)

    assert decision.config_values_used == {"threshold": 50.0, "margin": "HIGH"}


def test_build_decision_snapshot_evaluated_at_matches_recommended_at() -> None:
    """evaluated_atはrecommendation.recommended_atと厳密一致する(point-in-time保証)。"""
    recommended_at = dt.datetime(2026, 8, 8, 3, 0, tzinfo=dt.UTC)
    recommendation = _recommendation(recommended_at=recommended_at)

    decision = build_decision_snapshot(recommendation, DecisionType.BUY)

    assert decision.evaluated_at == recommended_at


def test_build_decision_snapshot_jst_boundary() -> None:
    """UTC深夜帯(JSTでは日付が繰り上がる)でもevaluation_date_jstが正しく
    導出されること(決算日修正で確立したJST境界原則の回帰確認)。"""
    recommended_at = dt.datetime(2026, 8, 7, 20, 0, tzinfo=dt.UTC)  # JST 2026-08-08 05:00
    recommendation = _recommendation(recommended_at=recommended_at)

    decision = build_decision_snapshot(recommendation, DecisionType.BUY)

    assert decision.evaluation_date_jst == dt.date(2026, 8, 8)


def test_build_decision_snapshot_model_version_matches_constant() -> None:
    recommendation = _recommendation()

    decision = build_decision_snapshot(recommendation, DecisionType.HOLDING_DECISION)

    assert decision.model_version == DECISION_SNAPSHOT_MODEL_VERSION


def test_build_decision_snapshot_copies_historical_valuation_score() -> None:
    """判定精度向上機能Phase B: Recommendation.historical_valuation_scoreが
    DecisionSnapshot.historical_valuation_scoreへそのままコピーされる
    (Shadow計測。既存の判定ロジックはこの値を一切参照しない)。"""
    recommendation = _recommendation(config_values_used={}).model_copy(
        update={"historical_valuation_score": 42.5}
    )

    decision = build_decision_snapshot(recommendation, DecisionType.BUY)

    assert decision.historical_valuation_score == 42.5


def test_build_decision_snapshot_historical_valuation_score_none_when_unset() -> None:
    recommendation = _recommendation()

    decision = build_decision_snapshot(recommendation, DecisionType.BUY)

    assert decision.historical_valuation_score is None


# --- RecommendationType/DecisionTypeの現行対応関係(コードレビュー対応item 12) ---
# decision_snapshot_builder.pyのモジュールdocstring参照。生産コードの横断調査の結果、
# 各パイプラインは常に単一のRecommendationType集合・単一のDecisionTypeとのみ対応する。
_RECOMMENDATION_TYPE_TO_DECISION_TYPE = [
    (RecommendationType.BUY, DecisionType.BUY),
    (RecommendationType.WATCH_BUY, DecisionType.BUY),
    (RecommendationType.SELL, DecisionType.SELL),
    (RecommendationType.PARTIAL_PROFIT_TAKE, DecisionType.PROFIT_TAKING),
    (RecommendationType.FULL_PROFIT_TAKE, DecisionType.PROFIT_TAKING),
    (RecommendationType.URGENT_HOLDING_REVIEW, DecisionType.HOLDING_DECISION),
]


@pytest.mark.parametrize(
    ("recommendation_type", "decision_type"), _RECOMMENDATION_TYPE_TO_DECISION_TYPE
)
def test_recommendation_type_to_decision_type_mapping(
    recommendation_type: RecommendationType, decision_type: DecisionType
) -> None:
    """現行生産コードのRecommendationType→DecisionType対応関係(横断調査結果)を
    回帰的に固定する。同一recommendation_idが複数のDecisionTypeへ保存される経路は
    存在しないため、existing_action(=recommendation_type)とdecision_typeの組は
    常にこの対応表どおりになる。"""
    recommendation = _recommendation(recommendation_type=recommendation_type)

    decision = build_decision_snapshot(recommendation, decision_type)

    assert decision.existing_action == recommendation_type
    assert decision.decision_type == decision_type


# --- 冪等性(コードレビュー対応、決定的decision_id) -------------------------------


def test_build_decision_id_is_deterministic_for_same_inputs() -> None:
    assert build_decision_id("rec-1") == build_decision_id("rec-1")


def test_build_decision_id_differs_by_recommendation_id() -> None:
    assert build_decision_id("rec-1") != build_decision_id("rec-2")


def test_build_decision_id_does_not_depend_on_decision_type() -> None:
    """コードレビュー対応: 横断調査の結果、生産コードでは1つのRecommendationは
    常に単一のDecisionTypeでのみDecisionSnapshotを保存するため、decision_idは
    recommendation_idのみから決定される(decision_typeを含めない)。"""
    recommendation = _recommendation()

    buy_decision = build_decision_snapshot(recommendation, DecisionType.BUY)
    sell_decision = build_decision_snapshot(recommendation, DecisionType.SELL)

    assert buy_decision.decision_id == sell_decision.decision_id


def test_build_decision_snapshot_reexecution_produces_same_decision_id() -> None:
    """同一Recommendationの保存処理が再実行されても、同じdecision_idになる
    (Repositoryのinsert_if_absentと組み合わせて増殖しないことを保証する)。"""
    recommendation = _recommendation()

    first = build_decision_snapshot(recommendation, DecisionType.BUY)
    second = build_decision_snapshot(recommendation, DecisionType.BUY)

    assert first.decision_id == second.decision_id == "decision|rec-1"
