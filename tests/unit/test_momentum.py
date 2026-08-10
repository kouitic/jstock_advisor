import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import TrendClassification
from jstock_advisor.domain.signals.momentum import (
    classify_trend,
    compute_high_over_window,
    compute_ma_slope_pct,
    compute_macd,
    compute_momentum_snapshot,
    compute_moving_average,
    compute_n_day_return_pct,
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


# ===== compute_n_day_return_pct(コードレビュー対応: Timing Score v3、直接テスト) =====


def test_compute_n_day_return_pct_five_day_uses_sixth_from_last_bar() -> None:
    bars = _bars([100, 102, 104, 106, 108, 110])  # 6本
    result = compute_n_day_return_pct(bars, 5)
    assert result == pytest.approx((110 / 100 - 1) * 100)


def test_compute_n_day_return_pct_returns_none_when_insufficient_bars_for_n() -> None:
    bars = _bars([100, 102, 104, 106, 108])  # 5本(n=5にはn+1=6本必要)
    assert compute_n_day_return_pct(bars, 5) is None


def test_compute_n_day_return_pct_one_day_uses_second_from_last_bar() -> None:
    bars = _bars([100, 110])  # 2本
    result = compute_n_day_return_pct(bars, 1)
    assert result == pytest.approx((110 / 100 - 1) * 100)


def test_compute_n_day_return_pct_returns_none_with_single_bar() -> None:
    bars = _bars([100])  # 1本(n=1にはn+1=2本必要)
    assert compute_n_day_return_pct(bars, 1) is None


def test_compute_n_day_return_pct_negative_for_declining_prices() -> None:
    bars = _bars([100, 90])
    result = compute_n_day_return_pct(bars, 1)
    assert result is not None
    assert result < 0


# ===== current_priceとbarsの時点整合性(コードレビュー対応: Timing Score v3) =====


def test_compute_momentum_snapshot_aligned_dates_computes_short_term_returns() -> None:
    closes = [100 + i for i in range(10)]
    bars = _bars(closes)
    as_of_date = bars[-1].date  # bars最新日と一致
    snapshot = compute_momentum_snapshot(bars, Decimal(str(closes[-1])), as_of_date, _CONFIG)
    assert snapshot.price_history_aligned is True
    assert snapshot.price_history_has_future_bars is False
    assert snapshot.one_day_return_pct is not None
    assert snapshot.five_day_return_pct is not None


def test_compute_momentum_snapshot_history_behind_current_price_suppresses_returns() -> None:
    """current_priceのas-of日付がbars最新日より後(historyが古い)の場合、
    補完せずone_day/five_day returnをNoneのままにする。"""
    closes = [100 + i for i in range(10)]
    bars = _bars(closes)
    as_of_date = bars[-1].date + dt.timedelta(days=1)
    snapshot = compute_momentum_snapshot(bars, Decimal(str(closes[-1])), as_of_date, _CONFIG)
    assert snapshot.price_history_aligned is False
    assert snapshot.price_history_has_future_bars is False
    assert snapshot.one_day_return_pct is None
    assert snapshot.five_day_return_pct is None


def test_compute_momentum_snapshot_future_bar_excluded_computes_return_from_effective() -> None:
    """コードレビュー対応(v4): bars最新日がcurrent_priceのas-of日付より未来の
    場合、その未来バーはtechnical計算全体から除外される(旧設計は「returnは
    常にNoneになる」だったが、これは誤り)。未来バーを除外した結果
    effective_bars[-1].date == as_of_dateとなるため、1日/5日returnは
    (未来バー抜きの)effective_barsから正しく算出される。"""
    closes = [100 + i for i in range(10)]  # 10本、最後の1本(index9)が未来バー
    bars = _bars(closes)
    as_of_date = bars[-1].date - dt.timedelta(days=1)  # index8の日付(未来バーの前日)
    snapshot = compute_momentum_snapshot(bars, Decimal(str(closes[-1])), as_of_date, _CONFIG)
    assert snapshot.price_history_has_future_bars is True
    assert snapshot.price_history_aligned is True  # 未来バー除外後は整合する
    effective_bars = bars[:-1]  # index0..8(9本)
    assert snapshot.one_day_return_pct == pytest.approx(
        compute_n_day_return_pct(effective_bars, 1)
    )
    assert snapshot.five_day_return_pct == pytest.approx(
        compute_n_day_return_pct(effective_bars, 5)
    )


def test_compute_momentum_snapshot_future_bar_and_behind_history_suppresses_returns() -> None:
    """未来バーを除外してもなお、残ったeffective_bars最新日がas_of_dateより
    過去(behind)の場合は、returnを算出しない(未来バー除外とbehind判定は
    独立に成立しうる複合ケース)。"""
    bars = [
        PriceBar(
            date=dt.date(2026, 1, 1) + dt.timedelta(days=offset),
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=1000,
        )
        for offset in (0, 1, 2, 4)  # day3が欠落、day4は未来バーに相当
    ]
    as_of_date = dt.date(2026, 1, 1) + dt.timedelta(days=3)  # day3(bars中に無い日)
    snapshot = compute_momentum_snapshot(bars, Decimal("100"), as_of_date, _CONFIG)
    assert snapshot.price_history_has_future_bars is True  # day4が除外される
    assert snapshot.price_history_aligned is False  # 除外後の最新日はday2 < day3
    assert snapshot.one_day_return_pct is None
    assert snapshot.five_day_return_pct is None


def test_compute_momentum_snapshot_empty_bars_is_not_treated_as_misaligned() -> None:
    """barsが空(データ取得不能)の場合は「不整合」ではなく単なるデータ不足
    として扱い、price_history_alignedはTrueのまま(one_day/five_day returnは
    既存どおりNone)。"""
    snapshot = compute_momentum_snapshot([], Decimal("1000"), dt.date(2026, 1, 1), _CONFIG)
    assert snapshot.price_history_aligned is True
    assert snapshot.price_history_has_future_bars is False
    assert snapshot.one_day_return_pct is None
    assert snapshot.five_day_return_pct is None


# ===== 未来バーのtechnical指標からの除外(コードレビュー対応: Timing Score v4) =====


def test_compute_momentum_snapshot_ma20_excludes_future_bar() -> None:
    closes = [100 + i for i in range(20)] + [100000]  # 21本、最後の1本が未来バー
    bars = _bars(closes)
    as_of_date = bars[19].date  # 20本目(未来バーの前日)
    snapshot = compute_momentum_snapshot(bars, Decimal("119"), as_of_date, _CONFIG)
    assert snapshot.price_history_has_future_bars is True
    assert snapshot.ma20 == compute_moving_average(bars[:20], 20)
    assert snapshot.ma20 != compute_moving_average(bars, 20)


def test_compute_momentum_snapshot_rsi_excludes_future_bar() -> None:
    closes = [100 + i * 10 for i in range(15)] + [1]  # 全て上昇の後、未来バーで暴落
    bars = _bars(closes)
    as_of_date = bars[14].date
    snapshot = compute_momentum_snapshot(bars, Decimal("1"), as_of_date, _CONFIG)
    assert snapshot.price_history_has_future_bars is True
    # 未来バー(暴落)を除外すれば「全て上昇」のままなのでRSI=100のはず
    assert snapshot.rsi == 100.0
    assert snapshot.rsi != compute_rsi(bars, 14)


def test_compute_momentum_snapshot_macd_excludes_future_bar() -> None:
    closes = [100 + (i**1.05) for i in range(60)] + [1.0]  # 61本、最後が未来の暴落バー
    bars = _bars(closes)
    as_of_date = bars[59].date
    snapshot = compute_momentum_snapshot(bars, Decimal("1"), as_of_date, _CONFIG)
    assert snapshot.price_history_has_future_bars is True
    assert snapshot.macd == compute_macd(bars[:60], 12, 26, 9)
    assert snapshot.macd != compute_macd(bars, 12, 26, 9)


def test_compute_momentum_snapshot_high_and_drawdown_exclude_future_bar() -> None:
    closes = [100 + i for i in range(20)] + [99999]  # 21本、最後が未来の急騰バー
    bars = _bars(closes)
    as_of_date = bars[19].date
    current_price = Decimal("119")
    snapshot = compute_momentum_snapshot(bars, current_price, as_of_date, _CONFIG)
    assert snapshot.price_history_has_future_bars is True
    assert snapshot.high_20d == Decimal("119")
    assert snapshot.drawdown_from_recent_high_pct == pytest.approx(0.0)


def test_compute_momentum_snapshot_volume_ratio_excludes_future_bar() -> None:
    volumes = [100] * 15 + [1000] * 5 + [10_000_000]  # 最後の1本が未来の異常出来高バー
    bars = [
        PriceBar(
            date=dt.date(2026, 1, 1) + dt.timedelta(days=i),
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=v,
        )
        for i, v in enumerate(volumes)
    ]
    as_of_date = bars[19].date
    snapshot = compute_momentum_snapshot(bars, Decimal("100"), as_of_date, _CONFIG)
    assert snapshot.price_history_has_future_bars is True
    assert snapshot.volume_ratio == compute_volume_ratio(bars[:20], 5, 20)
    assert snapshot.volume_ratio != compute_volume_ratio(bars, 5, 20)


def test_compute_momentum_snapshot_benchmark_relative_strength_excludes_future_bar() -> None:
    stock_bars = _bars([100 + i * 2 for i in range(61)])  # 61本(未来バーなし)
    benchmark_bars = _bars([100 + i for i in range(61)] + [1.0])  # 62本、最後が未来の暴落バー
    as_of_date = stock_bars[-1].date
    snapshot = compute_momentum_snapshot(
        stock_bars,
        Decimal(str(stock_bars[-1].close)),
        as_of_date,
        _CONFIG,
        benchmark_bars=benchmark_bars,
    )
    assert snapshot.relative_strength_vs_topix_pct == pytest.approx(60.0)


def test_compute_momentum_snapshot_sector_relative_strength_excludes_future_bar() -> None:
    stock_bars = _bars([100 + i * 2 for i in range(61)])  # 61本(未来バーなし)
    sector_bars = _bars([100 + i for i in range(61)] + [1.0])  # 62本、最後が未来の暴落バー
    as_of_date = stock_bars[-1].date
    snapshot = compute_momentum_snapshot(
        stock_bars,
        Decimal(str(stock_bars[-1].close)),
        as_of_date,
        _CONFIG,
        sector_bars=sector_bars,
    )
    assert snapshot.relative_strength_vs_sector_pct == pytest.approx(60.0)
