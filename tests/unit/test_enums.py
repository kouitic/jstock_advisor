"""domain/entities/enums.pyの分類ロジックのテスト(コードレビュー対応)。"""

from __future__ import annotations

import pytest

from jstock_advisor.domain.entities.enums import (
    _EXCLUDED_RECOMMENDATION_TYPES,
    _HOLDING_DECISION_RECOMMENDATION_TYPES,
    _LEGACY_SELL_RECOMMENDATION_TYPES,
    FULL_SELL_RECOMMENDATION_TYPES,
    BacktestRecommendationSource,
    HoldingSummaryAction,
    RecommendationType,
    classify_recommendation_source,
    is_full_sell_like,
    resolve_holding_summary_action,
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


# ===== 横断整合性レビュー対応(2026-08、指摘3): 全部売却検討の分類統一 =====


def test_full_sell_recommendation_types_is_exactly_strong_sell_and_full_profit_take() -> None:
    """個別LINE通知本文(recommendation_adapter.py)とまとめ通知集計
    (holdings_watchlist_handler.py)が同じ判定ソースを共有するための唯一の
    集合。意図せずメンバーが増減しないことを固定する。"""
    expected = frozenset(
        {
            RecommendationType.FULL_PROFIT_TAKE,
            RecommendationType.STRONG_SELL_CONSIDERATION,
        }
    )
    assert expected == FULL_SELL_RECOMMENDATION_TYPES


@pytest.mark.parametrize(
    "recommendation_type",
    [RecommendationType.FULL_PROFIT_TAKE, RecommendationType.STRONG_SELL_CONSIDERATION],
)
def test_is_full_sell_like_true_for_full_sell_types(
    recommendation_type: RecommendationType,
) -> None:
    assert is_full_sell_like(recommendation_type) is True


@pytest.mark.parametrize(
    "recommendation_type",
    [
        RecommendationType.SELL,
        RecommendationType.SELL_CONSIDERATION,
        RecommendationType.PARTIAL_PROFIT_TAKE,
        RecommendationType.PARTIAL_RISK_REDUCTION,
        RecommendationType.URGENT_REVIEW,
        RecommendationType.URGENT_HOLDING_REVIEW,
    ],
)
def test_is_full_sell_like_false_for_non_full_sell_types(
    recommendation_type: RecommendationType,
) -> None:
    assert is_full_sell_like(recommendation_type) is False


# ===== 再コードレビュー対応(2026-08、追加修正4): resolve_holding_summary_action =====


@pytest.mark.parametrize(
    ("recommendation_type", "expected_action"),
    [
        (RecommendationType.PARTIAL_PROFIT_TAKE, HoldingSummaryAction.PARTIAL),
        (RecommendationType.PARTIAL_RISK_REDUCTION, HoldingSummaryAction.PARTIAL),
        (RecommendationType.FULL_PROFIT_TAKE, HoldingSummaryAction.FULL),
        (RecommendationType.STRONG_SELL_CONSIDERATION, HoldingSummaryAction.FULL),
        (RecommendationType.SELL, HoldingSummaryAction.SELL),
        (RecommendationType.SELL_CONSIDERATION, HoldingSummaryAction.SELL),
        (RecommendationType.URGENT_REVIEW, HoldingSummaryAction.CRITICAL),
        (RecommendationType.URGENT_HOLDING_REVIEW, HoldingSummaryAction.CRITICAL),
    ],
)
def test_resolve_holding_summary_action_classifies_known_types(
    recommendation_type: RecommendationType, expected_action: HoldingSummaryAction
) -> None:
    assert resolve_holding_summary_action(recommendation_type) is expected_action


@pytest.mark.parametrize(
    "recommendation_type",
    [
        RecommendationType.BUY,
        RecommendationType.WATCH_BUY,
        RecommendationType.HOLD,
        RecommendationType.WATCH,
        RecommendationType.REVIEW,
        RecommendationType.MANUAL_REVIEW_REQUIRED,
        RecommendationType.WATCH_BEFORE_EARNINGS,
        RecommendationType.REVIEW_AFTER_EARNINGS,
        RecommendationType.REVIEW_BEFORE_EARNINGS,
        RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW,
    ],
)
def test_resolve_holding_summary_action_returns_none_for_unrelated_types(
    recommendation_type: RecommendationType,
) -> None:
    """ATTENTION(NotificationIntentという別軸)を含め、保有株サマリーの4分類
    (一部売却/全部売却/売却/緊急確認)に属さない型はNoneを返す(このRecommendation
    Typeがholdings_watchlist_handler.pyの4分類集計へ混入しないことを保証する)。
    """
    assert resolve_holding_summary_action(recommendation_type) is None


def test_resolve_holding_summary_action_covers_every_recommendation_type() -> None:
    """全RecommendationTypeについて例外なく分類できる(4分類のいずれか、または
    None)ことを保証する(将来のメンバー追加による無言の分岐漏れを検知する)。"""
    for recommendation_type in RecommendationType:
        result = resolve_holding_summary_action(recommendation_type)
        assert result is None or result in HoldingSummaryAction
