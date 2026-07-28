"""モメンタム・トレンド層のスナップショット(要求仕様9節)。"""

from __future__ import annotations

from decimal import Decimal

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.enums import ConfidenceLevel, TrendClassification


class MacdResult(ImmutableSnapshot):
    macd_line: Decimal
    signal_line: Decimal
    histogram: Decimal


class MomentumSnapshot(ImmutableSnapshot):
    ma20: Decimal | None = None
    ma60: Decimal | None = None
    ma120: Decimal | None = None
    ma200: Decimal | None = None
    ma20_slope_pct: float | None = None
    high_20d: Decimal | None = None
    high_60d: Decimal | None = None
    drawdown_from_recent_high_pct: float | None = None
    volume_ratio: float | None = None
    rsi: float | None = None
    macd: MacdResult | None = None
    relative_strength_vs_topix_pct: float | None = None
    relative_strength_vs_sector_pct: float | None = None
    trailing_stop_reference_price: Decimal | None = None
    trend_classification: TrendClassification
    confidence: ConfidenceLevel
