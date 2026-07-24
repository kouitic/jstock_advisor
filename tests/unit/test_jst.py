import datetime as dt

from jstock_advisor.domain.jst import format_jst, to_jst


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
