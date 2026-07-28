import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import TrendClassification
from jstock_advisor.domain.signals.momentum import (
    classify_trend,
    compute_high_over_window,
    compute_ma_slope_pct,
    compute_macd,
    compute_momentum_snapshot,
    compute_moving_average,
    compute_relative_strength_pct,
    compute_rsi,
    compute_trailing_stop_reference_price,
    compute_volume_ratio,
)
from jstock_advisor.interfaces.types import PriceBar

_CONFIG = load_config().momentum


def _bars(closes: list[float], start: dt.date = dt.date(2026, 1, 1)) -> list[PriceBar]:
    return [
        PriceBar(
            date=start + dt.timedelta(days=i),
            open=Decimal(str(c)),
            high=Decimal(str(c)),
            low=Decimal(str(c)),
            close=Decimal(str(c)),
            volume=1000 + i,
        )
        for i, c in enumerate(closes)
    ]


def test_moving_average_basic() -> None:
    bars = _bars([100, 110, 120, 130, 140])
    ma = compute_moving_average(bars, 5)
    assert ma == Decimal("120")


def test_moving_average_none_when_insufficient_data() -> None:
    bars = _bars([100, 110])
    assert compute_moving_average(bars, 5) is None


def test_ma_slope_positive_for_rising_prices() -> None:
    closes = [100 + i for i in range(60)]
    bars = _bars(closes)
    slope = compute_ma_slope_pct(bars, 20, 20)
    assert slope is not None
    assert slope > 0


def test_high_over_window() -> None:
    bars = _bars([100, 150, 120, 90, 80])
    assert compute_high_over_window(bars, 5) == Decimal("150")


def test_rsi_is_100_when_all_gains() -> None:
    closes = [100 + i * 10 for i in range(15)]
    bars = _bars(closes)
    rsi = compute_rsi(bars, 14)
    assert rsi == 100.0


def test_rsi_is_0_when_all_losses() -> None:
    closes = [1000 - i * 10 for i in range(15)]
    bars = _bars(closes)
    rsi = compute_rsi(bars, 14)
    assert rsi == 0.0


def test_rsi_none_when_insufficient_data() -> None:
    bars = _bars([100, 101, 102])
    assert compute_rsi(bars, 14) is None


def test_macd_none_when_insufficient_data() -> None:
    bars = _bars([100] * 10)
    assert compute_macd(bars, 12, 26, 9) is None


def test_macd_positive_histogram_for_accelerating_uptrend() -> None:
    # 上昇が徐々に加速する系列: MACD線がシグナル線を上回るはず
    closes = [100 + (i**1.05) for i in range(60)]
    bars = _bars(closes)
    macd = compute_macd(bars, 12, 26, 9)
    assert macd is not None
    assert macd.macd_line > macd.signal_line


def test_relative_strength_positive_when_outperforming() -> None:
    stock_bars = _bars([100 + i * 2 for i in range(30)])
    benchmark_bars = _bars([100 + i for i in range(30)])
    rs = compute_relative_strength_pct(stock_bars, benchmark_bars, 20)
    assert rs is not None
    assert rs > 0


def test_trailing_stop_reference_price() -> None:
    bars = _bars([100, 200, 150])
    price = compute_trailing_stop_reference_price(bars, 3, 10.0)
    assert price == Decimal("180")  # 200 * 0.9


def test_volume_ratio_above_one_when_recent_volume_higher() -> None:
    bars = [
        PriceBar(
            date=dt.date(2026, 1, 1) + dt.timedelta(days=i),
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=100 if i < 15 else 1000,
        )
        for i in range(20)
    ]
    ratio = compute_volume_ratio(bars, 5, 20)
    assert ratio is not None
    assert ratio > 1.0


def test_classify_trend_strong_uptrend() -> None:
    trend = classify_trend(
        current_price=Decimal("150"),
        ma20=Decimal("140"),
        ma60=Decimal("120"),
        ma20_slope_pct=5.0,
        rsi=75.0,
        strong_trend_rsi_threshold=60.0,
    )
    assert trend == TrendClassification.STRONG_UPTREND


def test_classify_trend_neutral_when_missing_data() -> None:
    trend = classify_trend(
        current_price=Decimal("150"),
        ma20=None,
        ma60=None,
        ma20_slope_pct=None,
        rsi=None,
        strong_trend_rsi_threshold=60.0,
    )
    assert trend == TrendClassification.NEUTRAL


def test_compute_momentum_snapshot_end_to_end() -> None:
    closes = [100 + i for i in range(250)]
    bars = _bars(closes)
    snapshot = compute_momentum_snapshot(
        bars, Decimal(str(closes[-1])), dt.date(2026, 1, 1) + dt.timedelta(days=249), _CONFIG
    )
    assert snapshot.ma20 is not None
    assert snapshot.ma200 is not None
    assert snapshot.trend_classification in (
        TrendClassification.UPTREND,
        TrendClassification.STRONG_UPTREND,
    )
