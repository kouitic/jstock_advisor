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
    # Timing Score(コードレビュー対応v3): current_price(get_latest_price()由来)と
    # bars(get_price_history()由来)は別Provider呼び出しであり、時点が一致する
    # 保証がコード上に無い。判定はas_of_date以前のバーだけに絞ったeffective_bars
    # 基準で行う(コードレビュー対応v4、下記price_history_has_future_bars参照)。
    # effective_bars[-1].dateがas_of_dateと一致しない(=historyがcurrent_priceより
    # 古い)場合はFalse(この場合one_day_return_pct/five_day_return_pctは補完せず
    # Noneのまま。effective_barsが空でそもそも比較対象が無い場合はTrueのまま)。
    price_history_aligned: bool
    # Timing Score(コードレビュー対応v4): barsにas_of_date(current_priceの
    # 実際のas-of日付)より未来の日付を持つPriceBarが混入していたためtechnical
    # 計算から除外した場合True。price_history_alignedとは独立した情報であり
    # (未来バーを除外した結果、残りのeffective_barsがas_of_dateと整合する
    # ケースもある)、監査上「未来バー混入」と「historyが古い」を区別するために
    # 保持する。
    price_history_has_future_bars: bool
