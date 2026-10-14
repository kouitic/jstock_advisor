"""資金入替の観測の『対象バッチの特定』と『起動の窓の上限』(Issue #604 P-b2。#128 R2。dormant)。

P-b の読み手(BatchRuns・保有の記録)が使う、時刻まわりの純粋な定義だけを持つ。
設計: #604 P-b 設計 rev6(…6169320796)の F2・F3 + rev8(…6169691749)の MUST-1。
**時計・I/O・AWS・環境変数に触れない**。どこからも import されない(dormant。配線は P-b4)。

## 保有バッチの batch_id(F3)

Scheduler 起動の保有バッチは `holdings-watchlist-{JST の日付 YYYYMMDD}T{JST の予定時刻 HHMMSS}`
(`lambda_handlers/_scheduling.derive_scheduled_batch_id` の形。★ 日付はハイフンなし ★)。
`batch_id` も `scheduled_time` も無い手動起動はランダムの接尾辞が付き、★ ここからは導出できない ★
(= P は Scheduler 起動のバッチだけを観測する)。予定時刻は注入する(既定 08:00:00 JST)。
予定時刻が変わったら『行が無い』= 読めない日になる(fail-closed)。契約テストが、実際の
`derive_scheduled_batch_id` と同じ文字列になることを固定する。

## 起動の窓の上限(MUST-1)

BatchRuns の行は ★ TTL 6 時間 ★ で消える(`batch_tracker._TTL_HOURS`。契約テストで一致を固定)。
DynamoDB の TTL は即時の削除ではないため、6 時間の境界付近は『読める日と読めない日』が混ざる
非決定になる(常に失敗するより悪い)。したがって起動の窓は、予定時刻から ★ TTL − 余裕 ★ 以内に収める。
`latest_safe_run_at` がその上限(08:00 JST の予定なら 13:00 JST)。
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from jstock_advisor.domain.jst import JST

#: BatchRuns の行の TTL(時間)。`batch_tracker._TTL_HOURS` と一致する(契約テストで固定)
BATCH_RUN_TTL_HOURS: Final = 6
#: TTL の境界(非決定の帯)を避ける余裕(時間)
READ_MARGIN_HOURS: Final = 1
#: 保有バッチの batch_id の prefix(`holdings_watchlist_handler` の `derive_scheduled_batch_id`)
HOLDINGS_BATCH_PREFIX: Final = "holdings-watchlist"
#: 保有バッチの Scheduler の予定時刻(JST。template の cron(0 8 ? * MON-FRI)・Asia/Tokyo)
DEFAULT_SCHEDULED_TIME: Final = dt.time(8, 0, 0)


class ScheduleContractError(ValueError):
    """入力の型・値が契約に反する。値を含めない。"""


def _check(target_date: object, scheduled_time: object) -> None:
    if not isinstance(target_date, dt.date) or isinstance(target_date, dt.datetime):
        raise ScheduleContractError("target_date must be a date")
    if not isinstance(scheduled_time, dt.time) or scheduled_time.tzinfo is not None:
        raise ScheduleContractError("scheduled_time must be a naive time of day (JST)")


def holdings_batch_id(
    target_date: dt.date, scheduled_time: dt.time = DEFAULT_SCHEDULED_TIME
) -> str:
    """対象日(JST)の保有バッチの batch_id(Scheduler 起動のもの)。"""
    _check(target_date, scheduled_time)
    return f"{HOLDINGS_BATCH_PREFIX}-{target_date:%Y%m%d}T{scheduled_time:%H%M%S}"


def scheduled_at(
    target_date: dt.date, scheduled_time: dt.time = DEFAULT_SCHEDULED_TIME
) -> dt.datetime:
    """対象日の予定時刻(JST の aware)。"""
    _check(target_date, scheduled_time)
    return dt.datetime.combine(target_date, scheduled_time, tzinfo=JST)


def latest_safe_run_at(
    target_date: dt.date, scheduled_time: dt.time = DEFAULT_SCHEDULED_TIME
) -> dt.datetime:
    """P の最後の回の上限(予定時刻 + TTL − 余裕。JST の aware)。これを超える窓は非決定になる。"""
    return scheduled_at(target_date, scheduled_time) + dt.timedelta(
        hours=BATCH_RUN_TTL_HOURS - READ_MARGIN_HOURS
    )
