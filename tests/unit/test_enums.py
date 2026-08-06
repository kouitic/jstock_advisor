"""domain/entities/enums.pyの分類ロジックのテスト(コードレビュー対応)。"""

from __future__ import annotations

import pytest

from jstock_advisor.domain.entities.enums import (
    _EXCLUDED_RECOMMENDATION_TYPES,
    _HOLDING_DECISION_RECOMMENDATION_TYPES,
    _LEGACY_SELL_RECOMMENDATION_TYPES,
    BacktestRecommendationSource,
    RecommendationType,
    classify_recommendation_source,
)


def test_all_recommendation_types_are_explicitly_classified() -> None:
    """3集合(すべてリテラル列挙)の和が全RecommendationTypeと一致することを保証する。

    将来RecommendationTypeへメンバーが追加されると、どの集合にも属さないため
    和集合が不一致になりこのテストが失敗する(未知値の無言EXCLUDED落ちを検知する)。
    """
    union = (
        _LEGACY_SELL_RECOMMENDATION_TYPES
        | _HOLDING_DECISION_RECOMMENDATION_TYPES
        | _EXCLUDED_RECOMMENDATION_TYPES
    )
    assert union == frozenset(RecommendationType)


def test_three_classification_sets_are_disjoint() -> None:
    assert not (_LEGACY_SELL_RECOMMENDATION_TYPES & _HOLDING_DECISION_RECOMMENDATION_TYPES)
    assert not (_LEGACY_SELL_RECOMMENDATION_TYPES & _EXCLUDED_RECOMMENDATION_TYPES)
    assert not (_HOLDING_DECISION_RECOMMENDATION_TYPES & _EXCLUDED_RECOMMENDATION_TYPES)


@pytest.mark.parametrize(
    "recommendation_type",
    [RecommendationType.SELL, RecommendationType.URGENT_REVIEW, RecommendationType.REVIEW],
)
def test_legacy_sell_types_classify_as_legacy_sell(
    recommendation_type: RecommendationType,
) -> None:
    assert (
        classify_recommendation_source(recommendation_type)
        == BacktestRecommendationSource.LEGACY_SELL
    )


@pytest.mark.parametrize(
    "recommendation_type",
    [
        RecommendationType.SELL_CONSIDERATION,
        RecommendationType.STRONG_SELL_CONSIDERATION,
        RecommendationType.URGENT_HOLDING_REVIEW,
    ],
)
def test_holding_decision_types_classify_as_holding_decision(
    recommendation_type: RecommendationType,
) -> None:
    assert (
        classify_recommendation_source(recommendation_type)
        == BacktestRecommendationSource.HOLDING_DECISION
    )


@pytest.mark.parametrize(
    "recommendation_type",
    [
        RecommendationType.BUY,
        RecommendationType.WATCH_BUY,
        RecommendationType.HOLD,
        RecommendationType.WATCH,
        RecommendationType.PARTIAL_PROFIT_TAKE,
        RecommendationType.FULL_PROFIT_TAKE,
        RecommendationType.WATCH_BEFORE_EARNINGS,
        RecommendationType.PARTIAL_RISK_REDUCTION,
        RecommendationType.REVIEW_AFTER_EARNINGS,
        RecommendationType.REVIEW_BEFORE_EARNINGS,
        RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW,
    ],
)
def test_unrelated_types_classify_as_excluded(recommendation_type: RecommendationType) -> None:
    assert (
        classify_recommendation_source(recommendation_type) == BacktestRecommendationSource.EXCLUDED
    )


def test_manual_review_required_is_excluded_because_no_service_generates_it() -> None:
    """MANUAL_REVIEW_REQUIREDはgrep全数確認の結果どのサービスも生成していないため、
    生成元が判明するまでLEGACY_SELLへ含めない(コードレビュー対応)。"""
    assert (
        classify_recommendation_source(RecommendationType.MANUAL_REVIEW_REQUIRED)
        == BacktestRecommendationSource.EXCLUDED
    )
