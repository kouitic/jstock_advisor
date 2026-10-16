"""通知カテゴリ・attention_origin を Recommendation から決める唯一の正本(domain 層)。

`resolve_notification_category()` と `resolve_attention_origin_for_recommendation()` は、
もとは services/line_notification_service.py にあった(F5。#604 P-b4 の前提)。
資金回転の観測 Lambda(P)は attention_origin を正本で再計算する必要があるが、
LINE 通知の service を import すると LINE 送信・永続化の closure を引き込み、#606 の
接続ゲートに反する。そのため、依存が domain.entities と notification_intent だけの
本 module へ**そのまま**移した(判定ロジックは 1 文字も変えていない)。

line_notification_service.py は本 module から import して使う(定義は持たない)。
LINE 通知・監査・サマリ・観測のいずれも、本 module の関数だけを呼ぶこと(重複実装の禁止)。
"""

from __future__ import annotations

from jstock_advisor.domain.entities.enums import (
    BUY_FAMILY_ACTIONS,
    BuyAction,
    NotificationCategory,
    RecommendationType,
    WatchType,
    is_critical_risk,
    is_sell_like,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.notification.notification_intent import (
    resolve_attention_origin as _resolve_attention_origin,
)


def resolve_notification_category(recommendation: Recommendation) -> NotificationCategory:
    """通知テンプレート・優先度・再送ポリシー選択のための唯一の分類ソース
    (BUY候補裾野拡大機能2026-08)。`evaluate_notification_status()`の前段
    ゲート・`_notification_status_for_send()`のNEAR BUY/WATCH_BEFORE_EARNINGS
    早期リターン・優先度比較のすべてがこの関数の結果だけを見る(buy_action/
    watch_type/recommendation_typeを個別に参照する判定を各所に重複させない)。

    NotificationCategory.WATCH_BEFORE_EARNINGS(買い候補側の決算待ち監視)の
    判定は必ず`recommendation.buy_action`を見る。`recommendation_type`側の
    同名メンバー(`RecommendationType.WATCH_BEFORE_EARNINGS`)は利確判定
    エンジンのWATCH抑制専用(buy_signal_service.pyのコメント参照)で、
    `buy_action=None`のときのみ到達し、NotificationCategory.WATCHへ分類する
    (コードレビュー対応2026-08: 以前はどの分岐にも一致せずOTHERへ落ち、
    実送信時に旧長文フォーマットを使ってしまう不備があった)。
    """
    if is_critical_risk(recommendation.recommendation_type):
        return NotificationCategory.CRITICAL_RISK
    if recommendation.buy_action is not None:
        if recommendation.buy_action in BUY_FAMILY_ACTIONS:
            return NotificationCategory.BUY
        if (
            recommendation.buy_action == BuyAction.WATCH_FOR_PRICE
            and recommendation.watch_type == WatchType.NEAR_BUY
        ):
            return NotificationCategory.NEAR_BUY
        if recommendation.buy_action == BuyAction.WATCH_BEFORE_EARNINGS:
            return NotificationCategory.WATCH_BEFORE_EARNINGS
        # 通常のWATCH_FOR_PRICE(NEAR BUY非該当)・MANUAL_REVIEW・NOT_ATTRACTIVE・
        # EXCLUDED・DATA_INSUFFICIENTはいずれも通知対象外(要求仕様6節)。
        return NotificationCategory.NOT_NOTIFIABLE
    # コードレビュー対応(2026-08、LINE通知/監査分離): RecommendationType.REVIEWは
    # enum定義上「単一の根拠のみで、SELL/URGENT_REVIEWへ進めるには不十分」という
    # 意味であり、is_sell_like()に含まれる(SELL_LIKE_RECOMMENDATION_TYPES自体は
    # 買い増しゲート・整合性検証・history replay分類など他箇所で使うため変更
    # しない)が、ユーザー向けには「売却検討」ではなく「要確認」として見せる。
    # is_sell_like()より前に判定する(is_critical_riskの既存パターンと同じ形)。
    if recommendation.recommendation_type == RecommendationType.REVIEW:
        return NotificationCategory.MANUAL_REVIEW
    if is_sell_like(recommendation.recommendation_type):
        return NotificationCategory.SELL
    # FULL_PROFIT_TAKEはSELL_LIKE_RECOMMENDATION_TYPESに含まれないため個別に
    # SELLへ分類する(STRONG_SELL_CONSIDERATIONとともに「全部売却検討」表示)。
    if recommendation.recommendation_type == RecommendationType.FULL_PROFIT_TAKE:
        return NotificationCategory.SELL
    if recommendation.recommendation_type in (
        RecommendationType.PARTIAL_PROFIT_TAKE,
        RecommendationType.PARTIAL_RISK_REDUCTION,
    ):
        return NotificationCategory.PARTIAL_SELL
    if recommendation.recommendation_type in (
        RecommendationType.WATCH,
        RecommendationType.WATCH_BEFORE_EARNINGS,
        RecommendationType.REVIEW_BEFORE_EARNINGS,
        RecommendationType.REVIEW_AFTER_EARNINGS,
        RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW,
    ):
        return NotificationCategory.WATCH
    # buy_action=Noneの残り(HOLD等)は既存どおりOTHER扱い(通知対象外の判定は
    # 呼び出し元のnotify_below_score等が別途行う)。
    return NotificationCategory.OTHER


def resolve_attention_origin_for_recommendation(recommendation: Recommendation) -> str | None:
    category = resolve_notification_category(recommendation)
    return _resolve_attention_origin(category, recommendation.profit_protection_signal)
