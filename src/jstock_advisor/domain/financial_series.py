"""四半期/年次の財務系列を季節性の影響を抑えた形に変換するユーティリティ。

実データ検証により、yfinance等から取得できる財務データは超大型株のみ四半期粒度
(period_endの間隔が約90日)で、それ以外の銘柄は年次粒度(約365日)でしか取得
できないことが分かっている。同じ「直近N期連続悪化」判定ロジックを両方の粒度に
安全に適用するため、四半期粒度の場合は直近12ヶ月移動合計(TTM: Trailing Twelve
Months)の系列に変換し、季節性(業種特有の繁閑差)を打ち消す。年次粒度の場合は
各値が既に12ヶ月分を表すため、変換は恒等写像(そのまま)となる。

一時的な特別損益(本決算特有のイレギュラーな値動き)については、この変換だけでは
解消しない。ただし呼び出し側の判定ロジック(continuous_operating_income_decline等)
が「連続悪化」を要求する設計になっているため、1期限りの特別要因は「連続」の
条件を満たさず誤検知しにくい。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.domain.entities.enums import PeriodType

_QUARTERLY_MAX_AVERAGE_GAP_DAYS = 200
_TTM_WINDOW = 4


@dataclass(frozen=True)
class FinancialPeriodValue:
    """期間種別を明示した財務系列1点(2026-07仕様レビュー対応)。

    period_type違いの値同士は比較しない(QUARTERとANNUALの比較禁止・
    単独四半期と累計値の比較禁止・年次フォールバック値をquarterlyとして
    扱うことの禁止)。fiscal_quarterは実データからは安定して算出できない
    ため常にNone(恒久的な制約。真の四半期同期(YoY)比較は未実装)。
    """

    value: Decimal
    period_end: dt.date
    period_type: PeriodType
    period_start: dt.date | None = None
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None
    is_cumulative: bool = False
    source: str | None = None


def is_quarterly_cadence(period_ends: list[dt.date]) -> bool:
    """period_endの平均間隔から四半期粒度かどうかを判定する(四半期≒91日、年次≒365日)。"""
    if len(period_ends) < 2:
        return False
    gaps = [(period_ends[i] - period_ends[i - 1]).days for i in range(1, len(period_ends))]
    if not gaps:
        return False
    average_gap = sum(gaps) / len(gaps)
    return 0 < average_gap < _QUARTERLY_MAX_AVERAGE_GAP_DAYS


def to_seasonally_adjusted_series(
    values: list[Decimal | None], period_ends: list[dt.date]
) -> list[Decimal | None]:
    """四半期粒度なら直近12ヶ月移動合計(TTM)の系列に変換し、季節性を打ち消す。

    年次粒度、またはTTMを計算するのに十分な四半期数(4期分)が無い場合はそのまま返す。
    valuesとperiod_endsは同じ長さ・同じ順序(古い→新しい)である前提。
    ウィンドウ内にNoneが1つでもあれば、その時点のTTM値はNoneとする(推測で補完しない)。
    """
    if len(values) != len(period_ends):
        raise ValueError("values and period_ends must have the same length")

    if not is_quarterly_cadence(period_ends):
        return list(values)

    if len(values) < _TTM_WINDOW:
        return []

    ttm: list[Decimal | None] = []
    for i in range(_TTM_WINDOW - 1, len(values)):
        window = values[i - _TTM_WINDOW + 1 : i + 1]
        non_null_window = [v for v in window if v is not None]
        if len(non_null_window) < len(window):
            ttm.append(None)
        else:
            ttm.append(sum(non_null_window, Decimal("0")))
    return ttm


def build_financial_period_series(
    values: list[Decimal | None], period_ends: list[dt.date], source: str | None = None
) -> list[FinancialPeriodValue]:
    """to_seasonally_adjusted_series()の変換結果に、明示的なperiod_typeを付与する。

    四半期粒度は変換後TTM(直近12ヶ月移動合計)、年次粒度はANNUALとしてタグ付けする。
    どちらも「約1年分の値」という点で意味的には比較可能だが、period_type自体は
    区別して保持し、呼び出し側(継続悪化判定)で異なるperiod_type同士を誤って
    比較しないようにする。
    """
    if len(values) != len(period_ends):
        raise ValueError("values and period_ends must have the same length")

    quarterly = is_quarterly_cadence(period_ends)
    adjusted = to_seasonally_adjusted_series(values, period_ends)

    result: list[FinancialPeriodValue] = []
    if quarterly:
        if len(values) < _TTM_WINDOW:
            return []
        for offset, value in enumerate(adjusted):
            period_end = period_ends[_TTM_WINDOW - 1 + offset]
            if value is None:
                continue
            result.append(
                FinancialPeriodValue(
                    value=value,
                    period_end=period_end,
                    period_type=PeriodType.TTM,
                    fiscal_year=period_end.year,
                    is_cumulative=False,
                    source=source,
                )
            )
    else:
        for value, period_end in zip(adjusted, period_ends, strict=True):
            if value is None:
                continue
            result.append(
                FinancialPeriodValue(
                    value=value,
                    period_end=period_end,
                    period_type=PeriodType.ANNUAL,
                    fiscal_year=period_end.year,
                    is_cumulative=False,
                    source=source,
                )
            )
    return result
