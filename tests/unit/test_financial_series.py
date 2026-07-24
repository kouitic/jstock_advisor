import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.financial_series import (
    is_quarterly_cadence,
    to_seasonally_adjusted_series,
)


def _dates(start: dt.date, count: int, step_days: int) -> list[dt.date]:
    return [start + dt.timedelta(days=step_days * i) for i in range(count)]


def test_is_quarterly_cadence_true_for_90_day_gaps() -> None:
    dates = _dates(dt.date(2025, 1, 1), 5, 91)
    assert is_quarterly_cadence(dates) is True


def test_is_quarterly_cadence_false_for_annual_gaps() -> None:
    dates = _dates(dt.date(2020, 3, 31), 5, 365)
    assert is_quarterly_cadence(dates) is False


def test_is_quarterly_cadence_false_for_single_point() -> None:
    assert is_quarterly_cadence([dt.date(2025, 1, 1)]) is False


def test_annual_series_passes_through_unchanged() -> None:
    dates = _dates(dt.date(2020, 3, 31), 5, 365)
    values = [Decimal(str(v)) for v in [100, 110, 120, 90, 95]]
    result = to_seasonally_adjusted_series(values, dates)
    assert result == values


def test_quarterly_series_converts_to_ttm() -> None:
    dates = _dates(dt.date(2025, 1, 1), 8, 91)
    # 各四半期100固定 -> TTM(4期合計)は常に400になるはず
    values = [Decimal("100")] * 8
    result = to_seasonally_adjusted_series(values, dates)
    assert len(result) == 5  # 8 - 4 + 1
    assert all(v == Decimal("400") for v in result)


def test_ttm_smooths_seasonal_single_quarter_dip() -> None:
    # Q4だけ大きく落ち込むが、通期では緩やかに成長しているケース(季節性の典型例)
    dates = _dates(dt.date(2024, 1, 1), 8, 91)
    pattern = [100, 120, 130, 60, 105, 126, 137, 63]  # 4期周期でQ4だけ落ち込む
    values = [Decimal(str(v)) for v in pattern]
    result = to_seasonally_adjusted_series(values, dates)
    assert len(result) == 5
    # TTMは各年ほぼ同水準〜微増になり、単純な最終期の急落は見えなくなるはず
    assert result[-1] is not None and result[0] is not None
    assert result[-1] >= result[0]


def test_quarterly_series_with_insufficient_history_returns_empty() -> None:
    dates = _dates(dt.date(2025, 1, 1), 3, 91)
    values = [Decimal("100")] * 3
    assert to_seasonally_adjusted_series(values, dates) == []


def test_ttm_propagates_none_for_incomplete_window() -> None:
    dates = _dates(dt.date(2025, 1, 1), 8, 91)
    values = [
        Decimal("100"),
        Decimal("100"),
        None,
        Decimal("100"),
        Decimal("100"),
        Decimal("100"),
        Decimal("100"),
        Decimal("100"),
    ]
    result = to_seasonally_adjusted_series(values, dates)
    assert len(result) == 5
    # index2のNoneを含むウィンドウ(先頭3つ)はNone、含まなくなった以降は計算できる
    assert result[0] is None
    assert result[1] is None
    assert result[2] is None
    assert result[3] == Decimal("400")
    assert result[4] == Decimal("400")
