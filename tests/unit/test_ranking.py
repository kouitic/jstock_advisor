"""domain/ranking.py: RankingCalculator/RankingResultのテスト(LINE通知品質改善)。

RankingCalculatorの責務はscore→rankのみに限定されており、順位の母数(総件数)は
結果に含まれない(WatchlistAdditionSummary.ranked_count側で一元管理する)。
"""

from __future__ import annotations

from jstock_advisor.domain.ranking import RankingCalculator, RankingResult


def test_rank_orders_by_score_descending() -> None:
    result = RankingCalculator.rank({"1111": 60.0, "2222": 90.0, "3333": 75.0})

    assert result["2222"].rank == 1
    assert result["3333"].rank == 2
    assert result["1111"].rank == 3


def test_rank_breaks_ties_by_stock_code_ascending() -> None:
    result = RankingCalculator.rank({"2222": 80.0, "1111": 80.0})

    assert result["1111"].rank == 1
    assert result["2222"].rank == 2


def test_rank_empty_dict_returns_empty_dict() -> None:
    assert RankingCalculator.rank({}) == {}


def test_ranking_result_has_only_rank_field() -> None:
    """RankingResultはrankフィールドのみを持ち、totalのような母数フィールドを
    持たない(修正⑤、案A採用)。"""
    assert set(RankingResult.model_fields) == {"rank"}


def test_rank_count_matches_input_size() -> None:
    total_score_by_code = {f"{1000 + i}": float(i) for i in range(90)}
    result = RankingCalculator.rank(total_score_by_code)

    assert len(result) == 90
    assert {r.rank for r in result.values()} == set(range(1, 91))
