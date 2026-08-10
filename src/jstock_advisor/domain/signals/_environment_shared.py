"""判定精度向上機能Phase D(Market/Sector/Environment Composite)の、
market_environment.py/sector_environment.py/environment.pyで共有する最小限の
ヘルパーのみを切り出したモジュール。

Entry/Exit Price Range用の_price_range_shared.pyとは意味の異なる用途
(価格レンジの信頼度合成 vs 環境スコアの信頼度合成)のため、あえて別モジュール
として独立させる(処理内容は同型だが概念上は独立)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.interfaces.types import PriceBar

_CONFIDENCE_RANK: dict[ConfidenceLevel, int] = {
    ConfidenceLevel.LOW: 0,
    ConfidenceLevel.MEDIUM: 1,
    ConfidenceLevel.HIGH: 2,
}


def weaker_confidence(a: ConfidenceLevel, b: ConfidenceLevel) -> ConfidenceLevel:
    """2つのConfidenceLevelのうち、より弱い(信頼度の低い)方を返す。"""
    return a if _CONFIDENCE_RANK[a] <= _CONFIDENCE_RANK[b] else b


def cap_confidence(value: ConfidenceLevel, cap: ConfidenceLevel) -> ConfidenceLevel:
    """valueがcapより高信頼なら、capまで引き下げる(cap未満ならそのまま)。"""
    return value if _CONFIDENCE_RANK[value] <= _CONFIDENCE_RANK[cap] else cap


def filter_future_bars(bars: list[PriceBar], as_of_date: dt.date) -> tuple[list[PriceBar], bool]:
    """as_of_dateより未来の日付を持つPriceBarを除外する(look-ahead bias対策、
    momentum.pyのeffective_bars算出と同一ロジック)。戻り値は
    (未来バー除外後のbars, 1件以上除外されたか)。"""
    effective = [b for b in bars if b.date <= as_of_date]
    return effective, len(effective) < len(bars)


def clamp_score(value: float) -> float:
    """-100〜100へclampする。"""
    return max(-100.0, min(100.0, value))
