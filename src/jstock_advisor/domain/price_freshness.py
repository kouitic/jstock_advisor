"""価格鮮度の判定policy(Issue #52 Phase B2)。

Phase B1 で追加した観測(`domain/market_session.py` の
`expected_latest_completed_trading_session` / `missed_trading_sessions`)を、
**判定contextごとのpolicy**へ変換する層である。

## 観測とpolicyを分ける理由

`missed_trading_sessions` は「何取引セッション取りこぼしているか」という**事実**であり、
それを許容するかどうかは判定の文脈で異なる。閾値をデータ自身の属性として持たせると、
BUYとSELLで別の許容度を持てなくなる。

```
BUY            買わずに見送るコストは機会損失のみ
holdings/SELL  売る/売らないの誤りは実損に直結する
```

そのため **BUYとholdings/SELLで閾値を非対称にする**(人間の確定判断、2026-09-02)。

## 決定表(人間確定。ここで再判断しない)

```
BUY            missed=0 -> NORMAL / missed=1 -> WARNING / missed>=2 -> HARD_STOP
               as_of UNKNOWN -> HARD_STOP
               as_of が TRUE_FUTURE / NON_TRADING_DAY -> HARD_STOP

holdings/SELL  missed=0 -> NORMAL / missed>=1 -> DATA_INSUFFICIENT
               as_of UNKNOWN -> DATA_INSUFFICIENT
               as_of が TRUE_FUTURE / NON_TRADING_DAY -> DATA_INSUFFICIENT

両経路共通  as_of が CURRENT_SESSION_PROVISIONAL -> NORMAL
```

## as_of の状態分類(Issue #52 回帰修正、2026-09-03)

当初の実装は「`expected_latest_completed_trading_session()` より後 = 未来日」と
していた。しかし provider は**立会中に当日の未確定barを返す**(実 provider を
read-only 実測して確認済み)一方、同関数は 15:30 JST までは前営業日を返す。
このため **毎営業日 09:00-15:30 の実行が必ず異常判定になる**回帰が生じた
(Issue #143 で CI が実行時刻により結果を変えた原因でもある)。

    「完了した最新セッション」  !=  「正当に観測されうる最大日付」

両者を分離し、`as_of` は `classify_as_of_date()` で状態へ写像する。

```
TRUE_FUTURE                  JST暦日の当日を超える。存在しえない -> fail-close
NON_TRADING_DAY              当日が非営業日なのに当日付 -> fail-close
CURRENT_SESSION_PROVISIONAL  進行中セッションの未確定bar -> 正常(取りこぼし0)
COMPLETED_SESSION            既存の missed ラダーで評価
```

`TRUE_FUTURE` の fail-close は**弱めない**。判定基準を「期待セッション」から
「JST暦日の当日」へ移しただけであり、真の未来日は従来どおり全経路で停止する。

`missed_trading_sessions()` は未来日に対して 0 を返すが(観測としては正しい)、
その 0 をそのまま judgement へ通すと未来日の価格が NORMAL になってしまう。
観測の contract は壊さず、policy層で分離する(`classify_as_of_date()`)。

休場日・土日・祝日・大引け後は**個別の閾値を持たない**。
`expected_latest_completed_trading_session()` の定義により missed=0 へ畳み込まれる
(Phase B1 の境界テストで固定済み)。ここで暦の再実装をしない
(Issue #66 と同じUTC/JST分散を再発させないため)。

既存の `max_data_age_business_days`(取得時刻ベースのgeneric freshness)は
**価格の閾値として流用しない**。両者を再び混ぜると Issue #52 の根本原因へ戻る。
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Final

from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.jst import to_jst
from jstock_advisor.domain.market_session import (
    JPX_REGULAR_SESSION_CLOSE_JST,
    JPX_REGULAR_SESSION_OPEN_JST,
    MarketSessionState,
    classify_market_session,
    missed_trading_sessions,
)

# 人間確定の閾値(2026-09-02)。ここを変更する場合は Issue #52 の decision record を更新すること。
BUY_WARNING_MISSED_SESSIONS: Final = 1
BUY_HARD_STOP_MISSED_SESSIONS: Final = 2
HOLDINGS_DATA_INSUFFICIENT_MISSED_SESSIONS: Final = 1


class PriceFreshnessVerdict(StrEnum):
    """価格鮮度の判定結果。

    NORMAL             期待どおり最新。既存挙動を変えない
    WARNING            判定は継続するが、古さを明示する
    HARD_STOP          BUY判定を実施しない(買い候補から除外する)
    DATA_INSUFFICIENT  判定不能として扱う(古い価格で売買判定を確定させない)
    """

    NORMAL = "NORMAL"
    WARNING = "WARNING"
    HARD_STOP = "HARD_STOP"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"


# 判定理由の文言はここへ集約する。各所へハードコードして分散させない。
_REASON_UNKNOWN_BUY: Final = "株価の基準日を確認できないため買い判定を実施しない"
_REASON_FUTURE_BUY: Final = (
    "株価の基準日が未来日({as_of})のため買い判定を実施しない"
)
_REASON_FUTURE_HOLDINGS: Final = "株価の基準日が未来日({as_of})のため判定できません"
_REASON_NON_TRADING_BUY: Final = (
    "株価の基準日({as_of})が非営業日のため買い判定を実施しない"
)
_REASON_NON_TRADING_HOLDINGS: Final = (
    "株価の基準日({as_of})が非営業日のため判定できません"
)
_REASON_UNKNOWN_HOLDINGS: Final = "株価の基準日を確認できないため判定できません"
_REASON_WARNING_BUY: Final = "株価が{missed}取引セッション前のものです"
_REASON_HARD_STOP_BUY: Final = (
    "株価が{missed}取引セッション以上前のため買い判定を実施しない"
)
_REASON_INSUFFICIENT_HOLDINGS: Final = (
    "最新の株価を確認できない(株価が{missed}取引セッション前)ため判定できません"
)


class AsOfDateState(StrEnum):
    """価格の基準日(`as_of_date`)が `now` に対してどの状態にあるか。

    Issue #52 の回帰の根本原因は、`expected_latest_completed_trading_session()`
    (= **鮮度の基準点**)を、そのまま **妥当性の上限** として使ったことである。
    両者は別概念であり、立会中は後者が当日を含む。

        「完了した最新セッション」  !=  「正当に観測されうる最大日付」

    本Enumはその区別を型として固定する。

    COMPLETED_SESSION            完了済みセッションのbar。既存の missed ラダーで評価する
    CURRENT_SESSION_PROVISIONAL  進行中セッションの**未確定**bar。異常ではない
    TRUE_FUTURE                  実在しえない未来日。timestamp / data integrity の異常
    NON_TRADING_DAY              非営業日付のbar。同じく integrity の異常
    """

    COMPLETED_SESSION = "COMPLETED_SESSION"
    CURRENT_SESSION_PROVISIONAL = "CURRENT_SESSION_PROVISIONAL"
    TRUE_FUTURE = "TRUE_FUTURE"
    NON_TRADING_DAY = "NON_TRADING_DAY"


def classify_as_of_date(
    as_of_date: dt.date,
    now: dt.datetime,
    calendar: BusinessCalendar,
    session_open_jst: dt.time = JPX_REGULAR_SESSION_OPEN_JST,
    session_close_jst: dt.time = JPX_REGULAR_SESSION_CLOSE_JST,
) -> AsOfDateState:
    """価格の基準日を `now` との関係で分類する。

    ## なぜ「期待される直近完了セッションより後 = 未来」ではないのか

    provider(yfinance系および mock)は、立会中に**当日の未確定bar**を返す。
    これは provider の異常ではなく正常な振る舞いである(Issue #52 で
    実 provider を read-only 実測して確認済み)。一方
    `expected_latest_completed_trading_session()` は 15:30 JST までは前営業日を返す。

    したがって「期待セッションより後」を未来判定にすると、
    **毎営業日 09:00-15:30 の実行が必ず異常判定になる**。これが Issue #52 の回帰であり、
    Issue #143 で CI が実行時刻により結果を変えた原因でもある。

    `TRUE_FUTURE` は **JST暦日の当日を超えているか**で判定する。
    まだ到来していない暦日のbarは存在しえないため、これは時刻ずれ・タイムゾーン
    取り違え・データ破損を示す真の異常であり、**fail-close を弱めない**。

    ## 非営業日付

    `as_of_date` が**当日**であって当日が非営業日の場合、その日付のbarは存在しえない
    ため `NON_TRADING_DAY` とする。過去日付については本関数は営業日検査を行わない
    (欠損・売買停止の表現として過去の非営業日付が来ることはなく、また
    既存の missed ラダーが遅れとして正しく評価するため)。
    """
    jst_today = to_jst(now).date()
    if as_of_date > jst_today:
        return AsOfDateState.TRUE_FUTURE
    if as_of_date < jst_today:
        return AsOfDateState.COMPLETED_SESSION

    session = classify_market_session(now, calendar, session_open_jst, session_close_jst)
    if session is MarketSessionState.NON_BUSINESS_DAY:
        return AsOfDateState.NON_TRADING_DAY
    if session is MarketSessionState.POST_CLOSE:
        return AsOfDateState.COMPLETED_SESSION
    return AsOfDateState.CURRENT_SESSION_PROVISIONAL


def evaluate_buy_price_freshness(
    as_of_date: dt.date | None,
    now: dt.datetime,
    calendar: BusinessCalendar,
    session_open_jst: dt.time = JPX_REGULAR_SESSION_OPEN_JST,
    session_close_jst: dt.time = JPX_REGULAR_SESSION_CLOSE_JST,
) -> tuple[PriceFreshnessVerdict, str | None]:
    """BUY経路の価格鮮度を判定する。

    戻り値は `(判定, 理由)`。`NORMAL` のときのみ理由は `None`。

    `HARD_STOP` の理由は呼び出し側で `exclusion_reasons` へ、
    `WARNING` の理由は `warnings` へ入れることを想定する。
    """
    if as_of_date is None:
        return PriceFreshnessVerdict.HARD_STOP, _REASON_UNKNOWN_BUY
    state = classify_as_of_date(as_of_date, now, calendar, session_open_jst, session_close_jst)
    if state is AsOfDateState.TRUE_FUTURE:
        return (
            PriceFreshnessVerdict.HARD_STOP,
            _REASON_FUTURE_BUY.format(as_of=as_of_date.isoformat()),
        )
    if state is AsOfDateState.NON_TRADING_DAY:
        return (
            PriceFreshnessVerdict.HARD_STOP,
            _REASON_NON_TRADING_BUY.format(as_of=as_of_date.isoformat()),
        )
    if state is AsOfDateState.CURRENT_SESSION_PROVISIONAL:
        # 進行中セッションの未確定bar。取りこぼしは無く、取得可能な中で最も新しい。
        # 鮮度ゲートは「古さ」を検出するためのものであり、ここで止めない(Issue #52)。
        return PriceFreshnessVerdict.NORMAL, None

    missed = missed_trading_sessions(as_of_date, now, calendar, session_close_jst)
    if missed is None:  # pragma: no cover - as_of_date is not None のため到達しない
        return PriceFreshnessVerdict.HARD_STOP, _REASON_UNKNOWN_BUY
    if missed >= BUY_HARD_STOP_MISSED_SESSIONS:
        return (
            PriceFreshnessVerdict.HARD_STOP,
            _REASON_HARD_STOP_BUY.format(missed=missed),
        )
    if missed >= BUY_WARNING_MISSED_SESSIONS:
        return PriceFreshnessVerdict.WARNING, _REASON_WARNING_BUY.format(missed=missed)
    return PriceFreshnessVerdict.NORMAL, None


def evaluate_holdings_price_freshness(
    as_of_date: dt.date | None,
    now: dt.datetime,
    calendar: BusinessCalendar,
    session_open_jst: dt.time = JPX_REGULAR_SESSION_OPEN_JST,
    session_close_jst: dt.time = JPX_REGULAR_SESSION_CLOSE_JST,
) -> tuple[PriceFreshnessVerdict, str | None]:
    """保有銘柄(売却判定・利確判定を含む)の価格鮮度を判定する。

    BUYより厳格にする。1取引セッションでも取りこぼしていれば
    `DATA_INSUFFICIENT` とし、古い価格で損切り・利確を確定させない。

    **当該銘柄を判定不能とするだけ**であり、バッチ全体を止める意図はない
    (呼び出し側は既存の銘柄単位ハンドリングへ委ねる)。
    """
    if as_of_date is None:
        return PriceFreshnessVerdict.DATA_INSUFFICIENT, _REASON_UNKNOWN_HOLDINGS
    state = classify_as_of_date(as_of_date, now, calendar, session_open_jst, session_close_jst)
    if state is AsOfDateState.TRUE_FUTURE:
        return (
            PriceFreshnessVerdict.DATA_INSUFFICIENT,
            _REASON_FUTURE_HOLDINGS.format(as_of=as_of_date.isoformat()),
        )
    if state is AsOfDateState.NON_TRADING_DAY:
        return (
            PriceFreshnessVerdict.DATA_INSUFFICIENT,
            _REASON_NON_TRADING_HOLDINGS.format(as_of=as_of_date.isoformat()),
        )
    if state is AsOfDateState.CURRENT_SESSION_PROVISIONAL:
        # 進行中セッションの未確定bar。損切り・利確はむしろ最新値で判定すべきであり、
        # ここで DATA_INSUFFICIENT にすると立会中は保有判定が全て不能になる(Issue #52)。
        return PriceFreshnessVerdict.NORMAL, None

    missed = missed_trading_sessions(as_of_date, now, calendar, session_close_jst)
    if missed is None:  # pragma: no cover - as_of_date is not None のため到達しない
        return PriceFreshnessVerdict.DATA_INSUFFICIENT, _REASON_UNKNOWN_HOLDINGS
    if missed >= HOLDINGS_DATA_INSUFFICIENT_MISSED_SESSIONS:
        return (
            PriceFreshnessVerdict.DATA_INSUFFICIENT,
            _REASON_INSUFFICIENT_HOLDINGS.format(missed=missed),
        )
    return PriceFreshnessVerdict.NORMAL, None
