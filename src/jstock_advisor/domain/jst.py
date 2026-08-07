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


def evaluation_date_jst(now: dt.datetime) -> dt.date:
    """比較用のJST暦日を取得する(決算日修正デプロイ前対応)。

    now(UTC-aware)に対して直接.date()を呼ぶと、JST 00:00-09:00の間は
    UTC上の前日日付になってしまい、決算予定日の過去/当日判定・営業日数計算が
    誤判定する(明治HD事例と同種のタイムゾーン境界バグ)。決算日関連の暦日比較は
    必ずこの関数を経由し、now.date()を直接呼ばない。
    """
    return to_jst(now).date()


def require_timezone_aware(now: dt.datetime) -> None:
    """評価時刻がtimezone-awareであることを保証する(naiveを暗黙にUTC扱いしない)。"""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
