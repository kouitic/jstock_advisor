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


def test_build_decision_snapshot_copies_historical_valuation_fields() -> None:
    """判定精度向上機能Phase B(コードレビュー対応): Recommendationの
    historical_valuation_*5フィールド(score/confidence/coverage/
    reason_codes/metrics)がDecisionSnapshotへそのままコピーされる
    (Shadow計測。既存の判定ロジックはこの値を一切参照しない)。DecisionSnapshot
    はRecommendationからのみコピーし、StockSnapshotを直接参照しないこと
    (再計算しないこと)も間接的に確認する。"""
    recommendation = _recommendation(config_values_used={}).model_copy(
        update={
            "historical_valuation_score": 42.5,
            "historical_valuation_confidence": ConfidenceLevel.HIGH,
            "historical_valuation_coverage": 0.75,
            "historical_valuation_reason_codes": ("PBR_INSUFFICIENT_DATA_OR_BASIS_MISMATCH",),
            "historical_valuation_metrics": {"per_score": 42.5, "model_version": "test_v2"},
        }
    )

    decision = build_decision_snapshot(recommendation, DecisionType.BUY)

    assert decision.historical_valuation_score == 42.5
    assert decision.historical_valuation_confidence == ConfidenceLevel.HIGH
    assert decision.historical_valuation_coverage == 0.75
    assert decision.historical_valuation_reason_codes == (
        "PBR_INSUFFICIENT_DATA_OR_BASIS_MISMATCH",
    )
    assert decision.historical_valuation_metrics == {
        "per_score": 42.5,
        "model_version": "test_v2",
    }


def test_build_decision_snapshot_historical_valuation_fields_default_when_unset() -> None:
    """Recommendation側に値が無い場合、DecisionSnapshot側で推測・再計算せず
    未設定のまま(None/空)保存する。"""
    recommendation = _recommendation()

    decision = build_decision_snapshot(recommendation, DecisionType.BUY)

    assert decision.historical_valuation_score is None
    assert decision.historical_valuation_confidence is None
    assert decision.historical_valuation_coverage is None
    assert decision.historical_valuation_reason_codes == ()
    assert decision.historical_valuation_metrics == {}


def test_build_decision_snapshot_copies_timing_score_fields() -> None:
    """判定精度向上機能Phase B第二弾: Recommendationのtiming_*5フィールド
    (score/confidence/coverage/reason_codes/metrics)がDecisionSnapshotへ
    そのままコピーされる(historical_valuation_*と同じパターン)。"""
    recommendation = _recommendation(config_values_used={}).model_copy(
        update={
            "timing_score": -30.5,
            "timing_confidence": ConfidenceLevel.MEDIUM,
            "timing_coverage": 0.5,
            "timing_reason_codes": ("MACD_UNAVAILABLE",),
            "timing_metrics": {"trend_component": -50.0, "model_version": "timing_test_v1"},
        }
    )

    decision = build_decision_snapshot(recommendation, DecisionType.SELL)

    assert decision.timing_score == -30.5
    assert decision.timing_confidence == ConfidenceLevel.MEDIUM
    assert decision.timing_coverage == 0.5
    assert decision.timing_reason_codes == ("MACD_UNAVAILABLE",)
    assert decision.timing_metrics == {
        "trend_component": -50.0,
        "model_version": "timing_test_v1",
    }


def test_build_decision_snapshot_timing_score_fields_default_when_unset() -> None:
    """Recommendation側に値が無い場合、DecisionSnapshot側で推測・再計算せず
    未設定のまま(None/空)保存する。"""
    recommendation = _recommendation()

    decision = build_decision_snapshot(recommendation, DecisionType.BUY)

    assert decision.timing_score is None
    assert decision.timing_confidence is None
    assert decision.timing_coverage is None
    assert decision.timing_reason_codes == ()
    assert decision.timing_metrics == {}


def test_build_decision_snapshot_copies_earnings_surprise_fields() -> None:
    """判定精度向上機能Phase C(コードレビュー対応v2/v3): Recommendationの
    earnings_surprise_*5フィールド(score/confidence/coverage/reason_codes/
    metrics)がDecisionSnapshotへそのままコピーされる。metricsにはraw監査
    情報(matched_quarter_end・eps_actual・eps_estimate・earnings_decision_
    relevance等)が欠落せず含まれることを確認する(historical_valuation_*/
    timing_*と同じパターン)。"""
    recommendation = _recommendation(config_values_used={}).model_copy(
        update={
            "earnings_surprise_score": 50.0,
            "earnings_surprise_confidence": ConfidenceLevel.HIGH,
            "earnings_surprise_coverage": 1.0,
            "earnings_surprise_reason_codes": (),
            "earnings_surprise_metrics": {
                "state": "EVALUATED",
                "analyst_consensus_component": 50.0,
                "matched_quarter_end": "2026-06-30",
                "resolved_financial_period_end": "2026-06-30",
                "eps_actual": "110",
                "eps_estimate": "100",
                "surprise_pct": 0.1,
                "earnings_surprise_source_provider": "yfinance",
                "earnings_surprise_source_fetched_at": "2026-08-10T00:00:00+00:00",
                "release_confirmation_state": "NOT_APPLICABLE",
                "earnings_decision_relevance": "NOT_RELEVANT",
                "model_version": "earnings_surprise_v3",
            },
        }
    )

    decision = build_decision_snapshot(recommendation, DecisionType.BUY)

    assert decision.earnings_surprise_score == 50.0
    assert decision.earnings_surprise_confidence == ConfidenceLevel.HIGH
    assert decision.earnings_surprise_coverage == 1.0
    assert decision.earnings_surprise_metrics["matched_quarter_end"] == "2026-06-30"
    assert decision.earnings_surprise_metrics["eps_actual"] == "110"
    assert decision.earnings_surprise_metrics["eps_estimate"] == "100"
    assert decision.earnings_surprise_metrics["surprise_pct"] == 0.1
    assert decision.earnings_surprise_metrics["earnings_surprise_source_provider"] == "yfinance"
    assert decision.earnings_surprise_metrics["earnings_decision_relevance"] == "NOT_RELEVANT"


def test_build_decision_snapshot_earnings_surprise_fields_default_when_unset() -> None:
    recommendation = _recommendation()

    decision = build_decision_snapshot(recommendation, DecisionType.BUY)

    assert decision.earnings_surprise_score is None
    assert decision.earnings_surprise_confidence is None
    assert decision.earnings_surprise_coverage is None
    assert decision.earnings_surprise_reason_codes == ()
    assert decision.earnings_surprise_metrics == {}


def test_build_decision_snapshot_copies_earnings_trend_fields() -> None:
    """判定精度向上機能Phase C(コードレビュー対応v2/v3・第3回): Recommendationの
    earnings_trend_*5フィールドがDecisionSnapshotへそのままコピーされる。
    metricsにはraw監査情報(before/after値・change_pct・recent_periods_source・
    period_end/period_type・earnings_decision_relevance・
    release_confirmation_state)が欠落せず含まれることを確認する
    (dict丸ごとコピーのため、新規キーを追加した第3回対応でも失われない)。"""
    recommendation = _recommendation(config_values_used={}).model_copy(
        update={
            "earnings_trend_score": -30.0,
            "earnings_trend_confidence": ConfidenceLevel.MEDIUM,
            "earnings_trend_coverage": 0.6,
            "earnings_trend_reason_codes": ("ANNUAL_FALLBACK_USED",),
            "earnings_trend_metrics": {
                "state": "EVALUATED",
                "operating_income_trend_component": -50.0,
                "latest_operating_income": "80",
                "previous_operating_income": "100",
                "operating_income_change_pct": -20.0,
                "recent_periods_source": "ANNUAL_FALLBACK",
                "latest_operating_income_period_end": "2026-03-31",
                "previous_operating_income_period_end": "2025-03-31",
                "operating_income_period_type": "ANNUAL",
                "earnings_decision_relevance": "NOT_RELEVANT",
                "release_confirmation_state": "DATA_UPDATED",
                "model_version": "earnings_trend_v3",
            },
        }
    )

    decision = build_decision_snapshot(recommendation, DecisionType.SELL)

    assert decision.earnings_trend_score == -30.0
    assert decision.earnings_trend_confidence == ConfidenceLevel.MEDIUM
    assert decision.earnings_trend_coverage == 0.6
    assert decision.earnings_trend_reason_codes == ("ANNUAL_FALLBACK_USED",)
    assert decision.earnings_trend_metrics["latest_operating_income"] == "80"
    assert decision.earnings_trend_metrics["previous_operating_income"] == "100"
    assert decision.earnings_trend_metrics["operating_income_change_pct"] == -20.0
    assert decision.earnings_trend_metrics["recent_periods_source"] == "ANNUAL_FALLBACK"
    assert decision.earnings_trend_metrics["latest_operating_income_period_end"] == "2026-03-31"
    assert decision.earnings_trend_metrics["previous_operating_income_period_end"] == "2025-03-31"
    assert decision.earnings_trend_metrics["operating_income_period_type"] == "ANNUAL"
    assert decision.earnings_trend_metrics["earnings_decision_relevance"] == "NOT_RELEVANT"
    assert decision.earnings_trend_metrics["release_confirmation_state"] == "DATA_UPDATED"


def test_build_decision_snapshot_earnings_trend_fields_default_when_unset() -> None:
    recommendation = _recommendation()

    decision = build_decision_snapshot(recommendation, DecisionType.BUY)

    assert decision.earnings_trend_score is None
    assert decision.earnings_trend_confidence is None
    assert decision.earnings_trend_coverage is None
    assert decision.earnings_trend_reason_codes == ()
    assert decision.earnings_trend_metrics == {}


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


def test_build_decision_snapshot_copies_company_quality_score_model_version() -> None:
    """Issue #22 Phase 3.5: 買い側品質スコアのモデル版がRecommendationから
    DecisionSnapshotへコピーされる(model_version=Decision Enhancement Layer
    全体の版とは別概念。config_values_used経由でscoring_weights等が間接流入
    しているため、将来のv1/v2混在集計防止にversionを併せて記録する)。"""
    recommendation = _recommendation()
    decision = build_decision_snapshot(recommendation, DecisionType.BUY)
    assert recommendation.company_quality_score_model_version == "v1"
    assert decision.company_quality_score_model_version == "v1"
    # Decision Enhancement Layer全体の版(model_version)とは独立に保持される
    assert decision.model_version != decision.company_quality_score_model_version
