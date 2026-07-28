"""モメンタム・トレンド層(要求仕様9節・10節)。

上昇トレンドが強い場合、割高評価だけで即座に全利確しない/一部利確やWATCHを
優先する、といったタイミング判断に使う。ファンダメンタル評価とは独立した
軸として扱い、上昇トレンドだけを理由に割高評価そのものを無効化しない
(利用側でfundamental_action/timing_action/final_actionを分離すること)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.config.models import MomentumRulesConfig
from jstock_advisor.domain.entities.enums import ConfidenceLevel, TrendClassification
from jstock_advisor.domain.entities.momentum import MacdResult, MomentumSnapshot
from jstock_advisor.interfaces.types import PriceBar


def compute_moving_average(bars: list[PriceBar], window: int) -> Decimal | None:
    if len(bars) < window:
        return None
    closes = [b.close for b in bars[-window:]]
    return sum(closes, Decimal("0")) / window


def compute_ma_slope_pct(bars: list[PriceBar], window: int, lookback_days: int) -> float | None:
    """window日移動平均の、lookback_days日前と比較した変化率(%)。"""
    if len(bars) < window + lookback_days:
        return None
    current_ma = compute_moving_average(bars, window)
    past_bars = bars[: len(bars) - lookback_days]
    past_ma = compute_moving_average(past_bars, window)
    if current_ma is None or past_ma is None or past_ma == 0:
        return None
    return float(current_ma / past_ma - 1) * 100


def compute_high_over_window(bars: list[PriceBar], window_days: int) -> Decimal | None:
    if not bars:
        return None
    recent = bars[-window_days:] if len(bars) >= window_days else bars
    if not recent:
        return None
    return max(b.high for b in recent)


def compute_drawdown_from_recent_high_pct(
    bars: list[PriceBar], current_price: Decimal, window_days: int
) -> float | None:
    high = compute_high_over_window(bars, window_days)
    if high is None or high == 0:
        return None
    return float(current_price / high - 1) * 100


def compute_volume_ratio(
    bars: list[PriceBar], short_window_days: int, long_window_days: int
) -> float | None:
    if len(bars) < long_window_days:
        return None
    short_avg = sum((b.volume for b in bars[-short_window_days:]), 0) / short_window_days
    long_avg = sum((b.volume for b in bars[-long_window_days:]), 0) / long_window_days
    if long_avg == 0:
        return None
    return short_avg / long_avg


def compute_rsi(bars: list[PriceBar], period: int) -> float | None:
    """単純移動平均によるRSI近似値(Wilderの平滑化は行わない簡易版)。"""
    if len(bars) < period + 1:
        return None
    closes = [b.close for b in bars[-(period + 1) :]]
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        if change > 0:
            gains.append(change)
        elif change < 0:
            losses.append(-change)
    avg_gain = sum(gains, Decimal("0")) / period
    avg_loss = sum(losses, Decimal("0")) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def _ema_series(values: list[Decimal], period: int) -> list[Decimal]:
    if len(values) < period:
        return []
    multiplier = Decimal("2") / (period + 1)
    ema = [sum(values[:period], Decimal("0")) / period]
    for v in values[period:]:
        ema.append((v - ema[-1]) * multiplier + ema[-1])
    return ema


def compute_macd(
    bars: list[PriceBar], fast_period: int, slow_period: int, signal_period: int
) -> MacdResult | None:
    closes = [b.close for b in bars]
    if len(closes) < slow_period + signal_period:
        return None
    ema_fast = _ema_series(closes, fast_period)
    ema_slow = _ema_series(closes, slow_period)
    offset = len(ema_fast) - len(ema_slow)
    if offset < 0:
        return None
    macd_line_series = [ema_fast[i + offset] - ema_slow[i] for i in range(len(ema_slow))]
    signal_series = _ema_series(macd_line_series, signal_period)
    if not signal_series:
        return None
    macd_value = macd_line_series[-1]
    signal_value = signal_series[-1]
    return MacdResult(
        macd_line=macd_value, signal_line=signal_value, histogram=macd_value - signal_value
    )


def compute_relative_strength_pct(
    bars: list[PriceBar], benchmark_bars: list[PriceBar], window_days: int
) -> float | None:
    """自銘柄とベンチマークの、window_days日間のリターン差(パーセントポイント)。"""
    if len(bars) < window_days + 1 or len(benchmark_bars) < window_days + 1:
        return None
    stock_return = float(bars[-1].close / bars[-(window_days + 1)].close - 1) * 100
    benchmark_return = (
        float(benchmark_bars[-1].close / benchmark_bars[-(window_days + 1)].close - 1) * 100
    )
    return stock_return - benchmark_return


def compute_trailing_stop_reference_price(
    bars: list[PriceBar], window_days: int, trailing_pct: float
) -> Decimal | None:
    high = compute_high_over_window(bars, window_days)
    if high is None:
        return None
    return high * (1 - Decimal(str(trailing_pct)) / 100)


def classify_trend(
    current_price: Decimal,
    ma20: Decimal | None,
    ma60: Decimal | None,
    ma20_slope_pct: float | None,
    rsi: float | None,
    strong_trend_rsi_threshold: float,
) -> TrendClassification:
    if ma20 is None or ma60 is None or ma20_slope_pct is None:
        return TrendClassification.NEUTRAL
    if current_price > ma20 > ma60 and ma20_slope_pct > 0:
        if rsi is not None and rsi >= strong_trend_rsi_threshold:
            return TrendClassification.STRONG_UPTREND
        return TrendClassification.UPTREND
    if current_price < ma20 < ma60 and ma20_slope_pct < 0:
        if rsi is not None and rsi <= (100 - strong_trend_rsi_threshold):
            return TrendClassification.STRONG_DOWNTREND
        return TrendClassification.DOWNTREND
    return TrendClassification.NEUTRAL


def compute_momentum_snapshot(
    bars: list[PriceBar],
    current_price: Decimal,
    as_of_date: dt.date,
    config: MomentumRulesConfig,
    benchmark_bars: list[PriceBar] | None = None,
    sector_bars: list[PriceBar] | None = None,
) -> MomentumSnapshot:
    ma20 = compute_moving_average(bars, 20)
    ma60 = compute_moving_average(bars, 60)
    ma120 = compute_moving_average(bars, 120)
    ma200 = compute_moving_average(bars, 200)
    ma20_slope_pct = compute_ma_slope_pct(bars, 20, config.moving_averages.slope_lookback_days)
    high_20d = compute_high_over_window(bars, config.high_low.high_window_days_short)
    high_60d = compute_high_over_window(bars, config.high_low.high_window_days_long)
    drawdown = compute_drawdown_from_recent_high_pct(
        bars, current_price, config.high_low.drawdown_window_days
    )
    volume_ratio = compute_volume_ratio(
        bars, config.volume.short_window_days, config.volume.long_window_days
    )
    rsi = compute_rsi(bars, config.rsi.period)
    macd = compute_macd(
        bars, config.macd.fast_period, config.macd.slow_period, config.macd.signal_period
    )
    relative_strength_topix = (
        compute_relative_strength_pct(bars, benchmark_bars, config.high_low.high_window_days_long)
        if benchmark_bars
        else None
    )
    relative_strength_sector = (
        compute_relative_strength_pct(bars, sector_bars, config.high_low.high_window_days_long)
        if sector_bars
        else None
    )
    trailing_stop = compute_trailing_stop_reference_price(
        bars, config.high_low.high_window_days_long, config.trailing_stop.trailing_pct
    )
    trend = classify_trend(
        current_price,
        ma20,
        ma60,
        ma20_slope_pct,
        rsi,
        config.trend_classification.strong_trend_rsi_threshold,
    )
    available_signals = sum(
        1 for v in (ma20, ma60, ma120, ma200, rsi, macd) if v is not None
    )
    confidence = (
        ConfidenceLevel.HIGH
        if available_signals >= 5
        else ConfidenceLevel.MEDIUM
        if available_signals >= 3
        else ConfidenceLevel.LOW
    )

    return MomentumSnapshot(
        ma20=ma20,
        ma60=ma60,
        ma120=ma120,
        ma200=ma200,
        ma20_slope_pct=ma20_slope_pct,
        high_20d=high_20d,
        high_60d=high_60d,
        drawdown_from_recent_high_pct=drawdown,
        volume_ratio=volume_ratio,
        rsi=rsi,
        macd=macd,
        relative_strength_vs_topix_pct=relative_strength_topix,
        relative_strength_vs_sector_pct=relative_strength_sector,
        trailing_stop_reference_price=trailing_stop,
        trend_classification=trend,
        confidence=confidence,
    )
