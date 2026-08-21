"""通知意図3段階化(2026-08)のdomain層resolver(notification_intent.py)の単体テスト。

送信ゲート・監査・サマリー集計いずれもresolve_notification_intent()/
resolve_attention_origin()の結果だけを参照する設計のため、ここでの正しさが
全体の正しさに直結する。line_notification_service.py側の統合(Recommendation
経由の利便関数・実際の送信ゲート)はtest_line_notification_service.pyで検証する。
"""

from __future__ import annotations

from jstock_advisor.domain.entities.enums import NotificationCategory, NotificationIntent
from jstock_advisor.domain.notification.notification_intent import (
    resolve_attention_origin,
    resolve_notification_intent,
)


def test_actionable_categories_are_always_actionable() -> None:
    for category in (
        NotificationCategory.CRITICAL_RISK,
        NotificationCategory.BUY,
        NotificationCategory.SELL,
        NotificationCategory.PARTIAL_SELL,
    ):
        assert resolve_notification_intent(category, None) is NotificationIntent.ACTIONABLE
        assert resolve_attention_origin(category, None) is None


def test_watch_with_candidate_signal_is_attention() -> None:
    intent = resolve_notification_intent(NotificationCategory.WATCH, "CANDIDATE")
    assert intent is NotificationIntent.ATTENTION
    assert resolve_attention_origin(NotificationCategory.WATCH, "CANDIDATE") == (
        "PROFIT_PROTECTION_CANDIDATE"
    )


def test_watch_with_strong_signal_is_attention_with_distinct_origin() -> None:
    intent = resolve_notification_intent(NotificationCategory.WATCH, "STRONG")
    assert intent is NotificationIntent.ATTENTION
    assert resolve_attention_origin(NotificationCategory.WATCH, "STRONG") == (
        "PROFIT_PROTECTION_STRONG_NOT_EXECUTABLE"
    )


def test_watch_without_profit_protection_signal_is_internal_only() -> None:
    for signal in (None, "NONE", "DATA_INSUFFICIENT"):
        intent = resolve_notification_intent(NotificationCategory.WATCH, signal)
        assert intent is NotificationIntent.INTERNAL_ONLY, signal
        assert resolve_attention_origin(NotificationCategory.WATCH, signal) is None


def test_manual_review_near_buy_watch_before_earnings_are_internal_only_even_with_signal() -> None:
    """profit_protection_signalはWATCH以外のカテゴリでは一切意味を持たない
    (誤ってMANUAL_REVIEW等をATTENTION化しない)。"""
    for category in (
        NotificationCategory.MANUAL_REVIEW,
        NotificationCategory.NEAR_BUY,
        NotificationCategory.WATCH_BEFORE_EARNINGS,
    ):
        intent = resolve_notification_intent(category, "CANDIDATE")
        assert intent is NotificationIntent.INTERNAL_ONLY, category


def test_other_and_not_notifiable_are_internal_only_fail_closed() -> None:
    """再コードレビュー対応(2026-08): allowlist方式(fail-closed)へ変更した。
    明示的にACTIONABLEと定義されないカテゴリ(OTHER/NOT_NOTIFIABLE含む)は、
    未知・未分類であってもINTERNAL_ONLYとして扱う(「送信意図の唯一の正本」が
    fail-openだと安全側ではないため)。実コード調査により、OTHERカテゴリ経由で
    実際にLINE送信される本番経路は存在しないことを確認済み(HOLD/WATCH_BUYは
    送信経路に到達せず、MANUAL_REVIEW_REQUIREDはこのintent判定より前の安全弁
    経路で処理される)。"""
    for category in (NotificationCategory.OTHER, NotificationCategory.NOT_NOTIFIABLE):
        assert resolve_notification_intent(category, None) is NotificationIntent.INTERNAL_ONLY
