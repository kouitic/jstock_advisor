"""市場の取引セッションを基準とした価格データの鮮度計算(Issue #52 Phase B1)。

## なぜ「取得時刻」ではなく「取引セッション」で測るのか

`DataSourceReference.fetched_at` は**APIを叩いた時刻**であり、返ってきたデータが
いつ時点のものかを表さない。yfinance系providerは常に `fetched_at = now` を設定する
ため、10営業日前の終値を今取得しても「年齢0日」となり、鮮度ゲートが発火しない。

価格については `PriceSnapshot.as_of_date`(実際に約定したbarの日付)が既に取得できて
いるため、これを「データが真である時点」として使える。

さらに「今日からの経過日数」でも不十分である。JPXは平日でも祝日は開かず、また
当日の大引け前は当日のbarがまだ存在しない。「昨日の終値しか無い」ことが正常な
場面(平日の朝の実行)と異常な場面(売買停止・整理銘柄)を区別するには、
**期待される直近の完了済みセッションから何セッション取りこぼしているか**で測る必要がある。

    EXPECTED_LATEST_COMPLETED_TRADING_SESSION
        now 時点で既に大引けを迎えている直近のJPX営業日

    MISSED_TRADING_SESSIONS
        as_of_date が上記から何営業日ぶん遅れているか

これにより、平日朝08:00の実行(前営業日の終値が最新 = missed 0)と、
売買停止で数セッション取り残された状態(missed >= 1)を区別できる。

## 本モジュールの位置づけ

**本モジュールは観測値を算出するだけであり、いかなる業務判定にも接続されていない**
(Issue #52 Phase B1 の scope)。閾値と挙動(WARNING / HARD_STOP /
DATA_INSUFFICIENT)は判定policy側の関心事であり、データ自身の属性として持たせない。
接続は Phase B2 で人間が閾値を確定したのちに行う。

## as_of が不明な場合

`fetched_at` へ fallback して鮮度を判定してはならない(Issue #52 の設計判断)。
`fetched_at` は取得時刻であって基準時点ではないため、fallback すると
「常に新鮮」と誤判定する原因そのものを再導入することになる。
基準時点が不明な場合は `MISSED_SESSIONS_UNKNOWN` を返す。
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Final

from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.jst import require_timezone_aware, to_jst

# JPXの通常立会の大引け(2024-11-05以降)。JST。
# 現物の日足barはこの時刻以降に確定するため、これより前は当日のbarを期待しない。
JPX_REGULAR_SESSION_CLOSE_JST: Final = dt.time(15, 30)

# JPXの通常立会の寄付(前場開始)。JST。
# `now` がこれより前であれば、当日はまだ1本も約定していない。
JPX_REGULAR_SESSION_OPEN_JST: Final = dt.time(9, 0)


class MarketSessionState(StrEnum):
    """`now` 時点のJPX通常立会の状態(**`now` の状態であり、データの状態ではない**)。

    Issue #52 の回帰は「最新の完了セッション」と「正当に観測されうる最大日付」を
    同一の関数で表現したことに起因する。両者を分けるには、まず `now` が
    立会のどこに位置するかを明示的な状態として持つ必要がある。

    PRE_OPEN          営業日・寄付前。当日のbarはまだ存在しない
    IN_SESSION        営業日・立会中。当日のbarは存在するが**未確定**
    POST_CLOSE        営業日・大引け後。当日のbarが確定している
    NON_BUSINESS_DAY  非営業日(土日・祝日・臨時休場)。当日のbarは存在しえない
    """

    PRE_OPEN = "PRE_OPEN"
    IN_SESSION = "IN_SESSION"
    POST_CLOSE = "POST_CLOSE"
    NON_BUSINESS_DAY = "NON_BUSINESS_DAY"


def classify_market_session(
    now: dt.datetime,
    calendar: BusinessCalendar,
    session_open_jst: dt.time = JPX_REGULAR_SESSION_OPEN_JST,
    session_close_jst: dt.time = JPX_REGULAR_SESSION_CLOSE_JST,
) -> MarketSessionState:
    """`now` がJPX通常立会のどの状態にあるかを返す。

    暦日・時刻はすべてJST基準で判定する(`domain/jst.py` の規約に従う)。
    本関数は**観測**であり、いかなる閾値も持たない。

    境界の扱い(Issue #52 で固定):

        寄付ちょうど(09:00)   IN_SESSION   立会は開始している
        大引けちょうど(15:30) POST_CLOSE   barは確定している

    Raises:
        ValueError: `now` がtimezone-naiveな場合。
    """
    require_timezone_aware(now)
    jst_now = to_jst(now)
    if not calendar.is_business_day(jst_now.date()):
        return MarketSessionState.NON_BUSINESS_DAY
    current = jst_now.time()
    if current < session_open_jst:
        return MarketSessionState.PRE_OPEN
    if current < session_close_jst:
        return MarketSessionState.IN_SESSION
    return MarketSessionState.POST_CLOSE


# as_of が不明で鮮度を評価できないことを表す。0(=新鮮)と混同しないための番兵。
MISSED_SESSIONS_UNKNOWN: Final = None


def expected_latest_completed_trading_session(
    now: dt.datetime,
    calendar: BusinessCalendar,
    session_close_jst: dt.time = JPX_REGULAR_SESSION_CLOSE_JST,
) -> dt.date:
    """`now` 時点で**既に大引けを迎えている**直近のJPX営業日(JST暦日)を返す。

    当日が営業日であっても大引け前であれば当日のbarはまだ存在しないため、
    前営業日まで遡る。土日・祝日・臨時休場も営業日ではないため遡る。

    暦日はすべてJST基準で判定する(JPXの営業日はJST基準であり、UTC暦日を
    使うとJST 00:00〜08:59の実行で1日ずれる。`domain/jst.py` の規約に従う)。

    例(JPX大引け15:30 JST):
        平日 08:00 JST  -> 前営業日(当日はまだ大引け前)
        平日 18:00 JST  -> 当日
        土曜 12:00 JST  -> 直前の金曜
        月曜が祝日で火曜 08:00 JST -> 前週金曜

    `now` はtimezone-aware必須。naiveなdatetimeを暗黙にUTC扱いすると、
    JST 00:00〜08:59に相当する時刻で1日ずれた営業日を返し、
    本Issueが是正しようとしている「基準がずれたまま鮮度を判定する」問題を
    別の形で再導入することになる(`domain/jst.py` の規約に従う)。

    Raises:
        ValueError: `now` がtimezone-naiveな場合。
    """
    require_timezone_aware(now)
    jst_now = to_jst(now)
    candidate = jst_now.date()

    session_completed_today = (
        calendar.is_business_day(candidate) and jst_now.time() >= session_close_jst
    )
    if not session_completed_today:
        candidate -= dt.timedelta(days=1)
        while not calendar.is_business_day(candidate):
            candidate -= dt.timedelta(days=1)
    return candidate


def missed_trading_sessions(
    as_of_date: dt.date | None,
    now: dt.datetime,
    calendar: BusinessCalendar,
    session_close_jst: dt.time = JPX_REGULAR_SESSION_CLOSE_JST,
) -> int | None:
    """価格の基準日が、期待される直近完了セッションから何営業日ぶん遅れているかを返す。

    戻り値:
        0以上の整数   取りこぼしたセッション数(0 = 期待どおり最新)
        None          `as_of_date` が不明で評価できない(MISSED_SESSIONS_UNKNOWN)

    `as_of_date` が期待セッションより**未来**の場合も 0 を返す。
    「取りこぼし」は負にならないためであり、未来barそのものの検出は
    `filter_future_bars`(domain/signals)の責務である。

    `fetched_at` へのfallbackは行わない(モジュールdocstring参照)。

    `now` のtimezone-aware検証は`expected_latest_completed_trading_session()`が
    行う。`as_of_date`がNoneの場合は同関数を呼ばずに返すため、その経路では
    `now`を検証しない(検証すべき用途が無いため。重複チェックは置かない)。

    Raises:
        ValueError: `as_of_date` が指定されており、かつ `now` がtimezone-naiveな場合。
    """
    if as_of_date is None:
        return MISSED_SESSIONS_UNKNOWN

    expected = expected_latest_completed_trading_session(now, calendar, session_close_jst)
    if as_of_date >= expected:
        return 0
    return calendar.business_days_between(as_of_date, expected)
