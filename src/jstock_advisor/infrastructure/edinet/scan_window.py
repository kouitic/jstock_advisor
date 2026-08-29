"""EDINET走査範囲と走査済み範囲の前進規約(Issue #53 Phase B1)。

disclosure_finder(臨時報告書)とdocument_finder(有報・半期報告書)が同じ規約で
動くよう、走査開始日の決定と`newest_scanned_date`の前進をここへ集約する。
"""

from __future__ import annotations

import datetime as dt

# refresh window(暦日)。当日+直前N暦日を毎回再走査する。
# 営業日ではなく暦日で定義するのは、連休・祝日を跨いでも直前の提出可能日を必ず
# 窓へ含めるため(JPX BusinessCalendarへの新たな依存を作らない)。土日祝は
# EDINETが0件を返し、日付単位キャッシュへ1回だけ記録されるだけなので安価。
DEFAULT_REFRESH_WINDOW_DAYS = 7
# 設定ミスで窓が無制限に広がる(=毎回大量再取得する)ことを防ぐ上限。
MAX_REFRESH_WINDOW_DAYS = 14


def validate_refresh_window_days(refresh_window_days: int) -> int:
    if refresh_window_days < 0 or refresh_window_days > MAX_REFRESH_WINDOW_DAYS:
        raise ValueError(
            f"refresh_window_daysは0〜{MAX_REFRESH_WINDOW_DAYS}の範囲で指定してください: "
            f"{refresh_window_days}"
        )
    return refresh_window_days


def business_days_between(start: dt.date, end: dt.date) -> list[dt.date]:
    """start〜end(両端含む)の平日を列挙する。

    祝日は除外しない(EDINETは祝日に0件を返すだけであり、祝日判定を誤って
    走査対象から落とすより安全側)。
    """
    days: list[dt.date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += dt.timedelta(days=1)
    return days


def compute_scan_start(
    today: dt.date,
    previous_newest_scanned: dt.date | None,
    initial_lookback_days: int,
    refresh_window_days: int,
) -> dt.date:
    """走査開始日(JST暦日)を決める。

    キャッシュが無ければ初期lookback。あれば「未走査分の先頭」と
    「refresh windowの先頭」の早い方から走査する(直近は毎回再走査する)。
    """
    if previous_newest_scanned is None:
        return today - dt.timedelta(days=initial_lookback_days)
    return min(
        previous_newest_scanned + dt.timedelta(days=1),
        today - dt.timedelta(days=refresh_window_days),
    )


def advance_newest_scanned(
    previous_newest_scanned: dt.date | None, last_complete_scan_date: dt.date | None
) -> dt.date | None:
    """走査済み範囲の末尾を決める。

    連続して完了した範囲の末尾までしか前進させない(取得に失敗した日を走査済みに
    すると、その営業日は二度と走査されない=cache poisoning)。既存の走査済み範囲を
    巻き戻すこともしない(直近を再走査するため開始日が過去へ戻ることがある)。
    戻り値がNoneなら「走査済みと言える日が一度も無い」ことを表す。
    """
    if last_complete_scan_date is None:
        return previous_newest_scanned
    if previous_newest_scanned is None:
        return last_complete_scan_date
    return max(previous_newest_scanned, last_complete_scan_date)
