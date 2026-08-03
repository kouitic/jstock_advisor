"""汎用的な「スコア→順位」計算(ウォッチリスト通知品質改善)。

責務はscore→rankのみに限定する。順位の母数(総件数)はこの計算結果には含めず、
呼び出し側(Presentation DTO)が別途保持する(RankingResultと母数の二重保持を
避けるため)。ウォッチリストの追加銘柄選定ロジック(WatchlistScreeningService.rank()/
rank_and_limit())とは無関係の、表示用の順位計算専用コンポーネント。
"""

from __future__ import annotations

from pydantic import BaseModel


class RankingResult(BaseModel):
    rank: int


class RankingCalculator:
    @staticmethod
    def rank(total_score_by_code: dict[str, float]) -> dict[str, RankingResult]:
        """スコア降順、同点はキー(銘柄コード)昇順で順位を割り当てる。"""
        ordered = sorted(total_score_by_code.items(), key=lambda kv: (-kv[1], kv[0]))
        return {
            stock_code: RankingResult(rank=rank)
            for rank, (stock_code, _score) in enumerate(ordered, start=1)
        }
