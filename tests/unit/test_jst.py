import datetime as dt

import pytest

from jstock_advisor.domain.jst import (
    evaluation_date_jst,
    format_jst,
    require_timezone_aware,
    to_jst,
)


def test_to_jst_converts_utc_to_jst_plus_9_hours() -> None:
    utc_value = dt.datetime(2026, 7, 24, 6, 40, tzinfo=dt.UTC)
    jst_value = to_jst(utc_value)
    assert jst_value.hour == 15
    assert jst_value.day == 24
    assert jst_value.utcoffset() == dt.timedelta(hours=9)


def test_to_jst_can_shift_date_across_midnight() -> None:
    utc_value = dt.datetime(2026, 7, 24, 20, 0, tzinfo=dt.UTC)
    jst_value = to_jst(utc_value)
    assert jst_value.day == 25
    assert jst_value.hour == 5


def test_format_jst_includes_jst_suffix() -> None:
    utc_value = dt.datetime(2026, 7, 24, 6, 40, tzinfo=dt.UTC)
    assert format_jst(utc_value) == "2026-07-24 15:40 JST"


# ===== evaluation_date_jst / require_timezone_aware(決算日修正デプロイ前対応) =====


def test_evaluation_date_jst_crosses_day_boundary_from_utc() -> None:
    """JST 2026-08-06 08:00 = UTC 2026-08-05 23:00。素の.date()を使うと前日
    (8/5)になってしまうが、evaluation_date_jst()は正しく8/6を返す。
    """
    now_utc = dt.datetime(2026, 8, 5, 23, 0, tzinfo=dt.UTC)
    assert now_utc.date() == dt.date(2026, 8, 5)  # 素の.date()は誤り(比較用)
    assert evaluation_date_jst(now_utc) == dt.date(2026, 8, 6)


def test_evaluation_date_jst_matches_utc_date_away_from_boundary() -> None:
    now_utc = dt.datetime(2026, 8, 6, 3, 0, tzinfo=dt.UTC)  # JST 12:00、日跨ぎなし
    assert evaluation_date_jst(now_utc) == dt.date(2026, 8, 6)


def test_require_timezone_aware_accepts_aware_datetime() -> None:
    require_timezone_aware(dt.datetime(2026, 8, 6, tzinfo=dt.UTC))  # 例外を送出しない


def test_require_timezone_aware_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        require_timezone_aware(dt.datetime(2026, 8, 6))
