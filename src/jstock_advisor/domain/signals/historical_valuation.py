"""判定精度向上機能Phase B: Historical Valuation Score(自己過去比較スコア)。

銘柄自身の過去PER/PBR水準に対して、現在の値がどの位置にあるかを
-100(過去最高値=最も割高)〜+100(過去最安値=最も割安)のランクベース
スコアで表す。同業他社・市場平均とは比較しない(自己過去比較のみ、
docs/functional_spec.md §15の既存制約を踏襲)。

yfinance等から取得できる過去バリュエーションデータは実質年次数点程度と
少ないため、平均・標準偏差ベースの手法(外れ値・少数データに弱い)ではなく、
ランク(パーセンタイル)ベースの手法を採用する。algorithm自体は外部I/Oを
一切行わない純関数(domain/signals/momentum.pyと同じパターン)。

コードレビュー対応(Shadow計測): このスコアはDecisionSnapshot(判定精度向上
機能Phase Aの自己評価基盤)へ記録する専用のものであり、BUY候補判定・保有判断
スコア・旧売却判定・ProfitTaking判定・LINE通知など既存の判定ロジックからは
一切参照されない。
"""

from __future__ import annotations

from decimal import Decimal

from jstock_advisor.config.models import HistoricalValuationRulesConfig
from jstock_advisor.interfaces.types import HistoricalValuation


def _valid_values(historical_valuations: list[HistoricalValuation], field: str) -> list[Decimal]:
    """0より大きい有効な値のみを抽出する(fair_value.py::median_historical_per/pbr
    と同じ除外条件を独立して適用する)。"""
    values: list[Decimal] = []
    for v in historical_valuations:
        value = getattr(v, field)
        if value is not None and value > 0:
            values.append(value)
    return values


def _percentile_rank_score(current: Decimal, historical: list[Decimal]) -> float:
    """現在値が過去の値と比べてどの水準にあるかを-100〜+100で表す。

    historicalのうちcurrent以上の値の割合pを求め、(p - 0.5) * 200で変換する。
    pが高い(=過去の多くの値が現在値以上)ほど、現在値は過去と比べて割安と
    判断し、スコアは+100に近づく。逆にpが低い(過去のほとんどの値より現在値が
    高い)ほど、現在値は割高と判断しスコアは-100に近づく。
    """
    count_at_or_above = sum(1 for h in historical if h >= current)
    p = count_at_or_above / len(historical)
    return (p - 0.5) * 200


def compute_historical_valuation_score(
    historical_valuations: list[HistoricalValuation],
    current_per: Decimal | None,
    current_pbr: Decimal | None,
    config: HistoricalValuationRulesConfig,
) -> float | None:
    """銘柄自身の過去PER/PBR水準に対する現在値のランクベーススコアを算出する。

    PER・PBRそれぞれについて、現在値が存在し、かつ有効な過去データ点数が
    config.min_data_points_required以上ある場合のみコンポーネントスコアを
    計算する。利用可能なコンポーネントのみをper_weight/pbr_weightで加重平均し
    (片方しか無ければその重みだけで正規化する)、両方とも算出不可の場合は
    Noneを返す(推測で補完しない)。
    """
    components: list[tuple[float, float]] = []  # (score, weight)

    historical_pers = _valid_values(historical_valuations, "per")
    if (
        current_per is not None
        and current_per > 0
        and len(historical_pers) >= config.min_data_points_required
    ):
        components.append((_percentile_rank_score(current_per, historical_pers), config.per_weight))

    historical_pbrs = _valid_values(historical_valuations, "pbr")
    if (
        current_pbr is not None
        and current_pbr > 0
        and len(historical_pbrs) >= config.min_data_points_required
    ):
        components.append((_percentile_rank_score(current_pbr, historical_pbrs), config.pbr_weight))

    if not components:
        return None

    total_weight = sum(weight for _, weight in components)
    if total_weight <= 0:
        return None
    return sum(score * weight for score, weight in components) / total_weight
