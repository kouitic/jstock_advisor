"""Lambdaハンドラ間で共有するスケジュール判定ヘルパー。

EventBridge Schedulerのcron式は「第1土曜日」のような月内序数指定に対応していない
ため、毎週土曜に実行したうえでLambda側で「今日が当月第1土曜日か」を判定する
(当初設計の方針: 「実行日が当月第1土曜日かどうかはLambda側で判定」)。
"""

from __future__ import annotations

import datetime as dt


def is_first_saturday_of_month(date: dt.date) -> bool:
    return date.weekday() == 5 and date.day <= 7
