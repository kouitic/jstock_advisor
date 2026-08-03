"""services/watchlist_addition_summary_builder.py: Presentation DTO組み立てのテスト
(LINE通知品質改善)。

このモジュールが通知チャネル(LINE等)を一切知らないこと、順位母数を
WatchlistAdditionSummary.ranked_countへ一元化していること(RankingResultとの
二重保持がないこと)、追加理由がスコア詳細から動的生成されることを確認する。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import WatchlistRegistrationSource
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.domain.ranking import RankingCalculator
from jstock_advisor.domain.signals.watchlist_screening import (
    ScoreCriterionValue,
    WatchlistScoreDetail,
)
from jstock_advisor.services.watchlist_addition_summary_builder import (
    build_evaluation_highlights,
    build_watchlist_addition_summary,
    describe_screening_policy_conditions,
)

_CONFIG = load_config()
_SCORING = _CONFIG.watchlist_screening.scoring
_THRESHOLDS = _CONFIG.watchlist_screening.thresholds
_NOW = dt.datetime(2026, 8, 1, 13, 30, tzinfo=dt.UTC)


def _item(stock_code: str, stock_name: str | None) -> WatchlistItem:
    return WatchlistItem(
        stock_code=stock_code,
        stock_name=stock_name,
        reason="",
        registration_source=WatchlistRegistrationSource.AUTO_SCREENING,
        registration_policy="high_dividend_financial_health",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _detail(stock_code: str, criteria: list[ScoreCriterionValue]) -> WatchlistScoreDetail:
    return WatchlistScoreDetail(stock_code=stock_code, criteria=criteria)


def test_build_summary_uses_real_batch_breakdown_counts() -> None:
    """今回の実績値(対象98・評価可能90・データ未検出8・合格2)を使い、
    ranked_count/data_unavailable_countがそのまま反映されることを確認する。"""
    added_items = [_item("2121", "MIXI"), _item("2108", "日本甜菜製糖")]
    total_score_by_code = {"2121": 67.0, "2108": 63.0}
    rank_by_code = RankingCalculator.rank(total_score_by_code)

    summary = build_watchlist_addition_summary(
        added_items=added_items,
        total_score_by_code=total_score_by_code,
        notification_detail_by_code={},
        rank_by_code=rank_by_code,
        total_target_count=98,
        ranked_count=90,
        data_unavailable_count=8,
        policy_name="high_dividend_financial_health",
        scoring_config=_SCORING,
        thresholds_config=_THRESHOLDS,
        evaluated_at=_NOW,
    )

    assert summary.total_target_count == 98
    assert summary.ranked_count == 90
    assert summary.data_unavailable_count == 8
    assert summary.added_count == 2
    assert summary.addition_rate_pct == 2 / 98 * 100


def test_build_summary_items_ordered_by_score_descending() -> None:
    added_items = [_item("1111", "ロー"), _item("2222", "ハイ")]
    total_score_by_code = {"1111": 60.0, "2222": 90.0}
    rank_by_code = RankingCalculator.rank(total_score_by_code)

    summary = build_watchlist_addition_summary(
        added_items=added_items,
        total_score_by_code=total_score_by_code,
        notification_detail_by_code={},
        rank_by_code=rank_by_code,
        total_target_count=2,
        ranked_count=2,
        data_unavailable_count=0,
        policy_name="high_dividend_financial_health",
        scoring_config=_SCORING,
        thresholds_config=_THRESHOLDS,
        evaluated_at=_NOW,
    )

    assert [item.stock_code for item in summary.items] == ["2222", "1111"]
    assert summary.items[0].rank == 1
    assert summary.items[1].rank == 2


def test_build_summary_item_rank_reflects_ranking_among_all_evaluable_candidates() -> None:
    """追加銘柄の順位は「評価可能全銘柄中の順位」であり、追加銘柄同士の
    連番(1,2,...)ではないことを確認する(FAILED_SCORE等の非合格銘柄が
    間に挟まりうる)。"""
    added_items = [_item("2121", "MIXI")]
    total_score_by_code = {"9999": 99.0, "2121": 67.0, "8888": 68.0}
    rank_by_code = RankingCalculator.rank(total_score_by_code)

    summary = build_watchlist_addition_summary(
        added_items=added_items,
        total_score_by_code=total_score_by_code,
        notification_detail_by_code={},
        rank_by_code=rank_by_code,
        total_target_count=3,
        ranked_count=3,
        data_unavailable_count=0,
        policy_name="high_dividend_financial_health",
        scoring_config=_SCORING,
        thresholds_config=_THRESHOLDS,
        evaluated_at=_NOW,
    )

    assert summary.items[0].rank == 3


def test_build_summary_display_name_falls_back_to_stock_code() -> None:
    added_items = [_item("1234", None)]
    total_score_by_code = {"1234": 80.0}
    rank_by_code = RankingCalculator.rank(total_score_by_code)

    summary = build_watchlist_addition_summary(
        added_items=added_items,
        total_score_by_code=total_score_by_code,
        notification_detail_by_code={},
        rank_by_code=rank_by_code,
        total_target_count=1,
        ranked_count=1,
        data_unavailable_count=0,
        policy_name="high_dividend_financial_health",
        scoring_config=_SCORING,
        thresholds_config=_THRESHOLDS,
        evaluated_at=_NOW,
    )

    assert summary.items[0].display_name == "1234"


def test_build_summary_zero_target_count_gives_zero_addition_rate() -> None:
    summary = build_watchlist_addition_summary(
        added_items=[],
        total_score_by_code={},
        notification_detail_by_code={},
        rank_by_code={},
        total_target_count=0,
        ranked_count=0,
        data_unavailable_count=0,
        policy_name="high_dividend_financial_health",
        scoring_config=_SCORING,
        thresholds_config=_THRESHOLDS,
        evaluated_at=_NOW,
    )

    assert summary.addition_rate_pct == 0.0


def test_build_evaluation_highlights_returns_top_three_positive_criteria() -> None:
    detail = _detail(
        "1234",
        [
            ScoreCriterionValue(
                criterion_key="dividend_yield", label="配当利回り", score=30.0, metric_value="6.6%"
            ),
            ScoreCriterionValue(
                criterion_key="equity_ratio", label="自己資本比率", score=20.0, metric_value="65.0%"
            ),
            ScoreCriterionValue(
                criterion_key="payout_ratio", label="配当性向", score=15.0, metric_value="45.2%"
            ),
            ScoreCriterionValue(
                criterion_key="dividend_growth", label="増配実績", score=1.5, metric_value="5年連続"
            ),
            ScoreCriterionValue(
                criterion_key="shareholder_benefit", label="株主優待", score=0.0, metric_value=None
            ),
        ],
    )

    highlights = build_evaluation_highlights(detail)

    assert len(highlights) == 3
    assert [h.label for h in highlights] == ["配当利回り", "自己資本比率", "配当性向"]
    assert all(h.score > 0 for h in highlights)


def test_build_evaluation_highlights_none_detail_returns_empty_list() -> None:
    assert build_evaluation_highlights(None) == []


def test_build_evaluation_highlights_uses_score_when_metric_value_missing() -> None:
    detail = _detail(
        "1234",
        [
            ScoreCriterionValue(
                criterion_key="shareholder_benefit",
                label="株主優待",
                score=7.5,
                metric_value=None,
            )
        ],
    )

    highlights = build_evaluation_highlights(detail)

    assert highlights[0].detail == "7.5点"


def test_describe_screening_policy_conditions_includes_minimum_total_score() -> None:
    lines = describe_screening_policy_conditions(_SCORING, _THRESHOLDS)

    assert any(f"{_SCORING.minimum_total_score:.1f}" in line for line in lines)


def test_module_does_not_import_line_notification_service() -> None:
    """Presentation DTO組み立てモジュールは通知チャネルを一切知らない
    (LINE等をimportするモジュールが存在しないこと。docstring中の言及は許容する)。"""
    import ast
    import inspect

    from jstock_advisor.services import watchlist_addition_summary_builder

    source = inspect.getsource(watchlist_addition_summary_builder)
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert not any("line_notification_service" in module for module in imported_modules)
