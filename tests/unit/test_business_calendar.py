import datetime as dt

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar


def _calendar() -> BusinessCalendar:
    return BusinessCalendar.from_config(load_config().holiday_calendar)


def test_weekday_is_business_day() -> None:
    cal = _calendar()
    assert cal.is_business_day(dt.date(2026, 7, 24))  # 金曜


def test_weekend_is_not_business_day() -> None:
    cal = _calendar()
    assert not cal.is_business_day(dt.date(2026, 7, 25))  # 土曜
    assert not cal.is_business_day(dt.date(2026, 7, 26))  # 日曜


def test_new_year_market_closure_is_not_business_day() -> None:
    cal = _calendar()
    assert not cal.is_business_day(dt.date(2026, 1, 1))
    assert not cal.is_business_day(dt.date(2026, 1, 2))
    assert not cal.is_business_day(dt.date(2026, 1, 3))
    assert not cal.is_business_day(dt.date(2025, 12, 31))


def test_national_holiday_is_not_business_day() -> None:
    cal = _calendar()
    # 海の日(7月第3月曜) 2026年は7/20
    assert not cal.is_business_day(dt.date(2026, 7, 20))


def test_next_business_day_skips_holiday_weekend() -> None:
    cal = _calendar()
    # 2026-07-24(金)の次の営業日は海の日明けの週明け月曜 7/27 のはず(7/25,26は週末)
    assert cal.next_business_day(dt.date(2026, 7, 24)) == dt.date(2026, 7, 27)


def test_add_business_days_matches_manual_walk() -> None:
    cal = _calendar()
    start = dt.date(2026, 7, 24)
    result = cal.add_business_days(start, 5)
    expected = start
    remaining = 5
    while remaining > 0:
        expected += dt.timedelta(days=1)
        if cal.is_business_day(expected):
            remaining -= 1
    assert result == expected


def test_business_days_between_symmetry() -> None:
    cal = _calendar()
    start = dt.date(2026, 1, 5)
    end = dt.date(2026, 1, 20)
    forward = cal.business_days_between(start, end)
    backward = cal.business_days_between(end, start)
    assert forward > 0
    assert forward == -backward
