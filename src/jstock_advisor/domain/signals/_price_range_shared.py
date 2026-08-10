"""Entry/Exit Price Range Shadow(判定精度向上機能次フェーズSTEP2)の、
両モジュール(entry_price_range.py/exit_price_range.py)で共有する
最小限のヘルパーのみを切り出したモジュール。

Fair Value confidenceとoverlay(Historical Valuation/Timing等)confidenceの
組み合わせは、必ず「弱い方」を採用する(片方が高信頼でももう片方が低信頼
なら全体としては低信頼として扱う、保守的な合成)。ConfidenceLevel同士の
比較は文字列比較ではなく明示的なrank dictで行う(StrEnumの並び順に依存しない)。
"""

from __future__ import annotations

from jstock_advisor.domain.entities.enums import ConfidenceLevel

_CONFIDENCE_RANK: dict[ConfidenceLevel, int] = {
    ConfidenceLevel.LOW: 0,
    ConfidenceLevel.MEDIUM: 1,
    ConfidenceLevel.HIGH: 2,
}


def weaker_confidence(a: ConfidenceLevel, b: ConfidenceLevel) -> ConfidenceLevel:
    """2つのConfidenceLevelのうち、より弱い(信頼度の低い)方を返す。"""
    return a if _CONFIDENCE_RANK[a] <= _CONFIDENCE_RANK[b] else b
