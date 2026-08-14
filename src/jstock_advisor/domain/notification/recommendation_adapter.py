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

from jstock_advisor.domain.entities.enums import (
    NotificationCategory,
    RecommendationType,
    WatchTransitionType,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.notification.message_formatter import NotificationTextInput

_WATCH_END_REASON_LABELS: dict[str, str] = {
    "PRICE_OUT_OF_RANGE": "買い水準から離脱",
    "NOT_ATTRACTIVE": "企業魅力度が低下",
    "STALE": "データ取得不可のため終了",
}

_CRITICAL_RISK_DEFAULT_REASON = "重大リスクのため緊急に保有内容の確認が必要です"

# 「全部売却検討」相当のRecommendationType(コードレビュー対応2026-08、
# LINE通知/監査分離)。SELLカテゴリを共用しつつlabel_overrideで区別する。
_FULL_SELL_RECOMMENDATION_TYPES = frozenset(
    {
        RecommendationType.STRONG_SELL_CONSIDERATION,
        RecommendationType.FULL_PROFIT_TAKE,
    }
)
_FULL_SELL_LABEL = "全部売却検討"
_FULL_SELL_WITHHELD_LABEL = "全部売却目安は算定保留"
_SELL_WITHHELD_LABEL = "売却目安は算定保留"
_MANUAL_REVIEW_REASON = "売買判断を保留"
_WATCH_PRICE_WITHHELD_LABEL = "価格目安は算定保留"
_PARTIAL_SELL_WITHHELD_LABEL = "売却目安は算定保留"
_PARTIAL_RISK_REDUCTION_LABEL = "一部縮小"


def _entry_price(recommendation: Recommendation) -> Decimal | None:
    prices = recommendation.buy_prices
    if prices is not None and prices.entry is not None:
        return prices.entry.price
    return None


def _standard_price(recommendation: Recommendation) -> Decimal | None:
    prices = recommendation.buy_prices
    if prices is not None and prices.standard is not None:
        return prices.standard.price
    return None


def _build_buy(recommendation: Recommendation) -> NotificationTextInput:
    promoted = recommendation.watch_transition_type == WatchTransitionType.PROMOTED_TO_BUY.value
    return NotificationTextInput(
        category=NotificationCategory.BUY,
        stock_code=recommendation.stock_code,
        stock_name=recommendation.stock_name,
        current_price=recommendation.price_at_recommendation,
        target_price=_entry_price(recommendation),
        # コードレビュー対応(2026-08、LINE通知/監査分離): 打診買い価格に加え、
        # 通常買い価格も併記する(secondary_target_price、reason文字列への
        # 埋め込みはしない)。
        secondary_target_price=_standard_price(recommendation),
        secondary_target_price_label="通常",
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


def _sell_target_price_and_label(
    recommendation: Recommendation,
) -> tuple[Decimal | None, str | None]:
    """SELL側の価格ラベル(コードレビュー対応2026-08、指摘3・LINE通知/監査分離)。
    「打診」は買い価格の表現であり、売却価格にそのまま流用すると意味的に
    不自然になる(common.pyのSellPriceLevelsドキュストリング参照)ため、価格
    フィールドの業務的意味に応じたラベルを個別に設定する。

    immediate_execution_price: 即時執行が真に必要な場合(URGENT_REVIEW等)の
    現在値ベースの参考価格 → 「即時執行」(共通)。

    STRONG_SELL_CONSIDERATION/FULL_PROFIT_TAKE(全部売却検討系)は
    full_profit_consideration_price → 「全部売却目安」を見る。
    stop_review_priceは常に現在値の監視専用フィールドであり、目安価格として
    表示すると「見直し{現在値}円」という誤解を招く表示になる不具合が過去に
    あったため、全部売却検討系では参照しない。

    それ以外(SELL/SELL_CONSIDERATION等)はstop_review_price → 「見直し」を
    見る(既存どおり)。
    """
    sp = recommendation.sell_prices
    if sp is None:
        return None, None
    if sp.immediate_execution_price is not None:
        return sp.immediate_execution_price.price, "即時執行"
    if recommendation.recommendation_type in _FULL_SELL_RECOMMENDATION_TYPES:
        if sp.full_profit_consideration_price is not None:
            return sp.full_profit_consideration_price.price, "全部売却目安"
        return None, None
    if sp.stop_review_price is not None:
        return sp.stop_review_price.price, "見直し"
    return None, None


def _build_sell(recommendation: Recommendation) -> NotificationTextInput:
    reason = recommendation.reasons[0] if recommendation.reasons else None
    if reason is None:
        reason = recommendation.recommended_action_summary
    target_price, target_price_label = _sell_target_price_and_label(recommendation)
    is_full_sell = recommendation.recommendation_type in _FULL_SELL_RECOMMENDATION_TYPES
    withheld_label = _FULL_SELL_WITHHELD_LABEL if is_full_sell else _SELL_WITHHELD_LABEL
    return NotificationTextInput(
        category=NotificationCategory.SELL,
        stock_code=recommendation.stock_code,
        stock_name=recommendation.stock_name,
        current_price=recommendation.price_at_recommendation,
        target_price=target_price,
        target_price_label=target_price_label,
        target_price_withheld_label=withheld_label if target_price is None else None,
        label_override=_FULL_SELL_LABEL if is_full_sell else None,
        reason=reason,
    )


def _build_critical_risk(recommendation: Recommendation) -> NotificationTextInput:
    reason = " / ".join(recommendation.reasons) if recommendation.reasons else None
    if not reason:
        reason = recommendation.recommended_action_summary or _CRITICAL_RISK_DEFAULT_REASON
    sp = recommendation.sell_prices
    target_price = (
        sp.immediate_execution_price.price
        if sp is not None and sp.immediate_execution_price is not None
        else None
    )
    return NotificationTextInput(
        category=NotificationCategory.CRITICAL_RISK,
        stock_code=recommendation.stock_code,
        stock_name=recommendation.stock_name,
        current_price=recommendation.price_at_recommendation,
        target_price=target_price,
        target_price_label="即時執行" if target_price is not None else None,
        reason=reason,
    )


def _build_watch(recommendation: Recommendation) -> NotificationTextInput:
    """利確WATCH・決算前監視・決算前後のレビュー保留・ポートフォリオ集中
    リスクをまとめる「監視」カテゴリ(コードレビュー対応2026-08、LINE通知/
    監査分離)。

    RecommendationType.WATCH(利確判定エンジン由来)のみ`partial_profit_start_
    price`(利確検討を開始する水準、即時売却を意味しない)を目安価格として
    持つ。それ以外(決算前監視・決算前後のレビュー保留・ポートフォリオ集中
    リスク)は価格フィールド自体が存在しない構造のため、reasonのみ表示する。

    RecommendationType.WATCH_BEFORE_EARNINGS(利確判定エンジンのWATCH抑制
    専用。買い候補側のBuyAction.WATCH_BEFORE_EARNINGS由来のNotificationCategory.
    WATCH_BEFORE_EARNINGSとは別物)は、決算接近によりまだ利確検討水準には
    達していない銘柄の監視を一旦保留している状態のため、REVIEW_BEFORE_
    EARNINGS(既に利確検討水準へ到達済みで確認待ち)とは異なる文言を使う
    (再コードレビュー対応2026-08、実装漏れ修正)。
    """
    if recommendation.recommendation_type == RecommendationType.WATCH:
        sp = recommendation.sell_prices
        target_price = (
            sp.partial_profit_start_price.price
            if sp is not None and sp.partial_profit_start_price is not None
            else None
        )
        return NotificationTextInput(
            category=NotificationCategory.WATCH,
            stock_code=recommendation.stock_code,
            stock_name=recommendation.stock_name,
            current_price=recommendation.price_at_recommendation,
            target_price=target_price,
            target_price_label="利確検討",
            target_price_withheld_label=(
                _WATCH_PRICE_WITHHELD_LABEL if target_price is None else None
            ),
        )
    if recommendation.recommendation_type == RecommendationType.WATCH_BEFORE_EARNINGS:
        return NotificationTextInput(
            category=NotificationCategory.WATCH,
            stock_code=recommendation.stock_code,
            stock_name=recommendation.stock_name,
            current_price=recommendation.price_at_recommendation,
            reason="決算発表接近のため様子見",
        )
    if recommendation.recommendation_type == RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW:
        weight = recommendation.portfolio_weight_pct
        reason = f"保有比率{weight:.1f}%" if weight is not None else None
        return NotificationTextInput(
            category=NotificationCategory.WATCH,
            stock_code=recommendation.stock_code,
            stock_name=recommendation.stock_name,
            current_price=recommendation.price_at_recommendation,
            reason=reason,
        )
    # REVIEW_BEFORE_EARNINGS/REVIEW_AFTER_EARNINGS: 決算発表状況確認待ち
    # (価格情報なし。長文フォーマット時代の見出し文言"決算発表状況確認待ち"を
    # 踏襲する)。
    return NotificationTextInput(
        category=NotificationCategory.WATCH,
        stock_code=recommendation.stock_code,
        stock_name=recommendation.stock_name,
        current_price=recommendation.price_at_recommendation,
        reason="決算発表状況確認待ち",
    )


def _build_partial_sell(recommendation: Recommendation) -> NotificationTextInput:
    """一部売却(PARTIAL_PROFIT_TAKE)・一部縮小(PARTIAL_RISK_REDUCTION、決算
    接近による表示ラベルのみの差し替え)を扱う(コードレビュー対応2026-08、
    LINE通知/監査分離)。内部の価格計算経路は両者で同一
    (`_compute_sell_prices`のPARTIAL分岐)。
    """
    sp = recommendation.sell_prices
    target_price: Decimal | None = None
    if sp is not None:
        if sp.recommended_limit_price is not None:
            target_price = sp.recommended_limit_price.price
        elif sp.partial_profit_start_price is not None:
            target_price = sp.partial_profit_start_price.price
    is_risk_reduction = (
        recommendation.recommendation_type == RecommendationType.PARTIAL_RISK_REDUCTION
    )
    return NotificationTextInput(
        category=NotificationCategory.PARTIAL_SELL,
        stock_code=recommendation.stock_code,
        stock_name=recommendation.stock_name,
        current_price=recommendation.price_at_recommendation,
        target_price=target_price,
        target_price_label="売却目安",
        target_price_withheld_label=(
            _PARTIAL_SELL_WITHHELD_LABEL if target_price is None else None
        ),
        label_override=_PARTIAL_RISK_REDUCTION_LABEL if is_risk_reduction else None,
    )


def _build_manual_review(recommendation: Recommendation) -> NotificationTextInput:
    """自動判定の安全条件を満たさず要手動確認となったケース(コードレビュー
    対応2026-08、LINE通知/監査分離)。RecommendationType.REVIEW(単一の根拠
    のみで根拠不足)・MANUAL_REVIEW_REQUIRED(データ品質アラート由来)の
    いずれも、価格は提示せず「売買判断を保留」で統一する(詳細な検出内容・
    独立根拠数等はAudit/Recommendationに残り、LINE本文には出さない)。
    """
    return NotificationTextInput(
        category=NotificationCategory.MANUAL_REVIEW,
        stock_code=recommendation.stock_code,
        stock_name=recommendation.stock_name,
        current_price=recommendation.price_at_recommendation,
        reason=_MANUAL_REVIEW_REASON,
    )


_BUILDERS = {
    NotificationCategory.BUY: _build_buy,
    NotificationCategory.NEAR_BUY: _build_near_buy,
    NotificationCategory.WATCH_BEFORE_EARNINGS: _build_watch_before_earnings,
    NotificationCategory.SELL: _build_sell,
    NotificationCategory.CRITICAL_RISK: _build_critical_risk,
    NotificationCategory.WATCH: _build_watch,
    NotificationCategory.PARTIAL_SELL: _build_partial_sell,
    NotificationCategory.MANUAL_REVIEW: _build_manual_review,
}

# 簡潔化(50/70文字ルール)の対象となるカテゴリ。OTHER・NOT_NOTIFIABLEのみ
# 対象外(実質、通知が発生するRecommendationTypeはすべてこのモジュール経由の
# 短文エンジンで表現する。コードレビュー対応2026-08、LINE通知/監査分離)。
# `_format_message()`長文フォーマットはrender_notification_preview()診断
# 専用として維持される(実送信経路からは到達しない)。
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
