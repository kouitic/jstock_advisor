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
    # Timing Score(判定精度向上機能Phase B第二弾)コードレビュー対応: classify_trend()は
    # ma20/ma60/ma20_slope_pctのいずれかが欠損している場合、安全側フォールバックとして
    # trend_classification=NEUTRALを返す。この「データ不足によるNEUTRAL」と「本当に
    # 中立」をtrend_classification単体からは区別できないため、判定に使った入力が
    # 全て揃っていたかを明示的に保持する(推測で「評価できた」ことにしない)。
    trend_evaluable: bool
    # Timing Score用(既存PriceBarのみから算出、新規Provider呼び出しは行わない)。
    one_day_return_pct: float | None = None
    five_day_return_pct: float | None = None
