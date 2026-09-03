"""Issue #52 Phase B2 regression — 「未確定」と「未来」を区別する(2026-09-03)。

## 何が壊れていたか

Phase B2 の未来日判定が `expected_latest_completed_trading_session()`
(既に大引けを迎えた直近営業日)を基準にしていたため、**市場時間中の当日bar**を
「未来のbar」と誤判定し、BUY / holdings 判定を全銘柄で停止させていた。

本番と同一の provider(yfinance)は市場時間中に当日の未確定な日足barを返す。
2026-09-03 09:24 JST の実測で `as_of_date = 2026-09-03` /
`expected_latest_completed_trading_session = 2026-09-02` となり、
`as_of > expected` -> HARD_STOP / DATA_INSUFFICIENT が成立していた。

さらに mock provider は `as_of_date` に **UTC 暦日**を使っていたため、
同一コードでも **実行時刻(UTC 日跨ぎ)によって CI が green / red に変わる**
状態だった。

## 本ファイルが固定する契約

```
PRE_OPEN          当日barは存在しない        -> 未来判定の基準 = 前営業日
IN_SESSION        当日barは未確定だが実在     -> 基準 = 当日(未来ではない)
POST_CLOSE        当日barは確定             -> 基準 = 当日
NON_BUSINESS_DAY                          -> 基準 = 直前営業日
真の未来(翌営業日以降)                      -> fail-close を維持
```

**すべての `now` を固定値で注入する。** 実行時の wall clock に依存しない。
`dt.datetime.now()` / `dt.date.today()` を本ファイルで使わない。

既存の freshness 閾値(BUY: 0 normal / 1 warning / >=2 hard stop、
holdings: 0 normal / >=1 data insufficient)は**変更していない**ことも固定する。
"""

from __future__ import annotations

import datetime as dt

import pytest

from jstock_advisor.domain.market_session import (
    JPX_REGULAR_SESSION_CLOSE_JST,
    JPX_REGULAR_SESSION_OPEN_JST,
    MarketSessionPhase,
    current_market_session_phase,
    expected_latest_completed_trading_session,
    latest_plausible_bar_date,
)
from jstock_advisor.domain.price_freshness import (
    PriceFreshnessVerdict,
    evaluate_buy_price_freshness,
    evaluate_holdings_price_freshness,
)
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.mock_fixtures import business_calendar

_JST = dt.timezone(dt.timedelta(hours=9))

# 2026-09 のJPXカレンダー(実カレンダーで確認済み)
#   09-02 水 / 09-03 木 / 09-04 金 = 営業日
#   09-05 土 / 09-06 日            = 休場
#   09-19 土 〜 09-23 水            = 5連休(敬老の日・秋分の日を含む)
#   09-24 木                       = 連休明け最初の営業日
_BUSINESS_DAY = dt.date(2026, 9, 3)
_PREV_BUSINESS_DAY = dt.date(2026, 9, 2)
_NEXT_BUSINESS_DAY = dt.date(2026, 9, 4)
_SATURDAY = dt.date(2026, 9, 5)
_LONG_WEEKEND_LAST_BUSINESS_DAY = dt.date(2026, 9, 18)
_LONG_WEEKEND_REOPEN = dt.date(2026, 9, 24)
_MOCK_STOCK = "2914"


@pytest.fixture
def calendar():
    return business_calendar()


def _jst(day: dt.date, hour: int, minute: int = 0, second: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=_JST)


# --- session phase の境界 ------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "minute", "expected_phase"),
    [
        (8, 0, MarketSessionPhase.PRE_OPEN),
        (8, 59, MarketSessionPhase.PRE_OPEN),
        (9, 0, MarketSessionPhase.IN_SESSION),
        (9, 24, MarketSessionPhase.IN_SESSION),
        (15, 29, MarketSessionPhase.IN_SESSION),
        (15, 30, MarketSessionPhase.POST_CLOSE),
        (16, 0, MarketSessionPhase.POST_CLOSE),
        (23, 59, MarketSessionPhase.POST_CLOSE),
        (0, 0, MarketSessionPhase.PRE_OPEN),
    ],
)
def test_session_phase_boundaries(calendar, hour, minute, expected_phase) -> None:
    """寄付(09:00)・大引け(15:30)の境界を固定時刻で固定する。"""
    now = _jst(_BUSINESS_DAY, hour, minute)
    assert current_market_session_phase(now, calendar) is expected_phase


def test_session_phase_on_non_business_day(calendar) -> None:
    assert (
        current_market_session_phase(_jst(_SATURDAY, 12), calendar)
        is MarketSessionPhase.NON_BUSINESS_DAY
    )


# --- latest_plausible_bar_date と expected_latest_completed_trading_session の差 ---


@pytest.mark.parametrize(
    ("hour", "minute", "plausible", "completed"),
    [
        # 寄付前は当日barが存在しない。両者とも前営業日
        (8, 0, _PREV_BUSINESS_DAY, _PREV_BUSINESS_DAY),
        (8, 59, _PREV_BUSINESS_DAY, _PREV_BUSINESS_DAY),
        # 寄付後〜大引け前は「存在しうる = 当日」「完了済み = 前営業日」で**分岐する**
        (9, 0, _BUSINESS_DAY, _PREV_BUSINESS_DAY),
        (9, 24, _BUSINESS_DAY, _PREV_BUSINESS_DAY),
        (15, 29, _BUSINESS_DAY, _PREV_BUSINESS_DAY),
        # 大引け後は両者とも当日
        (15, 30, _BUSINESS_DAY, _BUSINESS_DAY),
        (16, 0, _BUSINESS_DAY, _BUSINESS_DAY),
    ],
)
def test_plausible_vs_completed_diverge_only_in_session(
    calendar, hour, minute, plausible, completed
) -> None:
    """未来判定の基準(plausible)と鮮度の基準(completed)は市場時間中だけ食い違う。

    この食い違いこそが本 regression の本質であり、
    **未来判定に completed を使ってはならない**理由である。
    """
    now = _jst(_BUSINESS_DAY, hour, minute)
    assert latest_plausible_bar_date(now, calendar) == plausible
    assert expected_latest_completed_trading_session(now, calendar) == completed


def test_plausible_bar_date_on_non_business_day(calendar) -> None:
    assert latest_plausible_bar_date(_jst(_SATURDAY, 12), calendar) == _NEXT_BUSINESS_DAY


def test_plausible_bar_date_after_long_weekend(calendar) -> None:
    """5連休明けの寄付前は、連休前最後の営業日が最新の存在しうるbar。"""
    now = _jst(_LONG_WEEKEND_REOPEN, 8)
    assert latest_plausible_bar_date(now, calendar) == _LONG_WEEKEND_LAST_BUSINESS_DAY


def test_plausible_bar_date_after_long_weekend_in_session(calendar) -> None:
    """連休明けでも寄付後は当日barが存在しうる。"""
    now = _jst(_LONG_WEEKEND_REOPEN, 9, 24)
    assert latest_plausible_bar_date(now, calendar) == _LONG_WEEKEND_REOPEN


# --- 本 regression の中核: 市場時間中の当日barを FUTURE にしない -------------------


@pytest.mark.parametrize(("hour", "minute"), [(9, 0), (9, 24), (12, 0), (15, 29)])
def test_in_session_same_day_bar_is_not_future_for_buy(calendar, hour, minute) -> None:
    """IN_SESSION の当日bar(未確定)を BUY 側で FUTURE 扱いしない。"""
    verdict, reason = evaluate_buy_price_freshness(
        _BUSINESS_DAY, _jst(_BUSINESS_DAY, hour, minute), calendar
    )
    assert verdict is PriceFreshnessVerdict.NORMAL
    assert reason is None


@pytest.mark.parametrize(("hour", "minute"), [(9, 0), (9, 24), (12, 0), (15, 29)])
def test_in_session_same_day_bar_is_not_future_for_holdings(calendar, hour, minute) -> None:
    """IN_SESSION の当日bar(未確定)を holdings/SELL 側で FUTURE 扱いしない。"""
    verdict, reason = evaluate_holdings_price_freshness(
        _BUSINESS_DAY, _jst(_BUSINESS_DAY, hour, minute), calendar
    )
    assert verdict is PriceFreshnessVerdict.NORMAL
    assert reason is None


@pytest.mark.parametrize(("hour", "minute"), [(15, 30), (16, 0), (23, 59)])
def test_post_close_same_day_bar_is_normal(calendar, hour, minute) -> None:
    now = _jst(_BUSINESS_DAY, hour, minute)
    assert evaluate_buy_price_freshness(_BUSINESS_DAY, now, calendar)[0] is (
        PriceFreshnessVerdict.NORMAL
    )
    assert evaluate_holdings_price_freshness(_BUSINESS_DAY, now, calendar)[0] is (
        PriceFreshnessVerdict.NORMAL
    )


@pytest.mark.parametrize(("hour", "minute"), [(8, 0), (8, 59)])
def test_pre_open_previous_business_day_bar_is_normal(calendar, hour, minute) -> None:
    """営業日 08:00(定時バッチの実行時刻)で前営業日barが正常であること。"""
    now = _jst(_BUSINESS_DAY, hour, minute)
    assert evaluate_buy_price_freshness(_PREV_BUSINESS_DAY, now, calendar)[0] is (
        PriceFreshnessVerdict.NORMAL
    )
    assert evaluate_holdings_price_freshness(_PREV_BUSINESS_DAY, now, calendar)[0] is (
        PriceFreshnessVerdict.NORMAL
    )


def test_pre_open_same_day_bar_is_future_fail_closed(calendar) -> None:
    """寄付前の当日barは存在し得ない。fail-close を維持する。"""
    now = _jst(_BUSINESS_DAY, 8)
    buy_verdict, buy_reason = evaluate_buy_price_freshness(_BUSINESS_DAY, now, calendar)
    assert buy_verdict is PriceFreshnessVerdict.HARD_STOP
    assert buy_reason is not None and "未来" in buy_reason

    hold_verdict, hold_reason = evaluate_holdings_price_freshness(_BUSINESS_DAY, now, calendar)
    assert hold_verdict is PriceFreshnessVerdict.DATA_INSUFFICIENT
    assert hold_reason is not None and "未来" in hold_reason


# --- 真の未来は fail-close を維持 ----------------------------------------------


@pytest.mark.parametrize(("hour", "minute"), [(8, 0), (9, 24), (15, 30), (16, 0)])
def test_next_business_day_bar_is_always_future(calendar, hour, minute) -> None:
    """翌営業日以降のbarは、どの局面でも存在し得ない = 真の未来。"""
    now = _jst(_BUSINESS_DAY, hour, minute)
    assert evaluate_buy_price_freshness(_NEXT_BUSINESS_DAY, now, calendar)[0] is (
        PriceFreshnessVerdict.HARD_STOP
    )
    assert evaluate_holdings_price_freshness(_NEXT_BUSINESS_DAY, now, calendar)[0] is (
        PriceFreshnessVerdict.DATA_INSUFFICIENT
    )


def test_far_future_bar_is_future_on_non_business_day(calendar) -> None:
    now = _jst(_SATURDAY, 12)
    assert evaluate_buy_price_freshness(dt.date(2026, 10, 1), now, calendar)[0] is (
        PriceFreshnessVerdict.HARD_STOP
    )


# --- 既存 freshness 閾値を変更していないこと -------------------------------------


def test_buy_freshness_thresholds_unchanged(calendar) -> None:
    """BUY: missed 0=NORMAL / 1=WARNING / >=2=HARD_STOP / UNKNOWN=HARD_STOP。"""
    now = _jst(_BUSINESS_DAY, 16)  # POST_CLOSE。expected = 当日
    assert evaluate_buy_price_freshness(_BUSINESS_DAY, now, calendar)[0] is (
        PriceFreshnessVerdict.NORMAL
    )
    assert evaluate_buy_price_freshness(_PREV_BUSINESS_DAY, now, calendar)[0] is (
        PriceFreshnessVerdict.WARNING
    )
    assert evaluate_buy_price_freshness(dt.date(2026, 9, 1), now, calendar)[0] is (
        PriceFreshnessVerdict.HARD_STOP
    )
    assert evaluate_buy_price_freshness(None, now, calendar)[0] is (PriceFreshnessVerdict.HARD_STOP)


def test_holdings_freshness_thresholds_unchanged(calendar) -> None:
    """holdings/SELL: missed 0=NORMAL / >=1=DATA_INSUFFICIENT / UNKNOWN=同左。"""
    now = _jst(_BUSINESS_DAY, 16)
    assert evaluate_holdings_price_freshness(_BUSINESS_DAY, now, calendar)[0] is (
        PriceFreshnessVerdict.NORMAL
    )
    assert evaluate_holdings_price_freshness(_PREV_BUSINESS_DAY, now, calendar)[0] is (
        PriceFreshnessVerdict.DATA_INSUFFICIENT
    )
    assert evaluate_holdings_price_freshness(None, now, calendar)[0] is (
        PriceFreshnessVerdict.DATA_INSUFFICIENT
    )


def test_in_session_previous_day_bar_follows_freshness_policy(calendar) -> None:
    """IN_SESSION の前営業日barは「未来ではない」が、鮮度policyには従う。

    IN_SESSION では expected = 前営業日なので missed = 0 -> NORMAL。
    未来判定の基準(当日)と鮮度の基準(前営業日)が別であることの確認。
    """
    now = _jst(_BUSINESS_DAY, 9, 24)
    assert evaluate_buy_price_freshness(_PREV_BUSINESS_DAY, now, calendar)[0] is (
        PriceFreshnessVerdict.NORMAL
    )
    assert evaluate_holdings_price_freshness(_PREV_BUSINESS_DAY, now, calendar)[0] is (
        PriceFreshnessVerdict.NORMAL
    )


# --- wall clock 非依存(UTC / JST の日跨ぎ) ------------------------------------


@pytest.mark.parametrize(
    ("utc_hour", "utc_minute"),
    [
        (22, 0),  # JST 翌07:00 PRE_OPEN
        (23, 0),  # JST 翌08:00 PRE_OPEN(定時バッチ)
        (23, 59),  # UTC 日跨ぎ直前
        (0, 0),  # UTC 日跨ぎ直後 = JST 09:00 IN_SESSION
        (0, 24),  # JST 09:24(本 regression が顕在化した時刻)
        (6, 29),  # JST 15:29 IN_SESSION
        (6, 30),  # JST 15:30 POST_CLOSE
        (7, 0),  # JST 16:00 POST_CLOSE
    ],
)
def test_same_instant_gives_same_verdict_regardless_of_tzinfo(
    calendar, utc_hour, utc_minute
) -> None:
    """同一instantを UTC 表現でも JST 表現でも与えて、結果が一致すること。

    UTC 日跨ぎの前後を含む。tzinfo の与え方や UTC 暦日で結果が変わらないこと
    (= CI が実行時刻の表現に依存しないこと)を固定する。
    """
    base_utc = dt.datetime(2026, 9, 2, utc_hour, utc_minute, tzinfo=dt.UTC)
    as_of = latest_plausible_bar_date(base_utc, calendar)

    for now in (base_utc, base_utc.astimezone(_JST)):
        assert latest_plausible_bar_date(now, calendar) == as_of
        assert evaluate_buy_price_freshness(as_of, now, calendar)[0] is PriceFreshnessVerdict.NORMAL
        assert (
            evaluate_holdings_price_freshness(as_of, now, calendar)[0]
            is PriceFreshnessVerdict.NORMAL
        )


def test_jst_day_boundary_is_stable(calendar) -> None:
    """JST 日跨ぎ(23:59 -> 翌00:00)で局面と基準日が壊れないこと。"""
    late = _jst(_BUSINESS_DAY, 23, 59)
    early_next = _jst(_NEXT_BUSINESS_DAY, 0, 0)

    assert current_market_session_phase(late, calendar) is MarketSessionPhase.POST_CLOSE
    assert latest_plausible_bar_date(late, calendar) == _BUSINESS_DAY

    assert current_market_session_phase(early_next, calendar) is MarketSessionPhase.PRE_OPEN
    assert latest_plausible_bar_date(early_next, calendar) == _BUSINESS_DAY


# --- provider contract: mock が返す as_of は常に「存在しうる」日である ---------------


@pytest.mark.parametrize(
    ("day", "hour", "minute"),
    [
        (_BUSINESS_DAY, 8, 0),
        (_BUSINESS_DAY, 8, 59),
        (_BUSINESS_DAY, 9, 0),
        (_BUSINESS_DAY, 9, 24),
        (_BUSINESS_DAY, 15, 29),
        (_BUSINESS_DAY, 15, 30),
        (_BUSINESS_DAY, 16, 0),
        (_SATURDAY, 12, 0),
        (_LONG_WEEKEND_REOPEN, 8, 0),
        (_LONG_WEEKEND_REOPEN, 9, 24),
    ],
)
def test_mock_provider_as_of_never_exceeds_plausible_bar_date(day, hour, minute) -> None:
    """mock provider の `as_of_date` が未来にならないこと(契約テスト)。

    以前は UTC 暦日で打ち切っていたため、UTC 日跨ぎ後・JST 寄付前の時間帯に
    「まだ存在しない当日bar」を返し、判定側と衝突して CI が実行時刻依存になっていた。
    """
    calendar = business_calendar()
    now = _jst(day, hour, minute)
    snapshot = MockMarketDataProvider(now=now).get_latest_price(_MOCK_STOCK)
    assert snapshot is not None
    assert snapshot.as_of_date <= latest_plausible_bar_date(now, calendar)


@pytest.mark.parametrize(
    ("day", "hour", "minute"),
    [
        (_BUSINESS_DAY, 8, 0),
        (_BUSINESS_DAY, 9, 24),
        (_BUSINESS_DAY, 15, 30),
        (_SATURDAY, 12, 0),
        (_LONG_WEEKEND_REOPEN, 8, 0),
    ],
)
def test_mock_provider_price_passes_freshness_at_any_session_phase(day, hour, minute) -> None:
    """mock provider の価格は、どの局面で評価しても NORMAL になること。

    これが成立しないと、実行時刻によって既存テストが green / red に変わる。
    """
    calendar = business_calendar()
    now = _jst(day, hour, minute)
    snapshot = MockMarketDataProvider(now=now).get_latest_price(_MOCK_STOCK)
    assert snapshot is not None
    assert (
        evaluate_buy_price_freshness(snapshot.as_of_date, now, calendar)[0]
        is PriceFreshnessVerdict.NORMAL
    )
    assert (
        evaluate_holdings_price_freshness(snapshot.as_of_date, now, calendar)[0]
        is PriceFreshnessVerdict.NORMAL
    )


def test_mock_provider_uses_jst_calendar_day_not_utc(calendar) -> None:
    """UTC 暦日で打ち切っていた旧実装との差を直接固定する。

    UTC 2026-09-03T00:24(= JST 09:24、IN_SESSION)では当日barを返してよい。
    UTC 2026-09-02T23:00(= JST 翌08:00、PRE_OPEN)では当日barを返してはならない。
    """
    in_session = dt.datetime(2026, 9, 3, 0, 24, tzinfo=dt.UTC)
    pre_open = dt.datetime(2026, 9, 2, 23, 0, tzinfo=dt.UTC)

    assert (
        MockMarketDataProvider(now=in_session).get_latest_price(_MOCK_STOCK).as_of_date
        == _BUSINESS_DAY
    )
    assert (
        MockMarketDataProvider(now=pre_open).get_latest_price(_MOCK_STOCK).as_of_date
        == _PREV_BUSINESS_DAY
    )


# --- session 定数の契約 ---------------------------------------------------------


def test_session_constants_match_jpx_regular_session() -> None:
    """寄付・大引けの定数を固定する(市場営業時間の仕様変更は本Issueの範囲外)。"""
    assert dt.time(9, 0) == JPX_REGULAR_SESSION_OPEN_JST
    assert dt.time(15, 30) == JPX_REGULAR_SESSION_CLOSE_JST


# --- mock as_of の完全マトリクス(局面ごとの期待日を明示的に固定) -----------------


@pytest.mark.parametrize(
    ("label", "day", "hour", "minute", "expected_as_of"),
    [
        ("PRE_OPEN_0800", _BUSINESS_DAY, 8, 0, _PREV_BUSINESS_DAY),
        ("PRE_OPEN_0859", _BUSINESS_DAY, 8, 59, _PREV_BUSINESS_DAY),
        ("OPEN_0900", _BUSINESS_DAY, 9, 0, _BUSINESS_DAY),
        ("IN_SESSION_0924", _BUSINESS_DAY, 9, 24, _BUSINESS_DAY),
        ("BEFORE_CLOSE_1529", _BUSINESS_DAY, 15, 29, _BUSINESS_DAY),
        ("CLOSE_1530", _BUSINESS_DAY, 15, 30, _BUSINESS_DAY),
        ("POST_CLOSE_1600", _BUSINESS_DAY, 16, 0, _BUSINESS_DAY),
        ("WEEKEND", _SATURDAY, 12, 0, _NEXT_BUSINESS_DAY),
        ("LONG_WEEKEND_PRE_OPEN", _LONG_WEEKEND_REOPEN, 8, 0, _LONG_WEEKEND_LAST_BUSINESS_DAY),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_mock_as_of_matrix(label, day, hour, minute, expected_as_of) -> None:
    """mock provider が各局面で返す `as_of_date` を1件ずつ固定する。

    `latest_plausible_bar_date` を超えないことだけでなく、
    **どの日付を返すか**まで明示する(局面ごとの期待を曖昧にしない)。
    """
    now = _jst(day, hour, minute)
    snapshot = MockMarketDataProvider(now=now).get_latest_price(_MOCK_STOCK)
    assert snapshot is not None
    assert snapshot.as_of_date == expected_as_of


# --- mutation guard: 3方向すべてを非空振りで固定 ---------------------------------
#
# これらは「壊し方」を直接表現したテストである。実装を各方向へ戻したときに
# 必ず失敗することを保証し、guard が空振りしないようにする。


def test_mutation_guard_pre_open_must_not_accept_same_day_bar(calendar) -> None:
    """PRE_OPEN の当日barを NORMAL へ戻す mutation を検出する。

    mock を「常に当日」へ戻す(= 旧 UTC 暦日相当)と、寄付前に当日barが返り
    判定が fail-close する。両側から固定しておく。
    """
    now = _jst(_BUSINESS_DAY, 8)

    # 判定側: 寄付前の当日barは必ず fail-close
    assert evaluate_buy_price_freshness(_BUSINESS_DAY, now, calendar)[0] is (
        PriceFreshnessVerdict.HARD_STOP
    )
    assert evaluate_holdings_price_freshness(_BUSINESS_DAY, now, calendar)[0] is (
        PriceFreshnessVerdict.DATA_INSUFFICIENT
    )

    # provider側: 寄付前は当日barを返さない
    snapshot = MockMarketDataProvider(now=now).get_latest_price(_MOCK_STOCK)
    assert snapshot is not None
    assert snapshot.as_of_date != _BUSINESS_DAY
    assert snapshot.as_of_date == _PREV_BUSINESS_DAY


def test_mutation_guard_in_session_must_accept_same_day_bar(calendar) -> None:
    """IN_SESSION の当日barを未来扱いへ戻す mutation を検出する。"""
    now = _jst(_BUSINESS_DAY, 9, 24)
    assert latest_plausible_bar_date(now, calendar) == _BUSINESS_DAY
    assert evaluate_buy_price_freshness(_BUSINESS_DAY, now, calendar)[0] is (
        PriceFreshnessVerdict.NORMAL
    )
    assert evaluate_holdings_price_freshness(_BUSINESS_DAY, now, calendar)[0] is (
        PriceFreshnessVerdict.NORMAL
    )
    snapshot = MockMarketDataProvider(now=now).get_latest_price(_MOCK_STOCK)
    assert snapshot is not None
    assert snapshot.as_of_date == _BUSINESS_DAY


@pytest.mark.parametrize(("hour", "minute"), [(8, 0), (9, 24), (15, 30), (16, 0)])
def test_mutation_guard_true_future_must_stay_fail_closed(calendar, hour, minute) -> None:
    """真の未来を NORMAL へ緩める mutation を検出する。

    どの局面でも、翌営業日以降のbarは fail-close を維持する。
    """
    now = _jst(_BUSINESS_DAY, hour, minute)
    for future_date in (_NEXT_BUSINESS_DAY, dt.date(2026, 10, 1), dt.date(2027, 1, 4)):
        assert evaluate_buy_price_freshness(future_date, now, calendar)[0] is (
            PriceFreshnessVerdict.HARD_STOP
        )
        assert evaluate_holdings_price_freshness(future_date, now, calendar)[0] is (
            PriceFreshnessVerdict.DATA_INSUFFICIENT
        )


def test_non_business_day_same_day_bar_is_fail_closed(calendar) -> None:
    """非営業日の当日barは存在し得ない。fail-close を維持する。"""
    now = _jst(_SATURDAY, 12)
    assert evaluate_buy_price_freshness(_SATURDAY, now, calendar)[0] is (
        PriceFreshnessVerdict.HARD_STOP
    )
    assert evaluate_holdings_price_freshness(_SATURDAY, now, calendar)[0] is (
        PriceFreshnessVerdict.DATA_INSUFFICIENT
    )
