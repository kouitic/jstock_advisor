"""Recommendation → NotificationTextInput変換(コードレビュー対応2026-08、最優先)。

`format_notification_text()`は純粋関数として`message_formatter.py`に実装済み
だったが、実際にLINEへ送信される本文の生成経路(`LineNotificationService.
send_recommendation_notification()`)からは呼ばれておらず、旧来の長文
`_format_message()`のままだった。本モジュールは、`NotificationCategory`
ごとに`Recommendation`のどのフィールドを`NotificationTextInput`へマッピング
するかを一元管理し、実送信経路とWATCH終了通知の両方から共通で使う。

`NotificationCategory`の判定自体(`resolve_notification_category()`)は
サービス層(line_notification_service.py)の責務のままとし、本モジュールは
判定済みのカテゴリを受け取るだけに留める(domain層からservice層への逆依存を
作らないため)。
"""

from __future__ import annotations

from decimal import Decimal

from jstock_advisor.domain.entities.enums import NotificationCategory, WatchTransitionType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.notification.message_formatter import NotificationTextInput

_WATCH_END_REASON_LABELS: dict[str, str] = {
    "PRICE_OUT_OF_RANGE": "買い水準から離脱",
    "NOT_ATTRACTIVE": "企業魅力度が低下",
    "STALE": "データ取得不可のため終了",
}

_CRITICAL_RISK_DEFAULT_REASON = "重大リスクのため緊急に保有内容の確認が必要です"


def _entry_price(recommendation: Recommendation) -> Decimal | None:
    prices = recommendation.buy_prices
    if prices is not None and prices.entry is not None:
        return prices.entry.price
    return None


def _build_buy(recommendation: Recommendation) -> NotificationTextInput:
    promoted = recommendation.watch_transition_type == WatchTransitionType.PROMOTED_TO_BUY.value
    return NotificationTextInput(
        category=NotificationCategory.BUY,
        stock_code=recommendation.stock_code,
        stock_name=recommendation.stock_name,
        current_price=recommendation.price_at_recommendation,
        target_price=_entry_price(recommendation),
        label_override="到達" if promoted else None,
        promoted_from_watch_days=(
            recommendation.watch_previous_consecutive_business_days if promoted else None
        ),
        stock_types=list(recommendation.stock_types),
    )


def _build_near_buy(recommendation: Recommendation) -> NotificationTextInput:
    return NotificationTextInput(
        category=NotificationCategory.NEAR_BUY,
        stock_code=recommendation.stock_code,
        stock_name=recommendation.stock_name,
        current_price=recommendation.price_at_recommendation,
        target_price=_entry_price(recommendation),
        distance_pct=recommendation.required_decline_to_entry_pct,
        consecutive_business_days=recommendation.near_buy_consecutive_business_days,
        is_resumed_after_gap=(
            recommendation.watch_transition_type == WatchTransitionType.RESUMED.value
        ),
        stock_types=list(recommendation.stock_types),
    )


def _build_watch_before_earnings(recommendation: Recommendation) -> NotificationTextInput:
    return NotificationTextInput(
        category=NotificationCategory.WATCH_BEFORE_EARNINGS,
        stock_code=recommendation.stock_code,
        stock_name=recommendation.stock_name,
        current_price=recommendation.price_at_recommendation,
        reason="決算発表接近のため様子見",
    )


def _sell_target_price(recommendation: Recommendation) -> Decimal | None:
    sp = recommendation.sell_prices
    if sp is None:
        return None
    if sp.immediate_execution_price is not None:
        return sp.immediate_execution_price.price
    if sp.stop_review_price is not None:
        return sp.stop_review_price.price
    return None


def _build_sell(recommendation: Recommendation) -> NotificationTextInput:
    reason = recommendation.reasons[0] if recommendation.reasons else None
    if reason is None:
        reason = recommendation.recommended_action_summary
    return NotificationTextInput(
        category=NotificationCategory.SELL,
        stock_code=recommendation.stock_code,
        stock_name=recommendation.stock_name,
        current_price=recommendation.price_at_recommendation,
        target_price=_sell_target_price(recommendation),
        reason=reason,
    )


def _build_critical_risk(recommendation: Recommendation) -> NotificationTextInput:
    reason = " / ".join(recommendation.reasons) if recommendation.reasons else None
    if not reason:
        reason = recommendation.recommended_action_summary or _CRITICAL_RISK_DEFAULT_REASON
    return NotificationTextInput(
        category=NotificationCategory.CRITICAL_RISK,
        stock_code=recommendation.stock_code,
        stock_name=recommendation.stock_name,
        current_price=recommendation.price_at_recommendation,
        reason=reason,
    )


_BUILDERS = {
    NotificationCategory.BUY: _build_buy,
    NotificationCategory.NEAR_BUY: _build_near_buy,
    NotificationCategory.WATCH_BEFORE_EARNINGS: _build_watch_before_earnings,
    NotificationCategory.SELL: _build_sell,
    NotificationCategory.CRITICAL_RISK: _build_critical_risk,
}

# 簡潔化(50/70文字ルール)の対象となるカテゴリ。OTHER(利確・保有判断スコア
# 以外の一部・ポートフォリオ集中リスク等)・NOT_NOTIFIABLEは対象外のまま
# 従来の長文フォーマットを維持する(レビュー指摘1の対象範囲=このカテゴリ集合)。
SHORT_TEXT_CATEGORIES = frozenset(_BUILDERS.keys())


def build_notification_text_input(
    recommendation: Recommendation, category: NotificationCategory
) -> NotificationTextInput:
    """簡潔化対象カテゴリのRecommendationをNotificationTextInputへ変換する。

    `category`はSHORT_TEXT_CATEGORIESに含まれる値であること(呼び出し元が
    `resolve_notification_category()`の結果を渡す)。
    """
    return _BUILDERS[category](recommendation)


def build_watch_end_text_input(recommendation: Recommendation) -> NotificationTextInput:
    """WATCH終了通知(§3)専用。watch_end_reason/watch_previous_consecutive_
    business_daysが設定されているRecommendationにのみ呼ぶこと。
    """
    reason = _WATCH_END_REASON_LABELS.get(
        recommendation.watch_end_reason or "", recommendation.watch_end_reason or ""
    )
    return NotificationTextInput(
        category=NotificationCategory.NEAR_BUY,
        stock_code=recommendation.stock_code,
        stock_name=recommendation.stock_name,
        is_watch_end=True,
        watch_end_days=recommendation.watch_previous_consecutive_business_days,
        reason=reason,
    )
