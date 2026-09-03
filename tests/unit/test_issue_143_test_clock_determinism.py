"""Issue #143: テストが実 wall clock に依存しないことを固定する。

## 背景

`test_holding_decision_regression.py` / `test_holdings_watchlist_handler_integration.py`
は `dt.datetime.now(dt.UTC)` を基準時刻に使っていた。mock の価格 provider は
「now 以前の最新営業日」を `as_of_date` として返すため、市場時間中に実行すると
**当日の未確定 bar** が返る。価格鮮度ゲートはこれを「最新の完了セッション」と
比較するため、**同一 commit でも実行時刻によって結果が変わっていた**。

実測(main CI):

    2026-09-02 16:22 JST(大引け後)  test = success
    2026-09-03 09:05 JST(市場時間中) test = failure   ← 同じコード

本モジュールは次の2点を実行可能な契約として固定する。

1. 影響範囲のテストが wall clock を参照しないこと(再混入の防止)
2. セッション境界ごとの日付 semantics を固定表として明示すること

## 本モジュールが判定しないこと

「市場時間中の当日 bar を業務ロジックがどう扱うべきか」は **Issue #52** の責務であり、
本モジュールでは判定しない。ここで固定するのは

    * mock provider がどの日付を返すか
    * その時刻における「最新の完了セッション」がどの日か

という**観測可能な事実**のみである。両者の関係をどう解釈するか(FUTURE とするか
CURRENT_SESSION_PROVISIONAL とするか)は #52 が決める。
そのため本モジュールは #52 の修正後も書き換え不要である。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pytest

from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.market_session import expected_latest_completed_trading_session
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider

_JST = dt.timezone(dt.timedelta(hours=9))
_MOCK_STOCK_CODE = "2914"

# Issue #143 で wall clock 依存を解消した対象。ここへ再混入させない。
_AFFECTED_TEST_MODULES = (
    "test_holding_decision_regression.py",
    "test_holdings_watchlist_handler_integration.py",
)

_WALL_CLOCK_CALL = re.compile(
    r"(datetime\.now\(|date\.today\(|datetime\.today\(|utcnow\(|time\.time\()"
)


def _calendar() -> BusinessCalendar:
    payload = json.loads(Path("config/holiday_calendar.json").read_text(encoding="utf-8"))
    recurring = [d for d in (payload.get("recurring_market_closures") or []) if isinstance(d, str)]
    additional = [
        d
        for d in (payload.get("additional_closures") or [])
        if isinstance(d, str) and d[:1].isdigit()
    ]
    return BusinessCalendar(
        frozenset(recurring), frozenset(dt.date.fromisoformat(d) for d in additional)
    )


def _at(date: dt.date, hour: int, minute: int) -> dt.datetime:
    return dt.datetime.combine(date, dt.time(hour, minute), tzinfo=_JST)


_WED = dt.date(2026, 9, 2)  # 水曜(営業日)
_SAT = dt.date(2026, 9, 5)  # 土曜
_MON = dt.date(2026, 9, 7)  # 翌週月曜

# (ラベル, 固定now, 期待する最新完了セッション, 期待する mock as_of_date)
_SESSION_BOUNDARY_MATRIX = [
    ("PRE_OPEN", _at(_WED, 8, 0), dt.date(2026, 9, 1), _WED),
    ("MARKET_OPEN_JUST_BEFORE", _at(_WED, 8, 59), dt.date(2026, 9, 1), _WED),
    ("MARKET_OPEN_JUST_AFTER", _at(_WED, 9, 0), dt.date(2026, 9, 1), _WED),
    ("IN_SESSION", _at(_WED, 9, 24), dt.date(2026, 9, 1), _WED),
    ("SESSION_CLOSE_MINUS_1MIN", _at(_WED, 15, 29), dt.date(2026, 9, 1), _WED),
    ("SESSION_CLOSE", _at(_WED, 15, 30), _WED, _WED),
    ("POST_CLOSE", _at(_WED, 16, 0), _WED, _WED),
    ("POST_CLOSE_LATE", _at(_WED, 23, 0), _WED, _WED),
    ("WEEKEND", _at(_SAT, 12, 0), dt.date(2026, 9, 4), dt.date(2026, 9, 4)),
    ("MONDAY_PRE_OPEN", _at(_MON, 8, 0), dt.date(2026, 9, 4), _MON),
]


@pytest.mark.parametrize(
    ("label", "now", "expected_session", "expected_as_of"),
    _SESSION_BOUNDARY_MATRIX,
    ids=[row[0] for row in _SESSION_BOUNDARY_MATRIX],
)
def test_session_boundary_date_semantics_are_fixed(
    label: str, now: dt.datetime, expected_session: dt.date, expected_as_of: dt.date
) -> None:
    """セッション境界ごとの日付 semantics を固定表として明示する。

    **業務上の可否は判定しない**(#52 の責務)。ここで固定するのは
    「その時刻に mock がどの日付を返し、最新の完了セッションがどの日か」だけである。
    """
    calendar = _calendar()
    assert expected_latest_completed_trading_session(now, calendar) == expected_session

    snapshot = MockMarketDataProvider(now).get_latest_price(_MOCK_STOCK_CODE)
    assert snapshot is not None
    assert snapshot.as_of_date == expected_as_of


def test_mock_returns_current_session_bar_while_market_is_open() -> None:
    """市場時間中、mock は「まだ完了していない当日セッション」の bar を返す。

    これが Issue #143 の wall clock 依存を生んだ実体である。
    実 provider も同じ振る舞いをすることが #52 で read-only 実測されている。
    この状態を業務ロジックがどう扱うべきかは #52 が決める。
    """
    now = _at(_WED, 9, 24)
    calendar = _calendar()

    snapshot = MockMarketDataProvider(now).get_latest_price(_MOCK_STOCK_CODE)
    assert snapshot is not None
    latest_completed = expected_latest_completed_trading_session(now, calendar)

    assert snapshot.as_of_date == now.date()
    assert latest_completed < snapshot.as_of_date


def test_mock_as_of_date_never_exceeds_injected_clock() -> None:
    """mock は注入された now を超える日付を返さない(真の未来日は生成しない)。"""
    calendar = _calendar()
    for _label, now, _expected_session, _expected_as_of in _SESSION_BOUNDARY_MATRIX:
        snapshot = MockMarketDataProvider(now).get_latest_price(_MOCK_STOCK_CODE)
        assert snapshot is not None
        assert snapshot.as_of_date <= now.date()
        assert calendar.is_business_day(snapshot.as_of_date)


def test_result_depends_only_on_injected_clock_not_on_wall_clock() -> None:
    """同じ注入 clock なら、実時刻に関係なく常に同じ結果になる。

    provider を2回構築しても、注入した now が同じであれば結果が一致すること。
    実 wall clock を参照していれば、この不変条件は保証されない。
    """
    now = _at(_WED, 16, 0)
    first = MockMarketDataProvider(now).get_latest_price(_MOCK_STOCK_CODE)
    second = MockMarketDataProvider(now).get_latest_price(_MOCK_STOCK_CODE)
    assert first is not None
    assert second is not None
    assert first.as_of_date == second.as_of_date
    assert first.close_price == second.close_price


def test_different_injected_clocks_yield_different_but_deterministic_dates() -> None:
    """異なる注入 clock は異なる、しかし決定的な日付を返す。"""
    pre_open = MockMarketDataProvider(_at(_MON, 8, 0)).get_latest_price(_MOCK_STOCK_CODE)
    post_close = MockMarketDataProvider(_at(_WED, 16, 0)).get_latest_price(_MOCK_STOCK_CODE)
    assert pre_open is not None
    assert post_close is not None
    assert pre_open.as_of_date == _MON
    assert post_close.as_of_date == _WED


# --- 再混入の防止 --------------------------------------------------------------


@pytest.mark.parametrize("module_name", _AFFECTED_TEST_MODULES)
def test_affected_test_modules_do_not_reference_wall_clock(module_name: str) -> None:
    """影響範囲のテストが実 wall clock を参照しないことを固定する。

    `WALL_CLOCK_DEPENDENT_TESTS_FOR_AFFECTED_PATH = 0` を実行可能な契約にしたもの。
    将来 `datetime.now()` 等が再混入すると本テストが落ちる。
    """
    path = Path(__file__).parent / module_name
    assert path.exists(), f"対象モジュールが見つかりません: {module_name}"
    source = path.read_text(encoding="utf-8")

    offending = sorted(set(_WALL_CLOCK_CALL.findall(source)))
    assert offending == [], (
        f"{module_name} が実 wall clock を参照しています: {offending}。"
        "テストの基準時刻は固定値を使ってください(Issue #143)。"
    )
