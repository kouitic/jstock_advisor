"""東証の営業日判定・営業日ベースの日付計算。

国民の祝日は jpholiday(内閣府の祝日法規則に基づくアルゴリズム実装)で判定し、
年末年始等の東証固有の休業日は holiday_calendar.json から補う。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import jpholiday

from jstock_advisor.config.models import HolidayCalendarConfig

_WEEKEND = {5, 6}  # 土曜=5, 日曜=6


@dataclass(frozen=True)
class BusinessCalendar:
    extra_closure_mm_dd: frozenset[str]
    additional_closure_dates: frozenset[dt.date]

    @classmethod
    def from_config(cls, config: HolidayCalendarConfig) -> BusinessCalendar:
        extra_mm_dd = frozenset(config.recurring_market_closures.dates_mm_dd)
        additional = frozenset(dt.date.fromisoformat(d) for d in config.additional_closures.dates)
        return cls(extra_closure_mm_dd=extra_mm_dd, additional_closure_dates=additional)

    def is_business_day(self, date: dt.date) -> bool:
        if date.weekday() in _WEEKEND:
            return False
        if jpholiday.is_holiday(date):
            return False
        if date.strftime("%m-%d") in self.extra_closure_mm_dd:
            return False
        return date not in self.additional_closure_dates

    def next_business_day(self, date: dt.date) -> dt.date:
        current = date + dt.timedelta(days=1)
        while not self.is_business_day(current):
            current += dt.timedelta(days=1)
        return current

    def add_business_days(self, start: dt.date, count: int) -> dt.date:
        """startからcount営業日後の日付を返す(count>=1)。startが休業日でも起点として扱う。"""
        if count < 1:
            raise ValueError("count must be >= 1")
        current = start
        remaining = count
        while remaining > 0:
            current = current + dt.timedelta(days=1)
            if self.is_business_day(current):
                remaining -= 1
        return current

    def business_days_between(self, start: dt.date, end: dt.date) -> int:
        """startの翌日からendまでの営業日数を数える(startとendの前後関係は問わない)。"""
        if start == end:
            return 0
        step = 1 if end > start else -1
        current = start
        count = 0
        while current != end:
            current += dt.timedelta(days=step)
            if self.is_business_day(current):
                count += 1
        return count * step
