"""表示用の日本時間(JST)変換ヘルパー。

内部の日時はすべてUTCで保持・保存する(タイムゾーンを跨いだ比較・保存の一貫性を
保つため)。ユーザー向けの表示(LINE通知・CLI出力)でのみJSTへ変換する。
"""

from __future__ import annotations

import datetime as dt

JST = dt.timezone(dt.timedelta(hours=9))


def to_jst(value: dt.datetime) -> dt.datetime:
    return value.astimezone(JST)


def format_jst(value: dt.datetime) -> str:
    return to_jst(value).strftime("%Y-%m-%d %H:%M JST")
