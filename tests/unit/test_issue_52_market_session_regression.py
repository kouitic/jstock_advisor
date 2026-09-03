"""Issue #52: 市場セッション semantics の回帰修正と境界の固定。

## 何が壊れていたか

Phase B2 は `expected_latest_completed_trading_session()`(= **鮮度の基準点**)を
そのまま **妥当性の上限** として使い、それより後の `as_of_date` を未来日とした。
しかし provider は立会中に**当日の未確定bar**を返すため、
**毎営業日 09:00-15:30 の実行が必ず異常判定**になっていた。

Issue #143 で CI が実行時刻により結果を変えたのも同じ原因である。

    2026-09-02 16:22 JST(POST_CLOSE)  test success  <- false green
    2026-09-03 09:05 JST(IN_SESSION)   test failure  <- 同一コード

## 本モジュールが固定する契約

```
TRUE_FUTURE                  fail-close(**弱めない**)
NON_TRADING_DAY              fail-close
CURRENT_SESSION_PROVISIONAL  正常(FUTURE として扱わない)
COMPLETED_SESSION            既存の missed ラダー(不変)
```

基準時刻はすべて固定値である。実 wall clock を参照しないため、
本モジュールの結果は実行時刻に依存しない(Issue #143 の契約)。
"""

from __future__ import annotations

import datetime as dt

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.market_session import (
    MarketSessionState,
    classify_market_session,
)
from jstock_advisor.domain.price_freshness import (
    AsOfDateState,
    PriceFreshnessVerdict,
    classify_as_of_date,
    evaluate_buy_price_freshness,
    evaluate_holdings_price_freshness,
)

_CALENDAR = BusinessCalendar.from_config(load_config().holiday_calendar)
_JST = dt.timezone(dt.timedelta(hours=9))

_WED = dt.date(2026, 9, 2)  # 水曜(営業日)
_TUE = dt.date(2026, 9, 1)  # 火曜(前営業日)
_SAT = dt.date(2026, 9, 5)  # 土曜
_MON = dt.date(2026, 9, 7)  # 翌週月曜

# 年末年始の連続休場。12/31(木)・1/1(金)・1/2(土)・1/3(日) が非営業日。
_LAST_SESSION_OF_YEAR = dt.date(2026, 12, 30)  # 水曜
_NEW_YEAR_HOLIDAY = dt.date(2027, 1, 1)  # 金曜・休場
_AFTER_LONG_WEEKEND = dt.date(2027, 1, 4)  # 月曜・連休明け

# 表を読みやすく保つための別名。意味は変えない。
_PROVISIONAL = AsOfDateState.CURRENT_SESSION_PROVISIONAL
_COMPLETED = AsOfDateState.COMPLETED_SESSION
_FUTURE = AsOfDateState.TRUE_FUTURE
_NON_TRADING = AsOfDateState.NON_TRADING_DAY


def _at(date: dt.date, hour: int, minute: int) -> dt.datetime:
    return dt.datetime.combine(date, dt.time(hour, minute), tzinfo=_JST)


# --- 1. `now` のセッション状態(要求された境界を網羅)-------------------------

# (ラベル, 固定now, 期待する MarketSessionState)
_SESSION_STATE_MATRIX = [
    ("MARKET_OPEN_JUST_BEFORE", _at(_WED, 8, 59), MarketSessionState.PRE_OPEN),
    ("MARKET_OPEN_EXACT", _at(_WED, 9, 0), MarketSessionState.IN_SESSION),
    ("MARKET_OPEN_JUST_AFTER", _at(_WED, 9, 1), MarketSessionState.IN_SESSION),
    ("IN_SESSION", _at(_WED, 12, 0), MarketSessionState.IN_SESSION),
    ("CLOSE_MINUS_1MIN", _at(_WED, 15, 29), MarketSessionState.IN_SESSION),
    ("CLOSE_EXACT", _at(_WED, 15, 30), MarketSessionState.POST_CLOSE),
    ("POST_CLOSE", _at(_WED, 16, 0), MarketSessionState.POST_CLOSE),
    ("WEEKEND", _at(_SAT, 12, 0), MarketSessionState.NON_BUSINESS_DAY),
    ("HOLIDAY", _at(_NEW_YEAR_HOLIDAY, 12, 0), MarketSessionState.NON_BUSINESS_DAY),
    ("AFTER_LONG_WEEKEND", _at(_AFTER_LONG_WEEKEND, 8, 0), MarketSessionState.PRE_OPEN),
]


@pytest.mark.parametrize(
    ("label", "now", "expected"),
    _SESSION_STATE_MATRIX,
    ids=[row[0] for row in _SESSION_STATE_MATRIX],
)
def test_market_session_state_boundaries(
    label: str, now: dt.datetime, expected: MarketSessionState
) -> None:
    """寄付・大引けの境界を含む `now` の状態分類を固定する。

    寄付ちょうど(09:00)は IN_SESSION、大引けちょうど(15:30)は POST_CLOSE。
    """
    assert classify_market_session(now, _CALENDAR) == expected


def test_market_session_requires_timezone_aware_now() -> None:
    """naive datetime は拒否する(Issue #66 のUTC/JST分散を再発させない)。"""
    naive = dt.datetime(2026, 9, 2, 12, 0)  # noqa: DTZ001
    with pytest.raises(ValueError, match=".*"):
        classify_market_session(naive, _CALENDAR)


# --- 2. as_of_date の状態分類 --------------------------------------------------

# (ラベル, as_of_date, 固定now, 期待する AsOfDateState)
_AS_OF_STATE_MATRIX = [
    # 立会前・立会中の当日bar = 未確定だが正常。ここが Issue #52 の回帰点。
    ("PRE_OPEN_TODAY_BAR", _WED, _at(_WED, 8, 0), _PROVISIONAL),
    ("OPEN_EXACT_TODAY_BAR", _WED, _at(_WED, 9, 0), _PROVISIONAL),
    ("IN_SESSION_TODAY_BAR", _WED, _at(_WED, 9, 24), _PROVISIONAL),
    ("CLOSE_MINUS_1MIN_TODAY_BAR", _WED, _at(_WED, 15, 29), _PROVISIONAL),
    # 大引け後の当日bar = 確定済み。
    ("CLOSE_EXACT_TODAY_BAR", _WED, _at(_WED, 15, 30), _COMPLETED),
    ("POST_CLOSE_TODAY_BAR", _WED, _at(_WED, 16, 0), _COMPLETED),
    # 過去のbarは時刻によらず完了済み。
    ("PREV_SESSION_BAR_PRE_OPEN", _TUE, _at(_WED, 8, 0), _COMPLETED),
    ("PREV_SESSION_BAR_IN_SESSION", _TUE, _at(_WED, 12, 0), _COMPLETED),
    # 非営業日に、その非営業日付のbarが来る = 存在しえない。
    ("WEEKEND_SAME_DAY_BAR", _SAT, _at(_SAT, 12, 0), _NON_TRADING),
    ("HOLIDAY_SAME_DAY_BAR", _NEW_YEAR_HOLIDAY, _at(_NEW_YEAR_HOLIDAY, 12, 0), _NON_TRADING),
    # 連休明け朝。最後の完了セッションは 12/30 で、間に4日の休場がある。
    (
        "LONG_WEEKEND_LAST_SESSION",
        _LAST_SESSION_OF_YEAR,
        _at(_AFTER_LONG_WEEKEND, 8, 0),
        _COMPLETED,
    ),
    # 真の未来日。
    ("TRUE_FUTURE_NEXT_DAY", dt.date(2026, 9, 3), _at(_WED, 16, 0), _FUTURE),
    ("TRUE_FUTURE_IN_SESSION", dt.date(2026, 9, 3), _at(_WED, 12, 0), _FUTURE),
    ("TRUE_FUTURE_FAR", dt.date(2026, 12, 1), _at(_WED, 12, 0), _FUTURE),
    ("TRUE_FUTURE_FROM_WEEKEND", _MON, _at(_SAT, 12, 0), _FUTURE),
]


@pytest.mark.parametrize(
    ("label", "as_of", "now", "expected"),
    _AS_OF_STATE_MATRIX,
    ids=[row[0] for row in _AS_OF_STATE_MATRIX],
)
def test_as_of_date_state_boundaries(
    label: str, as_of: dt.date, now: dt.datetime, expected: AsOfDateState
) -> None:
    assert classify_as_of_date(as_of, now, _CALENDAR) == expected


def test_current_session_bar_is_never_classified_as_future() -> None:
    """立会中の当日barを TRUE_FUTURE としない(Issue #52 の中心的な契約)。

    立会の全区間で成り立つこと。1点だけの検証にしない。
    """
    for hour, minute in [(9, 0), (9, 24), (11, 30), (12, 30), (15, 0), (15, 29)]:
        now = _at(_WED, hour, minute)
        state = classify_as_of_date(_WED, now, _CALENDAR)
        assert state is not _FUTURE, f"{hour:02d}:{minute:02d} で未来判定された"
        assert state is _PROVISIONAL


# --- 3. 判定への接続 -----------------------------------------------------------

_PROVISIONAL_CLOCKS = [
    ("PRE_OPEN", _at(_WED, 8, 0)),
    ("IN_SESSION", _at(_WED, 9, 24)),
    ("CLOSE_MINUS_1MIN", _at(_WED, 15, 29)),
]


_PROVISIONAL_IDS = [r[0] for r in _PROVISIONAL_CLOCKS]


@pytest.mark.parametrize(("label", "now"), _PROVISIONAL_CLOCKS, ids=_PROVISIONAL_IDS)
def test_provisional_bar_does_not_block_buy(label: str, now: dt.datetime) -> None:
    """立会中の当日barで BUY 判定を止めない。

    修正前はここが HARD_STOP になり、立会中の実行が全銘柄除外されていた。
    """
    verdict, reason = evaluate_buy_price_freshness(_WED, now, _CALENDAR)
    assert verdict is PriceFreshnessVerdict.NORMAL
    assert reason is None


@pytest.mark.parametrize(("label", "now"), _PROVISIONAL_CLOCKS, ids=_PROVISIONAL_IDS)
def test_provisional_bar_does_not_block_holdings(label: str, now: dt.datetime) -> None:
    """立会中の当日barで保有判定を止めない。

    修正前はここが DATA_INSUFFICIENT になり、立会中は損切り・利確が全て不能だった。
    """
    verdict, reason = evaluate_holdings_price_freshness(_WED, now, _CALENDAR)
    assert verdict is PriceFreshnessVerdict.NORMAL
    assert reason is None


# --- 4. fail-close を弱めていないこと ------------------------------------------


@pytest.mark.parametrize("days", [1, 2, 30])
def test_true_future_still_hard_stops_buy(days: int) -> None:
    """真の未来日は従来どおり BUY を停止する(fail-close を弱めない)。"""
    as_of = _WED + dt.timedelta(days=days)
    verdict, reason = evaluate_buy_price_freshness(as_of, _at(_WED, 16, 0), _CALENDAR)
    assert verdict is PriceFreshnessVerdict.HARD_STOP
    assert reason is not None
    assert as_of.isoformat() in reason


@pytest.mark.parametrize("days", [1, 2, 30])
def test_true_future_still_blocks_holdings(days: int) -> None:
    """真の未来日は従来どおり保有判定を不能にする(fail-close を弱めない)。"""
    as_of = _WED + dt.timedelta(days=days)
    verdict, reason = evaluate_holdings_price_freshness(as_of, _at(_WED, 16, 0), _CALENDAR)
    assert verdict is PriceFreshnessVerdict.DATA_INSUFFICIENT
    assert reason is not None
    assert as_of.isoformat() in reason


def test_true_future_hard_stops_even_while_market_is_open() -> None:
    """立会中でも真の未来日は停止する。

    回帰修正が「立会中は何でも通す」になっていないことを固定する。
    """
    tomorrow = _WED + dt.timedelta(days=1)
    buy, _ = evaluate_buy_price_freshness(tomorrow, _at(_WED, 12, 0), _CALENDAR)
    holdings, _ = evaluate_holdings_price_freshness(tomorrow, _at(_WED, 12, 0), _CALENDAR)
    assert buy is PriceFreshnessVerdict.HARD_STOP
    assert holdings is PriceFreshnessVerdict.DATA_INSUFFICIENT


def test_non_trading_day_bar_fails_closed() -> None:
    """非営業日付のbarは両経路で停止する。"""
    buy, buy_reason = evaluate_buy_price_freshness(_SAT, _at(_SAT, 12, 0), _CALENDAR)
    holdings, h_reason = evaluate_holdings_price_freshness(_SAT, _at(_SAT, 12, 0), _CALENDAR)
    assert buy is PriceFreshnessVerdict.HARD_STOP
    assert holdings is PriceFreshnessVerdict.DATA_INSUFFICIENT
    assert buy_reason is not None
    assert _SAT.isoformat() in buy_reason
    assert h_reason is not None
    assert _SAT.isoformat() in h_reason


def test_unknown_as_of_still_fails_closed() -> None:
    """as_of 不明は従来どおり停止する。"""
    buy, _ = evaluate_buy_price_freshness(None, _at(_WED, 12, 0), _CALENDAR)
    holdings, _ = evaluate_holdings_price_freshness(None, _at(_WED, 12, 0), _CALENDAR)
    assert buy is PriceFreshnessVerdict.HARD_STOP
    assert holdings is PriceFreshnessVerdict.DATA_INSUFFICIENT


# --- 5. 既存の missed ラダーが不変であること -----------------------------------


def test_stale_ladder_is_unchanged_for_completed_sessions() -> None:
    """完了済みセッションの missed ラダーは回帰修正の影響を受けない。

    大引け後(POST_CLOSE)の 2026-09-02 16:00 を基準とする。
    期待される直近完了セッションは 09-02(水)。
    """
    now = _at(_WED, 16, 0)
    # missed=0 -> NORMAL
    assert evaluate_buy_price_freshness(_WED, now, _CALENDAR)[0] is PriceFreshnessVerdict.NORMAL
    # missed=1 -> BUY は WARNING、holdings は DATA_INSUFFICIENT(非対称)
    assert evaluate_buy_price_freshness(_TUE, now, _CALENDAR)[0] is PriceFreshnessVerdict.WARNING
    assert (
        evaluate_holdings_price_freshness(_TUE, now, _CALENDAR)[0]
        is PriceFreshnessVerdict.DATA_INSUFFICIENT
    )
    # missed>=2 -> BUY も HARD_STOP
    assert (
        evaluate_buy_price_freshness(dt.date(2026, 8, 31), now, _CALENDAR)[0]
        is PriceFreshnessVerdict.HARD_STOP
    )
