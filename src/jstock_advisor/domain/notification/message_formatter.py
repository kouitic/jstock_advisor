"""通知本文の生成・切り詰めルール(BUY候補裾野拡大機能2026-08、要求仕様§9・
Profit Protection Round 3 コードレビュー対応)。

原則50文字程度・目安70文字(Python `len()`基準)。判定・銘柄コード・銘柄名は
必須(削れない)。優先順位: 1.判定 2.銘柄コード・銘柄名 3.現在値 4.売却数量
(PARTIAL専用、あれば必須) 5.目標価格/乖離率 6.WATCH連続日数 7.理由 8.StockType
(最大2件)。この簡潔化はユーザー向けLINE通知本文にのみ適用し、内部データ・監査
ログの情報量は一切減らさない(呼び出し元は本モジュールを通知テキスト生成にのみ
使い、Recommendation自体は完全な情報を保持したまま保存すること)。

70文字は通常セグメント向けの目安であり、ユーザーアクション(PARTIALの売却数量等)
に不可欠な必須セグメントは70文字を超えても省略しない(soft limit)。
重大リスク(is_critical_risk)の場合も同様に理由情報を欠落させない。

必須セグメントより手前にある非必須セグメント(例: 現在値)だけで70文字を超えた
場合でも、必須セグメント(PARTIAL売却数量等)の評価・追加は打ち切らない
(再コードレビュー対応2026-08、指摘2再修正)。format_notification_text()内の
ループは、非必須セグメントが上限超過の場合そのセグメントのみをスキップし、
後続の必須セグメントの判定へ進む(以前は最初の超過セグメントでbreakしており、
それより後ろの必須セグメントが本文へ一切到達できない不具合があった)。
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
    # コードレビュー対応(2026-08、LINE通知/監査分離): URGENT_REVIEW/
    # URGENT_HOLDING_REVIEWはいずれも「緊急確認」であり、必ずしも売却判定を
    # 意味しないことを明確にする(旧「緊急」から変更)。
    NotificationCategory.CRITICAL_RISK: "緊急確認",
    NotificationCategory.WATCH: "監視",
    NotificationCategory.PARTIAL_SELL: "一部売却",
    NotificationCategory.MANUAL_REVIEW: "要確認",
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
    # target_priceの意味を示すラベル(コードレビュー対応2026-08、指摘3)。
    # Noneの場合は「打診」(BUY/NEAR BUYの打診買い価格、既定・後方互換)。
    # SELLでは「打診」は買い価格の表現であり売却価格には使わないため、
    # recommendation_adapter.py側で「即時執行」「見直し」等を明示的に設定する。
    target_price_label: str | None = None
    # 目安価格が構造上存在しない/算定不能な場合に、価格の代わりに表示する文言
    # (コードレビュー対応2026-08、LINE通知/監査分離)。例:「売却目安は算定保留」。
    # target_priceがNoneの場合のみ意味を持つ。他の任意セグメントと異なり、
    # 70文字上限でも欠落させない必須セグメントとして扱う(重大リスクのreasonと
    # 同様、「価格が取れたか算定保留か」はユーザーが必ず知るべき優先度4の情報)。
    target_price_withheld_label: str | None = None
    # 打診/通常のように、同じ判定内で2つ目の目安価格を併記する場合に使う
    # (コードレビュー対応2026-08)。reason文字列への埋め込みはしない。
    secondary_target_price: Decimal | None = None
    secondary_target_price_label: str | None = None
    # 「あと何%」の接近率(NEAR BUY等)。正の値。
    distance_pct: Decimal | None = None
    consecutive_business_days: int | None = None
    # PAUSED後リセット(§5-3): 評価不能を挟んで連続日数が1へリセットされた
    # 直後であることを示す(「◯日連続」ではなく「監視再開」相当の文言にする)。
    is_resumed_after_gap: bool = False
    is_watch_end: bool = False
    reason: str | None = None
    stock_types: list[StockType] = field(default_factory=list)
    # --- 通知簡潔化・WATCH状態遷移伝播のコードレビュー対応(2026-08)で追加 ---
    # ラベルを既定のカテゴリ表示(例: category=BUYなら「買い」)から差し替える。
    # NEAR BUY監視からBUYへ昇格した通知を「到達」と表示するために使う。
    # is_watch_end=Trueの場合は既存どおり「監視終了」が優先される。
    label_override: str | None = None
    # WatchStateから昇格した場合の「何営業日監視した後にBUYへ到達したか」。
    # Noneでない場合、consecutive_business_days/distance_pct由来のセグメントより
    # 優先して「{N}日監視後」を表示する。
    promoted_from_watch_days: int | None = None
    # WATCH終了通知(is_watch_end=True)専用の「何営業日連続で監視していたか」。
    # 通常の継続監視(「N日連続」)と紛らわしくないよう「N日継続」と表示する。
    watch_end_days: int | None = None
    # PARTIAL売却の推奨株数(コードレビュー対応2026-08、指摘3)。
    # Noneの場合はセグメント自体を生成しない。Noneでなく>=0の値。
    suggested_sell_shares: int | None = None
    # suggested_sell_sharesに対応する比率(0.0-1.0)。表示時は「300株(60%)」等で使う。
    # Noneの場合は比率を併記しない。suggested_sell_sharesがNoneでない場合の比率は、
    # Recommendation生成時点で数量と整合していることを保証すること(通知層では
    # 独立に再計算せず、保存済みのratio値を信頼)。
    suggested_sell_ratio: float | None = None


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
    label = _WATCH_END_LABEL if data.is_watch_end else (
        data.label_override or _CATEGORY_LABELS.get(data.category, "通知")
    )

    # 優先度1・2は必須(削れない)。
    required = f"{label} {data.stock_code} {data.stock_name}"

    price_label = data.target_price_label or "打診"
    # (segment_text, required)のリスト。requiredなセグメントはmax_charsを
    # 超えても欠落させない(コードレビュー対応2026-08、LINE通知/監査分離:
    # 「価格が取れたか、算定保留か」はユーザーが必ず知るべき優先度4の情報のため)。
    optional_segments: list[tuple[str, bool]] = []  # 優先度の高い順
    if data.current_price is not None:
        optional_segments.append((_fmt_price(data.current_price), False))
    if data.suggested_sell_shares is not None:
        ratio_pct = (
            data.suggested_sell_ratio * 100 if data.suggested_sell_ratio is not None else None
        )
        if ratio_pct is not None:
            optional_segments.append(
                (f"{data.suggested_sell_shares}株({ratio_pct:.0f}%)", True)
            )
        else:
            optional_segments.append((f"{data.suggested_sell_shares}株", True))
    if data.target_price is not None and data.distance_pct is not None:
        optional_segments.append(
            (f"{price_label}{_fmt_price(data.target_price)}まで{data.distance_pct:.1f}%", False)
        )
    elif data.target_price is not None:
        optional_segments.append((f"{price_label}{_fmt_price(data.target_price)}", False))
    elif data.distance_pct is not None:
        optional_segments.append((f"あと{data.distance_pct:.1f}%", False))
    elif data.target_price_withheld_label is not None:
        optional_segments.append((data.target_price_withheld_label, True))
    if data.target_price is not None and data.secondary_target_price is not None:
        secondary_label = data.secondary_target_price_label or "目安"
        optional_segments.append(
            (f"{secondary_label}{_fmt_price(data.secondary_target_price)}", False)
        )
    if data.is_resumed_after_gap:
        optional_segments.append(("監視再開", False))
    elif data.promoted_from_watch_days is not None:
        optional_segments.append((f"{data.promoted_from_watch_days}日監視後", False))
    elif data.is_watch_end and data.watch_end_days is not None:
        optional_segments.append((f"{data.watch_end_days}日継続", False))
    elif data.consecutive_business_days is not None:
        optional_segments.append((f"{data.consecutive_business_days}日連続", False))
    if data.reason:
        optional_segments.append((data.reason, False))
    type_label = _representative_stock_types(data.stock_types)
    if type_label:
        optional_segments.append((type_label, False))

    text = required
    for segment, is_required_segment in optional_segments:
        candidate = f"{text}｜{segment}" if text != required else f"{text}\n{segment}"
        # 重大リスク・requiredなセグメント(PARTIAL売却数量・算定保留の明示)は
        # max_charsを厳密な上限として扱わず欠落させない。それ以外は候補文字列が
        # 上限を超える場合そのセグメントのみ追加をスキップする(要求仕様の
        # soft limit)。
        #
        # 以前はここでbreakしており、必須セグメントより手前の非必須セグメント
        # (例: current_price)だけで上限を超えると、それ以降の全セグメント
        # (必須のPARTIAL売却数量を含む)が本文へ一切到達できなかった
        # (再コードレビュー対応2026-08、指摘2再修正)。breakをやめてスキップに
        # 変えることで、後続の必須セグメントの評価を打ち切らないようにする。
        if is_critical_risk or is_required_segment or len(candidate) <= max_chars:
            text = candidate
    return text
