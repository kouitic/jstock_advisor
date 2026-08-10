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


def compute_n_day_return_pct(bars: list[PriceBar], n: int) -> float | None:
    """直近n営業日リターン(%)。

    1営業日リターン(n=1) = 直近のhistorical close / 1本前のhistorical close - 1
    5営業日リターン(n=5) = 直近のhistorical close / 5本前のhistorical close - 1

    bars[-1]を最新のhistorical close、bars[-(n+1)]をn営業日前のhistorical close
    とする(barsが日付昇順であることが前提。compute_moving_average等、本モジュール
    の既存関数群と同じ前提を踏襲する)。算出にはn+1本が必要で、満たない場合はNone。

    コードレビュー対応(v3): この関数自体はcurrent_price(ライブの最新値)との
    時点整合性を一切保証しない(barsのみから算出する純粋な過去バー間の変化率)。
    current_priceとbars[-1]が同一時点のデータかどうかの検証は、呼び出し側
    (compute_momentum_snapshot())の責務とする。

    Timing Score(判定精度向上機能Phase B第二弾)専用。既存barsのみから算出し、
    新規Provider呼び出しは行わない。
    """
    if len(bars) < n + 1:
        return None
    return float(bars[-1].close / bars[-(n + 1)].close - 1) * 100


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
    """MomentumSnapshotを算出する(コードレビュー対応v4でlook-ahead bias対策を
    強化)。

    入力前提:
    - bars/benchmark_bars/sector_barsはいずれも日付昇順であることが前提
      (compute_moving_average等、本モジュールの既存関数群と同じ前提)。
    - as_of_dateはcurrent_priceの実際のas-of日付(例: PriceSnapshot.as_of_date)。
      current_priceとbars(いずれも別Provider呼び出し由来であり、時点が一致する
      保証はコード上に無い。コードレビュー対応v3)。

    未来バーの扱い(コードレビュー対応v4): as_of_dateより未来の日付を持つ
    PriceBarがbarsへ混入していた場合、MA20/60/120/200・MA20 slope・high・
    drawdown・volume_ratio・RSI・MACD・trailing_stop・relative strength(TOPIX/
    セクター)を含む**全てのtechnical計算**から内部的に除外する
    (`effective_bars`/`effective_benchmark_bars`/`effective_sector_bars`のみを
    使う)。以前のバージョンはone_day_return_pct/five_day_return_pctのみを
    無効化し、他のtechnical指標は未来バーを含んだまま計算していたため
    look-ahead biasとなっていた。current_price自体はas-of日付の実測値であり、
    未来バーの有無によって補正・書き換えは行わない。

    未来バー除外後の整合性判定: effective_bars[-1].dateがas_of_dateと一致
    しない(=historyがcurrent_priceより古い、behind)場合、one_day_return_pct/
    five_day_return_pctは算出せずNoneのまま(推測で同一時点とみなさない)。
    未来バーを除外した結果effective_bars[-1].date == as_of_dateとなった場合は
    (未来バーが存在したこと自体を理由に)無条件でNoneにはしない。
    """
    effective_bars = [b for b in bars if b.date <= as_of_date]
    price_history_has_future_bars = len(effective_bars) < len(bars)
    effective_benchmark_bars = (
        [b for b in benchmark_bars if b.date <= as_of_date] if benchmark_bars else None
    )
    effective_sector_bars = (
        [b for b in sector_bars if b.date <= as_of_date] if sector_bars else None
    )

    ma20 = compute_moving_average(effective_bars, 20)
    ma60 = compute_moving_average(effective_bars, 60)
    ma120 = compute_moving_average(effective_bars, 120)
    ma200 = compute_moving_average(effective_bars, 200)
    ma20_slope_pct = compute_ma_slope_pct(
        effective_bars, 20, config.moving_averages.slope_lookback_days
    )
    high_20d = compute_high_over_window(effective_bars, config.high_low.high_window_days_short)
    high_60d = compute_high_over_window(effective_bars, config.high_low.high_window_days_long)
    drawdown = compute_drawdown_from_recent_high_pct(
        effective_bars, current_price, config.high_low.drawdown_window_days
    )
    volume_ratio = compute_volume_ratio(
        effective_bars, config.volume.short_window_days, config.volume.long_window_days
    )
    rsi = compute_rsi(effective_bars, config.rsi.period)
    macd = compute_macd(
        effective_bars,
        config.macd.fast_period,
        config.macd.slow_period,
        config.macd.signal_period,
    )
    relative_strength_topix = (
        compute_relative_strength_pct(
            effective_bars, effective_benchmark_bars, config.high_low.high_window_days_long
        )
        if effective_benchmark_bars
        else None
    )
    relative_strength_sector = (
        compute_relative_strength_pct(
            effective_bars, effective_sector_bars, config.high_low.high_window_days_long
        )
        if effective_sector_bars
        else None
    )
    trailing_stop = compute_trailing_stop_reference_price(
        effective_bars, config.high_low.high_window_days_long, config.trailing_stop.trailing_pct
    )
    trend = classify_trend(
        current_price,
        ma20,
        ma60,
        ma20_slope_pct,
        rsi,
        config.trend_classification.strong_trend_rsi_threshold,
    )
    trend_evaluable = ma20 is not None and ma60 is not None and ma20_slope_pct is not None
    # コードレビュー対応(v4): 未来バー除外後のeffective_bars基準で判定する。
    # effective_barsが空の場合は単なるデータ不足であり「historyが古い」わけ
    # ではないため、price_history_aligned=Trueのまま(既存方針を踏襲)。
    price_history_behind = bool(effective_bars) and effective_bars[-1].date < as_of_date
    price_history_aligned = not price_history_behind
    one_day_return_pct = (
        None if price_history_behind else compute_n_day_return_pct(effective_bars, 1)
    )
    five_day_return_pct = (
        None if price_history_behind else compute_n_day_return_pct(effective_bars, 5)
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
        trend_evaluable=trend_evaluable,
        one_day_return_pct=one_day_return_pct,
        five_day_return_pct=five_day_return_pct,
        price_history_aligned=price_history_aligned,
        price_history_has_future_bars=price_history_has_future_bars,
        confidence=confidence,
    )
