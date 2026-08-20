"""LINE通知を送るべきかどうかの意味論的判定(2026-08、通知意図3段階化)の唯一の正本。

`NotificationCategory`(resolve_notification_category()、line_notification_service.py)
が「表示テンプレート選択」のための分類であるのに対し、本モジュールの
`NotificationIntent`は「送るか送らないか」を決める意味論を表す。同じ
`RecommendationType.WATCH`/`NotificationCategory.WATCH`でも、Profit Protectionの
candidate/strongシグナルに起因する場合はATTENTION、決算待ち・ポートフォリオ集中等が
理由の場合はINTERNAL_ONLYと、WHYによって異なる意図になる。

本モジュールはdomain層に置くため、service層の`resolve_notification_category()`を
直接importしない(domain→service方向の逆依存を作らないため、recommendation_adapter.py
と同じ制約)。そのため`resolve_notification_intent()`はRecommendationそのものではなく、
呼び出し側(service層)が`resolve_notification_category()`で既に算出した
`NotificationCategory`を受け取る。Recommendationを直接受け取る利便関数
(`resolve_notification_intent_for_recommendation()`等)はline_notification_service.py
側に置き、そちらが本モジュールへ委譲する。

呼び出し側(送信ゲート・監査記録・サマリ集計のいずれも)はこの委譲チェーンの結果のみを
参照し、独自の条件式を重複実装しないこと。
"""

from __future__ import annotations

from jstock_advisor.domain.entities.enums import NotificationCategory, NotificationIntent

# ATTENTIONの初期スコープ(要求仕様2026-08): Profit Protectionのcandidate/strong
# シグナルに起因するWATCHのみ。STRONGはpartial_sale_executable=False(単元未満等の
# 理由で一部売却を提案できない)の場合にのみWATCHへ着地することを実コードで確認済み
# (profit_taking.pyのorigin floorロジック、_RawLevelOrigin.PROFIT_PROTECTION_STRONGは
# raw_level>=PARTIAL成立時に必ずfinal_level>=PARTIALへ床上げされるため、STRONG WATCHは
# この経路以外から生じない)。
_ATTENTION_PROFIT_PROTECTION_SIGNALS = frozenset({"CANDIDATE", "STRONG"})

# 2026-08「LINE通知アクション限定化」で導入された非アクション系カテゴリの
# denylist(line_notification_service.pyの旧_NON_ACTIONABLE_CATEGORIESと同じ
# 4カテゴリ)。通知意図3段階化ではこのdenylist方式(デフォルト許可・明示的拒否)を
# そのまま踏襲する。allowlist方式(デフォルト拒否)に変更すると、OTHER(BUY/SELL
# 系のbuy_action/recommendation_type設定漏れ等、明示的にゲートされてこなかった
# 残余区分)を新たに誤ってブロックしてしまう(コードレビュー対応2026-08、
# 実装中の回帰テストで検出)。
_INTERNAL_ONLY_CATEGORIES = frozenset(
    {
        NotificationCategory.WATCH,
        NotificationCategory.MANUAL_REVIEW,
        NotificationCategory.NEAR_BUY,
        NotificationCategory.WATCH_BEFORE_EARNINGS,
    }
)


def resolve_notification_intent(
    category: NotificationCategory,
    profit_protection_signal: str | None,
) -> NotificationIntent:
    """NotificationCategoryとProfit Protectionシグナルから送信意図を判定する。

    categoryは呼び出し側がresolve_notification_category()で算出済みのものを渡すこと
    (本関数はカテゴリ判定ロジックを重複実装しない)。
    """
    if (
        category is NotificationCategory.WATCH
        and profit_protection_signal in _ATTENTION_PROFIT_PROTECTION_SIGNALS
    ):
        return NotificationIntent.ATTENTION
    if category in _INTERNAL_ONLY_CATEGORIES:
        return NotificationIntent.INTERNAL_ONLY
    return NotificationIntent.ACTIONABLE


def resolve_attention_origin(
    category: NotificationCategory,
    profit_protection_signal: str | None,
) -> str | None:
    """ATTENTIONの場合のみ、その根拠を表す文字列を返す。ATTENTION以外はNone。

    STRONG起因の場合、attention_originは「単元未満等の理由で一部売却を提案できない
    STRONG WATCH」という観測された状態のみを表し、内部原因を断定しない
    (`PROFIT_PROTECTION_STRONG_NOT_EXECUTABLE`という名前自体はコード上の実証済みの
    唯一の成立経路(partial_sale_executable=False)を指すが、具体的な原因(単元未満か
    奇数株取引不可か等)はAuditの`partial_sale_executable`/`trading_unit`フィールド側で
    追跡する)。
    """
    intent = resolve_notification_intent(category, profit_protection_signal)
    if intent is not NotificationIntent.ATTENTION:
        return None
    if profit_protection_signal == "STRONG":
        return "PROFIT_PROTECTION_STRONG_NOT_EXECUTABLE"
    return "PROFIT_PROTECTION_CANDIDATE"
