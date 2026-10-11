"""Issue #605 / 605-C: 本日の投資アクションの LINE renderer(情報欠落なしの分割つき。4.3 節)。

**純粋な関数だけ**である。入力は 605-A の `TodayActionsView`、出力は LINE の本文(1 通 = 1 要素)の
tuple。**送信はしない**。ネットワーク・ファイル・AWS・永続化・時計に触れず、送信の呼び出し元を
作らない(U-16)。renderer の存在を LINE 送信・ACTIVE 化の承認と解釈しない。どこからも import
されない(dormant)。現行の【保有株チェック完了】【買い候補分析完了】・line_notification_service・
infrastructure/line は変更しない。

## 組み立て

```
各通   【本日の投資アクション】<owner> (i/N)      ← 先頭。N は詰めた後に確定(桁数は最大幅を先に確保)
       試験運用：投資判断には使いません            ← mode = SHADOW のとき全通に付ける
1 通目 見出し行 → 件数の行(群ごとの実数・通数)→ 買付余力 → 本日の結論(2 行。605-B と同じ語彙)
以降   ■ 群の見出し + 項目(リスク回避 → 割安解消 → 利益保全 → 既存の判定 → 資金入替 → 購入)
       ■ 算出不能の理由
最後   見込み残高 / 保有銘柄に変更なし: N銘柄
```

## 分割(U-11)

・1 項目(銘柄 1 件 = 数行)は分割しない。次の項目が予算を超えるなら次の通へ送る
・群の順序は絶対に入れ替えない。群が通をまたぐときは、次の通の見出しに『（続き）』を付ける
・『ほか』『他N件』『…など』のような省略を一切出さない(重要な売却・リスク情報を落とさない)
・1 項目が予算を超える異常時だけ、行の境界(長い行は文字数)で分け、『（続く）』を付ける。
  項目の識別(『・』の行)は最初の断片にだけあり、重複して数えられない

予算は LINE_TEXT_CHAR_BUDGET(アプリケーション側の安全予算。protocol の上限 5000 に対する余裕)。
domain は services / infrastructure を import できないため値を domain 側に持ち、同値であることは
契約テストで固定する。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from jstock_advisor.domain.notification.today_actions_cli_renderer import (
    FINAL_ACTION_LABELS,
    HEADER_BUY,
    HEADER_LEGACY,
    HEADER_PROFIT,
    HEADER_REASONS,
    HEADER_RISK,
    HEADER_ROTATION,
    HEADER_VALUE,
    ROTATION_UNCONFIRMED_TEXT,
    SHADOW_BANNER,
    cash_line,
    conclusion_lines,
    format_amount,
    format_price_basis,
    format_sell_shares,
    heading_line,
    remaining_cash_line,
)
from jstock_advisor.domain.notification.today_actions_view import (
    BuyFinalAction,
    BuyItemView,
    LegacyDecisionItemView,
    RotationItemView,
    RotationLeg,
    SellItemView,
    TodayActionsView,
    ViewMode,
)

LINE_TEXT_CHAR_BUDGET = 4500
MIN_BUDGET = 200
_MIN_CAPACITY = 40
_CONTINUED = "（続き）"
_CONTINUES_MARK = "  （続く）"
_WORST_NUMBER = 9999
_INDENT = "  "


@dataclass(frozen=True)
class _Unit:
    """分割の最小単位。section があるときは、その群の見出しの下に置かれる。"""

    section: str | None
    lines: tuple[str, ...]


def _title(owner: str, index: int, total: int) -> str:
    return f"【本日の投資アクション】{owner} ({index}/{total})"


# --- 項目の整形 -------------------------------------------------------------------------------


def _quantity_text(shares: int | None, amount_text: str | None, price_text: str | None) -> str:
    parts = [f"{shares}株"] if shares is not None else []
    if amount_text is not None:
        parts.append(amount_text)
    if price_text is not None:
        parts.append(f"価格 {price_text}")
    return " ".join(parts)


def _name(code: str, name: str) -> str:
    return f"{name}({code})"


def _sell_unit(item: SellItemView) -> _Unit:
    lines = [
        f"・{_name(item.stock_code, item.stock_name)}",
        f"{_INDENT}{item.sell_action.label}（{item.sell_action.value}）"
        f"／{item.exit_reason.label}（{item.exit_reason.value}）",
    ]
    if item.shares is None:
        lines.append(f"{_INDENT}{format_sell_shares(None)}")
    else:
        amount = None if item.estimated_amount is None else format_amount(item.estimated_amount)
        price = None if item.price_basis is None else format_price_basis(item.price_basis)
        lines.append(f"{_INDENT}{_quantity_text(item.shares, amount, price)}")
    if item.summary_reasons:
        lines.append(f"{_INDENT}要点: {'・'.join(item.summary_reasons)}")
    return _Unit(section=None, lines=tuple(lines))


def _legacy_unit(item: LegacyDecisionItemView) -> _Unit:
    lines = [
        f"・{_name(item.stock_code, item.stock_name)}",
        f"{_INDENT}{item.legacy_label}／{format_sell_shares(item.shares)}",
    ]
    if item.note:
        lines.append(f"{_INDENT}{item.note}")
    return _Unit(section=HEADER_LEGACY, lines=tuple(lines))


def _leg_text(leg: RotationLeg) -> str:
    amount = None if leg.estimated_amount is None else format_amount(leg.estimated_amount)
    return _quantity_text(leg.shares, amount, None)


def _rotation_unit(item: RotationItemView) -> _Unit:
    first = (
        f"・売却: {_name(item.sell_leg.stock_code, item.sell_leg.stock_name)}"
        f" → 購入: {_name(item.buy_leg.stock_code, item.buy_leg.stock_name)}"
    )
    # 確定済みの Rotation だけが改善幅と算入割合を持つ(605-A の型が構築時に保証する)
    improvement = item.improvement
    ratio = item.counted_ratio
    if improvement is not None and ratio is not None:
        detail = (
            f"売却 {_leg_text(item.sell_leg)}／購入 {_leg_text(item.buy_leg)}"
            f"／改善幅 {improvement:f}・算入割合 {ratio:f}"
        )
    else:
        detail = ROTATION_UNCONFIRMED_TEXT
    return _Unit(section=HEADER_ROTATION, lines=(first, f"{_INDENT}{detail}"))


def _buy_unit(item: BuyItemView) -> _Unit:
    lines = [
        f"・{_name(item.stock_code, item.stock_name)}",
        f"{_INDENT}銘柄単独の判断: {item.standalone_judgement}"
        f"／最終アクション: {FINAL_ACTION_LABELS[item.final_action]}",
    ]
    if item.final_action is BuyFinalAction.SKIP:
        lines.append(f"{_INDENT}理由: {item.skip_reason_label}（{item.skip_reason_code}）")
    else:
        amount = None if item.estimated_amount is None else format_amount(item.estimated_amount)
        price = None if item.price_basis is None else format_price_basis(item.price_basis)
        detail = _quantity_text(item.shares, amount, price)
        if item.price_tier:
            detail += f" 段階: {item.price_tier}"
        lines.append(f"{_INDENT}{detail}")
    return _Unit(section=HEADER_BUY, lines=tuple(lines))


# --- 単位の並び -------------------------------------------------------------------------------


def _count_line(view: TodayActionsView, total: int) -> str:
    purchases = sum(1 for b in view.buy_items if b.final_action is BuyFinalAction.PURCHASE)
    return (
        f"件数: リスク回避{len(view.risk_exit_items)}・割安解消{len(view.value_exit_items)}"
        f"・利益保全{len(view.profit_protection_items)}・既存の判定{len(view.legacy_items)}"
        f"・資金入替{len(view.rotation_items)}・購入欄{len(view.buy_items)}（うち購入{purchases}）"
        f"・通数{total}"
    )


def _units(view: TodayActionsView) -> list[_Unit]:
    units = [
        _Unit(None, (heading_line(view),)),
        _Unit(None, (_count_line(view, _WORST_NUMBER),)),
        _Unit(None, (cash_line(view.cash),)),
        _Unit(None, conclusion_lines(view)),
    ]
    groups: tuple[tuple[str, list[_Unit]], ...] = (
        (HEADER_RISK, [_sell_unit(i) for i in view.risk_exit_items]),
        (HEADER_VALUE, [_sell_unit(i) for i in view.value_exit_items]),
        (HEADER_PROFIT, [_sell_unit(i) for i in view.profit_protection_items]),
        (HEADER_LEGACY, [_legacy_unit(i) for i in view.legacy_items]),
        (HEADER_ROTATION, [_rotation_unit(i) for i in view.rotation_items]),
        (HEADER_BUY, [_buy_unit(i) for i in view.buy_items]),
    )
    for header, group_units in groups:
        units.extend(_Unit(header, unit.lines) for unit in group_units)
    units.extend(_Unit(HEADER_REASONS, (f"・{r.label}（{r.value}）",)) for r in view.reasons)
    remaining = remaining_cash_line(view)
    if remaining is not None:
        units.append(_Unit(None, (remaining,)))
    if view.unchanged_holdings_count is not None:
        units.append(_Unit(None, (f"保有銘柄に変更なし: {view.unchanged_holdings_count}銘柄",)))
    return units


# --- 詰め込み ---------------------------------------------------------------------------------


def _cost(lines: Sequence[str]) -> int:
    return sum(len(line) + 1 for line in lines)


def _split_long_line(line: str, limit: int) -> list[str]:
    """1 行が limit を超えるときだけ文字数で分ける。2 本目以降は項目の続きとしてインデントする。"""
    if len(line) <= limit:
        return [line]
    pieces = [line[:limit]]
    rest = line[limit:]
    step = limit - len(_INDENT)
    while rest:
        pieces.append(_INDENT + rest[:step])
        rest = rest[step:]
    return pieces


class _Packer:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.messages: list[list[str]] = [[]]
        self.used = 0
        self.headers_here: set[str] = set()
        self.headers_seen: set[str] = set()

    def _new_message(self) -> None:
        self.messages.append([])
        self.used = 0
        self.headers_here = set()

    def _header_line(self, section: str | None) -> str | None:
        if not section or section in self.headers_here:
            return None
        return section + _CONTINUED if section in self.headers_seen else section

    def _put(self, section: str | None, lines: Sequence[str]) -> None:
        header = self._header_line(section)
        if header is not None:
            self.messages[-1].append(header)
            self.used += len(header) + 1
        if section:
            self.headers_here.add(section)
            self.headers_seen.add(section)
        self.messages[-1].extend(lines)
        self.used += _cost(lines)

    def add(self, unit: _Unit) -> None:
        header = self._header_line(unit.section)
        cost = _cost(unit.lines) + (0 if header is None else len(header) + 1)
        if self.used + cost <= self.capacity:
            self._put(unit.section, unit.lines)
            return
        if self.messages[-1]:
            self._new_message()
            header = self._header_line(unit.section)
            cost = _cost(unit.lines) + (0 if header is None else len(header) + 1)
            if self.used + cost <= self.capacity:
                self._put(unit.section, unit.lines)
                return
        self._add_oversized(unit)

    def _add_oversized(self, unit: _Unit) -> None:
        """1 項目が予算を超える異常時: 行の境界(長い行は文字数)で分け、『（続く）』を付ける。"""
        section = unit.section
        worst_header = (len(section) + len(_CONTINUED) + 1) if section else 0
        room = self.capacity - worst_header - len(_CONTINUES_MARK) - 1
        lines: list[str] = []
        for line in unit.lines:
            lines.extend(_split_long_line(line, room - 1))
        chunks: list[list[str]] = [[]]
        used = 0
        for line in lines:
            if used + len(line) + 1 > room and chunks[-1]:
                chunks.append([])
                used = 0
            chunks[-1].append(line)
            used += len(line) + 1
        for index, chunk in enumerate(chunks):
            if index > 0:
                self._new_message()
            last = index == len(chunks) - 1
            self._put(section, chunk if last else [*chunk, _CONTINUES_MARK])


# --- 公開関数 ---------------------------------------------------------------------------------


def render_today_actions_line_messages(
    view: TodayActionsView, *, budget: int = LINE_TEXT_CHAR_BUDGET
) -> tuple[str, ...]:
    """LINE の本文を、予算以下の複数通に分けて返す(送信はしない)。"""
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < MIN_BUDGET:
        raise ValueError("budget must be an int of at least the minimum")
    banner = SHADOW_BANNER if view.mode is ViewMode.SHADOW else None
    worst_title = _title(view.owner, _WORST_NUMBER, _WORST_NUMBER)
    overhead = len(worst_title) + (0 if banner is None else len(banner) + 1)
    capacity = budget - overhead
    if capacity < _MIN_CAPACITY:
        raise ValueError("budget is too small for the title of this owner")

    packer = _Packer(capacity)
    for unit in _units(view):
        packer.add(unit)

    total = len(packer.messages)
    placeholder = _count_line(view, _WORST_NUMBER)
    actual = _count_line(view, total)
    out: list[str] = []
    for index, body in enumerate(packer.messages, start=1):
        head = [_title(view.owner, index, total)]
        if banner is not None:
            head.append(banner)
        lines = [actual if line == placeholder else line for line in body]
        out.append("\n".join(head + lines))
    return tuple(out)


__all__ = ["LINE_TEXT_CHAR_BUDGET", "MIN_BUDGET", "render_today_actions_line_messages"]
