import datetime as dt

from jstock_advisor.lambda_handlers._scheduling import is_first_saturday_of_month


def test_first_saturday_is_true() -> None:
    # 2026-08-01は土曜日
    assert is_first_saturday_of_month(dt.date(2026, 8, 1)) is True


def test_second_saturday_is_false() -> None:
    assert is_first_saturday_of_month(dt.date(2026, 8, 8)) is False


def test_non_saturday_within_first_week_is_false() -> None:
    assert is_first_saturday_of_month(dt.date(2026, 8, 3)) is False


def test_saturday_on_day_seven_boundary_is_true() -> None:
    # 2020-03-07は土曜日で、day<=7の境界値を明示的に検証する
    assert is_first_saturday_of_month(dt.date(2020, 3, 7)) is True
