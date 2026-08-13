"""通知本文の生成・切り詰めルール(BUY候補裾野拡大機能2026-08、要求仕様§9)。

原則50文字程度・最大70文字程度(Python `len()`基準)。判定・銘柄コード・
銘柄名は必須(削れない)。優先順位: 1.判定 2.銘柄コード・銘柄名 3.現在値
4.目標価格/乖離率 5.WATCH連続日数 6.理由 7.StockType(最大2件)。この
簡潔化はユーザー向けLINE通知本文にのみ適用し、内部データ・監査ログの
情報量は一切減らさない(呼び出し元は本モジュールを通知テキスト生成にのみ
使い、Recommendation自体は完全な情報を保持したまま保存すること)。

重大リスク(is_critical_risk)の場合のみ、70文字上限を厳密に適用せず、
理由情報を欠落させない(例外条件)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from jstock_advisor.domain.entities.enums import NotificationCategory, StockType, stock_type_label

TARGET_CHARS = 50
MAX_CHARS = 70
MAX_STOCK_TYPES_SHOWN = 2

_CATEGORY_LABELS: dict[NotificationCategory, str] = {
    NotificationCategory.BUY: "買い",
    NotificationCategory.NEAR_BUY: "接近",
    NotificationCategory.WATCH_BEFORE_EARNINGS: "決算待ち",
    NotificationCategory.SELL: "売却検討",
    NotificationCategory.CRITICAL_RISK: "緊急",
    NotificationCategory.OTHER: "通知",
}

_WATCH_END_LABEL = "監視終了"


@dataclass(frozen=True)
class NotificationTextInput:
    category: NotificationCategory
    stock_code: str
    stock_name: str
    current_price: Decimal | None = None
    target_price: Decimal | None = None
    # 「あと何%」の接近率(NEAR BUY等)。正の値。
    distance_pct: Decimal | None = None
    consecutive_business_days: int | None = None
    # PAUSED後リセット(§5-3): 評価不能を挟んで連続日数が1へリセットされた
    # 直後であることを示す(「◯日連続」ではなく「監視再開」相当の文言にする)。
    is_resumed_after_gap: bool = False
    is_watch_end: bool = False
    reason: str | None = None
    stock_types: list[StockType] = field(default_factory=list)


def _fmt_price(price: Decimal) -> str:
    return f"{price:,.0f}円"


def _representative_stock_types(stock_types: list[StockType]) -> str:
    if not stock_types:
        return ""
    shown = stock_types[:MAX_STOCK_TYPES_SHOWN]
    return "・".join(stock_type_label(t) for t in shown)


def format_notification_text(
    data: NotificationTextInput,
    is_critical_risk: bool = False,
    max_chars: int = MAX_CHARS,
) -> str:
    """優先度順に情報セグメントを組み立て、max_charsを超える場合は低優先度の
    セグメントから順に落として収める。重大リスクの場合(is_critical_risk=True)は
    理由(reason)を欠落させないため、max_charsを厳密な上限として扱わない。

    文字数はPython `len()`で判定する(全角/半角・絵文字を区別しない)。
    """
    label = _WATCH_END_LABEL if data.is_watch_end else _CATEGORY_LABELS.get(data.category, "通知")

    # 優先度1・2は必須(削れない)。
    required = f"{label} {data.stock_code} {data.stock_name}"

    optional_segments: list[str] = []  # 優先度の高い順
    if data.current_price is not None:
        optional_segments.append(_fmt_price(data.current_price))
    if data.target_price is not None and data.distance_pct is not None:
        optional_segments.append(f"打診{_fmt_price(data.target_price)}まで{data.distance_pct:.1f}%")
    elif data.target_price is not None:
        optional_segments.append(f"打診{_fmt_price(data.target_price)}")
    elif data.distance_pct is not None:
        optional_segments.append(f"あと{data.distance_pct:.1f}%")
    if data.is_resumed_after_gap:
        optional_segments.append("監視再開")
    elif data.consecutive_business_days is not None:
        optional_segments.append(f"{data.consecutive_business_days}日連続")
    if data.reason:
        optional_segments.append(data.reason)
    type_label = _representative_stock_types(data.stock_types)
    if type_label:
        optional_segments.append(type_label)

    text = required
    for segment in optional_segments:
        candidate = f"{text}｜{segment}" if text != required else f"{text}\n{segment}"
        # 重大リスクはmax_charsを厳密な上限として扱わず、理由情報等を欠落させない
        # (要求仕様の例外条件)。それ以外は上限を超える最初のセグメントで打ち切る。
        if is_critical_risk or len(candidate) <= max_chars:
            text = candidate
        else:
            break
    return text
