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
               as_of が未来日 -> HARD_STOP

holdings/SELL  missed=0 -> NORMAL / missed>=1 -> DATA_INSUFFICIENT
               as_of UNKNOWN -> DATA_INSUFFICIENT
               as_of が未来日 -> DATA_INSUFFICIENT
```

**閾値は変更しない。** 変更したのは「未来日」の定義のみである(下記)。

未来日は「取りこぼし0(=新鮮)」ではなく **timestamp / data integrity の異常**として
扱う。`missed_trading_sessions()` は未来日に対して 0 を返すが(観測としては正しい)、
その 0 をそのまま judgement へ通すと未来日の価格が NORMAL になってしまう。
観測の contract は壊さず、policy層で分離する(`_is_future_as_of`)。

## 「未確定」と「未来」は別概念(Phase B2 regression 是正)

未来判定の基準は `latest_plausible_bar_date()`(barが存在しうる最も新しい営業日)
であり、`expected_latest_completed_trading_session()` ではない。

```
寄付前          当日barは存在しない  -> 未来判定の基準 = 前営業日
寄付〜大引け前   当日barは未確定だが実在 -> 基準 = 当日(未来ではない)
大引け後        当日barは確定        -> 基準 = 当日
非営業日        -> 基準 = 直前営業日
```

当初の実装は完了セッションを基準にしていたため、市場時間中の当日barを
未来と誤判定し、BUY/holdings判定を全銘柄で停止させた。

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
    JPX_REGULAR_SESSION_OPEN_JST,
    latest_plausible_bar_date,
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
_REASON_FUTURE_BUY: Final = "株価の基準日が未来日({as_of})のため買い判定を実施しない"
_REASON_FUTURE_HOLDINGS: Final = "株価の基準日が未来日({as_of})のため判定できません"
_REASON_UNKNOWN_HOLDINGS: Final = "株価の基準日を確認できないため判定できません"
_REASON_WARNING_BUY: Final = "株価が{missed}取引セッション前のものです"
_REASON_HARD_STOP_BUY: Final = "株価が{missed}取引セッション以上前のため買い判定を実施しない"
_REASON_INSUFFICIENT_HOLDINGS: Final = (
    "最新の株価を確認できない(株価が{missed}取引セッション前)ため判定できません"
)


def _is_future_as_of(
    as_of_date: dt.date,
    now: dt.datetime,
    calendar: BusinessCalendar,
    session_open_jst: dt.time,
    session_close_jst: dt.time,
) -> bool:
    """価格の基準日が、その時点で**市場上存在し得ない**未来日か。

    未来日の市場価格は「新鮮」ではなく **timestamp / data integrity の異常**である。
    まだ発生していない取引の終値が存在するはずがないため、provider側の時刻ずれ、
    タイムゾーン取り違え、あるいはデータ破損を示す。

    ## 「未確定」と「未来」を混同しない(Phase B2 regression 是正)

    基準は `latest_plausible_bar_date()`(barが存在しうる最も新しい営業日)であり、
    `expected_latest_completed_trading_session()`(既に大引けを迎えた営業日)では**ない**。

    ```
    寄付前          当日barは存在しない -> 基準 = 前営業日
    寄付〜大引け前   当日barは未確定だが実在する -> 基準 = 当日
    大引け後        当日barは確定 -> 基準 = 当日
    非営業日        -> 基準 = 直前営業日
    ```

    当初の実装は完了セッションを基準にしていたため、**市場時間中の当日barを
    未来と誤判定し、BUY/holdings判定を全銘柄で停止させた**
    (2026-09-03 に本番providerで再現。Issue #52 の durable record 参照)。
    未確定であることは鮮度(`missed_trading_sessions`)側の関心事であり、
    未来判定の基準にしない。

    `missed_trading_sessions()` は未来日に対して 0 を返す(取りこぼしは負にならない、
    という観測としては正しい contract)。しかしその 0 を鮮度judgementへそのまま
    通すと **真の未来日の価格が NORMAL として売買判定に使われる**。
    観測の contract は壊さず、policy層でこの異常を明示的に検査して分離する。

    なお `filter_future_bars`(domain/signals)は **`as_of_date` を基準として
    未来の PriceBar を除外する**ものであり、`as_of_date` 自身の妥当性は検査しない。
    さらに市場・セクター環境スコアの経路でのみ呼ばれ、`get_latest_price` →
    `StockSnapshot.price_as_of_date` → BUY/holdings 判定の経路には適用されない。
    したがって未来 as_of の検査はここで行う必要がある。
    """
    plausible = latest_plausible_bar_date(now, calendar, session_open_jst, session_close_jst)
    return as_of_date > plausible


def evaluate_buy_price_freshness(
    as_of_date: dt.date | None,
    now: dt.datetime,
    calendar: BusinessCalendar,
    session_close_jst: dt.time = JPX_REGULAR_SESSION_CLOSE_JST,
    session_open_jst: dt.time = JPX_REGULAR_SESSION_OPEN_JST,
) -> tuple[PriceFreshnessVerdict, str | None]:
    """BUY経路の価格鮮度を判定する。

    戻り値は `(判定, 理由)`。`NORMAL` のときのみ理由は `None`。

    `HARD_STOP` の理由は呼び出し側で `exclusion_reasons` へ、
    `WARNING` の理由は `warnings` へ入れることを想定する。
    """
    if as_of_date is None:
        return PriceFreshnessVerdict.HARD_STOP, _REASON_UNKNOWN_BUY
    if _is_future_as_of(as_of_date, now, calendar, session_open_jst, session_close_jst):
        return (
            PriceFreshnessVerdict.HARD_STOP,
            _REASON_FUTURE_BUY.format(as_of=as_of_date.isoformat()),
        )

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
    session_close_jst: dt.time = JPX_REGULAR_SESSION_CLOSE_JST,
    session_open_jst: dt.time = JPX_REGULAR_SESSION_OPEN_JST,
) -> tuple[PriceFreshnessVerdict, str | None]:
    """保有銘柄(売却判定・利確判定を含む)の価格鮮度を判定する。

    BUYより厳格にする。1取引セッションでも取りこぼしていれば
    `DATA_INSUFFICIENT` とし、古い価格で損切り・利確を確定させない。

    **当該銘柄を判定不能とするだけ**であり、バッチ全体を止める意図はない
    (呼び出し側は既存の銘柄単位ハンドリングへ委ねる)。
    """
    if as_of_date is None:
        return PriceFreshnessVerdict.DATA_INSUFFICIENT, _REASON_UNKNOWN_HOLDINGS
    if _is_future_as_of(as_of_date, now, calendar, session_open_jst, session_close_jst):
        return (
            PriceFreshnessVerdict.DATA_INSUFFICIENT,
            _REASON_FUTURE_HOLDINGS.format(as_of=as_of_date.isoformat()),
        )

    missed = missed_trading_sessions(as_of_date, now, calendar, session_close_jst)
    if missed is None:  # pragma: no cover - as_of_date is not None のため到達しない
        return PriceFreshnessVerdict.DATA_INSUFFICIENT, _REASON_UNKNOWN_HOLDINGS
    if missed >= HOLDINGS_DATA_INSUFFICIENT_MISSED_SESSIONS:
        return (
            PriceFreshnessVerdict.DATA_INSUFFICIENT,
            _REASON_INSUFFICIENT_HOLDINGS.format(missed=missed),
        )
    return PriceFreshnessVerdict.NORMAL, None
