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

holdings/SELL  missed=0 -> NORMAL / missed>=1 -> DATA_INSUFFICIENT
               as_of UNKNOWN -> DATA_INSUFFICIENT
```

休場日・土日・祝日・寄付前・大引け前後は**個別の閾値を持たない**。
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
from jstock_advisor.domain.market_session import (
    JPX_REGULAR_SESSION_CLOSE_JST,
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
_REASON_UNKNOWN_HOLDINGS: Final = "株価の基準日を確認できないため判定できません"
_REASON_WARNING_BUY: Final = "株価が{missed}取引セッション前のものです"
_REASON_HARD_STOP_BUY: Final = (
    "株価が{missed}取引セッション以上前のため買い判定を実施しない"
)
_REASON_INSUFFICIENT_HOLDINGS: Final = (
    "最新の株価を確認できない(株価が{missed}取引セッション前)ため判定できません"
)


def evaluate_buy_price_freshness(
    as_of_date: dt.date | None,
    now: dt.datetime,
    calendar: BusinessCalendar,
    session_close_jst: dt.time = JPX_REGULAR_SESSION_CLOSE_JST,
) -> tuple[PriceFreshnessVerdict, str | None]:
    """BUY経路の価格鮮度を判定する。

    戻り値は `(判定, 理由)`。`NORMAL` のときのみ理由は `None`。

    `HARD_STOP` の理由は呼び出し側で `exclusion_reasons` へ、
    `WARNING` の理由は `warnings` へ入れることを想定する。
    """
    missed = missed_trading_sessions(as_of_date, now, calendar, session_close_jst)
    if missed is None:
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
    session_close_jst: dt.time = JPX_REGULAR_SESSION_CLOSE_JST,
) -> tuple[PriceFreshnessVerdict, str | None]:
    """保有銘柄(売却判定・利確判定を含む)の価格鮮度を判定する。

    BUYより厳格にする。1取引セッションでも取りこぼしていれば
    `DATA_INSUFFICIENT` とし、古い価格で損切り・利確を確定させない。

    **当該銘柄を判定不能とするだけ**であり、バッチ全体を止める意図はない
    (呼び出し側は既存の銘柄単位ハンドリングへ委ねる)。
    """
    missed = missed_trading_sessions(as_of_date, now, calendar, session_close_jst)
    if missed is None:
        return PriceFreshnessVerdict.DATA_INSUFFICIENT, _REASON_UNKNOWN_HOLDINGS
    if missed >= HOLDINGS_DATA_INSUFFICIENT_MISSED_SESSIONS:
        return (
            PriceFreshnessVerdict.DATA_INSUFFICIENT,
            _REASON_INSUFFICIENT_HOLDINGS.format(missed=missed),
        )
    return PriceFreshnessVerdict.NORMAL, None
