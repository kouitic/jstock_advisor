"""Issue #605 / 605-B: 本日の投資アクションの CLI 用 renderer(表・JSON。V605-v1)。

**純粋な関数だけ**である。入力は 605-A の `TodayActionsView`、出力は文字列(表)と
dict / JSON 文字列。ネットワーク・ファイル・AWS・永続化・時計・LINE 送信に触れない。
現行の判定・通知・保存の挙動を変えず、どこからも import されない(dormant)。
CLI(605-D)・LINE renderer(605-C)は含まない。

## 表(V605-v1 の 4.1 節)

```
試験運用：投資判断には使いません       ← mode = SHADOW のとき常に先頭
本日の投資アクション  所有者: …  評価日時: … JST  評価モード: SHADOW
買付余力: …（最終棚卸し …）/ 未登録 / 最終棚卸し：未確認
本日の結論:                           ← 常に 2 行(U-17 = B 案)
  新しい提案（購入・資金入替・新分類の売却）: あり（…）/ なし / 算出不能（理由: …）
  既存の売却関連の判定: あり — 区分ごとの件数（理由は未分類）/ なし
■ 群の見出し + 『| 』で始まる行       ← リスク回避 → 割安解消 → 利益保全
                                         → 既存の判定 → 入替 → 購入
■ 算出不能の理由 + 『・』の行
見込み残高: …（目安）/ 保有銘柄に変更なし: N銘柄（--verbose のときだけ一覧）
```

・売却は『日本語名称（内部コード）』を併記する。数量が未確定なら
  『売却株数：未確定』(0 でも全株でもない)
・Rotation は売りと買いを 1 行に持つ。数値が未確定の間は
  『株数・金額・改善幅：未確定（試験運用）』
・金額は Decimal のまま整形する(float にしない)。日時は JST で表示する

## JSON(V605-v1 の 4.2 節)

キーは固定。金額・価格・比率は文字列(Decimal を float にしない)。コードと表示名は別の
field。未確定の数量・数値は null(0 にしない)。日本語の見出し・帯は含めない。
破壊的変更は schema_version を更新する。

## U-17 / U-17a(USER 決定済み。#605 …6102823176)

本日の結論は『新しい提案』と『既存の売却関連の判定』の 2 行に分ける。既存の判定がある日に
『見送り』だけを表示しない(『見送り』の語は結論に使わず、新しい提案が無い日は『なし』とする)。
既存の判定は区分ごとの件数を、既存の表記のまま出す(新しい Exit 分類へ自動変換しない)。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal

from jstock_advisor.domain.jst import format_jst, to_jst
from jstock_advisor.domain.notification.today_actions_view import (
    ROTATION_CONDITION_TEXT,
    SCHEMA_VERSION,
    BuyFinalAction,
    BuyItemView,
    CashView,
    LegacyDecisionItemView,
    PriceBasis,
    RemainingCashBasis,
    RotationItemView,
    RotationLeg,
    SellItemView,
    TodayActionsView,
    ViewMode,
    ViewReason,
    ViewStatus,
)

SHADOW_BANNER = "試験運用：投資判断には使いません"
JSON_SCHEMA_VERSION = SCHEMA_VERSION

_CONCLUSION_NEW_LABEL = "  新しい提案（購入・資金入替・新分類の売却）: "
_CONCLUSION_LEGACY_LABEL = "  既存の売却関連の判定: "
_H_RISK = "■ リスク回避の売却"
_H_VALUE = "■ 割安解消の売却"
_H_PROFIT = "■ 利益保全の売却"
_H_LEGACY = "■ 既存の売却関連の判定（売却理由は未分類）"
_H_ROTATION = f"■ 資金入替（売却と購入は一体。{ROTATION_CONDITION_TEXT}）"
_H_BUY = "■ 購入（最終アクション）"
_H_REASONS = "■ 算出不能の理由"

_FINAL_ACTION_LABELS: dict[BuyFinalAction, str] = {
    BuyFinalAction.PURCHASE: "購入",
    BuyFinalAction.SKIP: "見送り",
}
_SHARES_UNCONFIRMED = "売却株数：未確定"
_ROTATION_UNCONFIRMED = "株数・金額・改善幅：未確定（試験運用）"


# --- 表の整形 ---------------------------------------------------------------------------------


def _money(value: Decimal) -> str:
    return format(value, ",f")


def _name(code: str, name: str) -> str:
    return f"{name}({code})"


def _price_text(basis: PriceBasis | None) -> str:
    if basis is None:
        return "-"
    return f"{_money(basis.price)}円（{to_jst(basis.as_of):%Y-%m-%d %H:%M}）"


def _amount_text(amount: Decimal | None) -> str:
    return "-" if amount is None else f"約{_money(amount)}円"


def _sell_shares_text(shares: int | None) -> str:
    return _SHARES_UNCONFIRMED if shares is None else f"{shares}株"


def _cash_line(cash: CashView) -> str:
    # 確認済みの買付余力は棚卸し日時を持ち、未確認は持たない(605-A の型が構築時に保証する)。
    # 日時の有無で分けると、型の絞り込みになり、到達しない分岐を作らない
    last = cash.last_reconciled_at
    reconciled = f"最終棚卸し {to_jst(last):%Y-%m-%d}" if last is not None else "最終棚卸し：未確認"
    amount = "未登録" if cash.amount is None else f"{_money(cash.amount)}円"
    return f"買付余力: {amount}（{reconciled}）"


def _proposal_counts(view: TodayActionsView) -> tuple[int, int, int]:
    sells = len(view.risk_exit_items) + len(view.value_exit_items)
    sells += len(view.profit_protection_items)
    purchases = sum(1 for b in view.buy_items if b.final_action is BuyFinalAction.PURCHASE)
    return sells, len(view.rotation_items), purchases


def _new_proposal_text(view: TodayActionsView) -> str:
    if view.status is ViewStatus.ACTIONS:
        sells, rotations, purchases = _proposal_counts(view)
        return f"あり（売却{sells}件・資金入替{rotations}件・購入{purchases}件）"
    if view.status is ViewStatus.NO_ACTION:
        return "なし"
    labels = "・".join(reason.label for reason in view.reasons)
    return f"算出不能（理由: {labels}）"


def _legacy_state_text(view: TodayActionsView) -> str:
    """既存の判定を区分ごとの件数で示す。区分名は既存の表記のまま(変換しない)。"""
    if not view.legacy_items:
        return "なし"
    counts: dict[str, int] = {}
    for item in view.legacy_items:
        counts[item.legacy_label] = counts.get(item.legacy_label, 0) + 1
    parts = "・".join(f"{label}{count}件" for label, count in counts.items())
    return f"あり — {parts}（理由は未分類）"


def _sell_row(item: SellItemView) -> str:
    cells = [
        _name(item.stock_code, item.stock_name),
        f"{item.sell_action.label}（{item.sell_action.value}）",
        f"{item.exit_reason.label}（{item.exit_reason.value}）",
        _sell_shares_text(item.shares),
        _amount_text(item.estimated_amount),
        _price_text(item.price_basis),
        "・".join(item.summary_reasons) or "-",
    ]
    return _row(cells)


def _legacy_row(item: LegacyDecisionItemView) -> str:
    cells = [
        _name(item.stock_code, item.stock_name),
        item.legacy_label,
        _sell_shares_text(item.shares),
        item.note or "-",
    ]
    return _row(cells)


def _leg_text(leg: RotationLeg, *, with_numbers: bool) -> str:
    text = _name(leg.stock_code, leg.stock_name)
    if with_numbers and leg.shares is not None and leg.estimated_amount is not None:
        text += f" {leg.shares}株 {_amount_text(leg.estimated_amount)}"
    return text


def _rotation_row(item: RotationItemView) -> str:
    # 確定済みの Rotation だけが改善幅と算入割合を持つ(605-A の型が構築時に保証する)。
    # 値の有無で分けると、型の絞り込みになり、到達しない分岐を作らない。未確定は数値を出さない
    improvement = item.improvement
    ratio = item.counted_ratio
    if improvement is not None and ratio is not None:
        confirmed = True
        detail = f"改善幅 {improvement:f}・算入割合 {ratio:f}"
    else:
        confirmed = False
        detail = _ROTATION_UNCONFIRMED
    return _row(
        [
            f"売却: {_leg_text(item.sell_leg, with_numbers=confirmed)}",
            f"購入: {_leg_text(item.buy_leg, with_numbers=confirmed)}",
            detail,
        ]
    )


def _buy_row(item: BuyItemView) -> str:
    if item.final_action is BuyFinalAction.SKIP:
        tail = f"{item.skip_reason_label}（{item.skip_reason_code}）"
        shares = amount = price = "-"
    else:
        tail = f"段階: {item.price_tier}" if item.price_tier else "-"
        shares = "-" if item.shares is None else f"{item.shares}株"
        amount = _amount_text(item.estimated_amount)
        price = _price_text(item.price_basis)
    return _row(
        [
            _name(item.stock_code, item.stock_name),
            item.standalone_judgement,
            _FINAL_ACTION_LABELS[item.final_action],
            shares,
            amount,
            price,
            tail,
        ]
    )


def _row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _remaining_cash_line(view: TodayActionsView) -> str | None:
    if view.projected_remaining_cash is None:
        return None
    suffix = (
        "（未確認の残高に基づく目安）"
        if view.remaining_cash_basis is RemainingCashBasis.ESTIMATE_FROM_UNCONFIRMED_CASH
        else "（目安）"
    )
    return f"見込み残高: {_money(view.projected_remaining_cash)}円{suffix}"


def render_today_actions_table(
    view: TodayActionsView,
    *,
    verbose: bool = False,
    unchanged_holding_names: Sequence[str] | None = None,
) -> str:
    """表(CLI 用)。群ごとに見出しと『| 』で始まる行を並べる。空の群は出さない。"""
    lines: list[str] = []
    if view.mode is ViewMode.SHADOW:
        lines.append(SHADOW_BANNER)
    lines.append(
        f"本日の投資アクション  所有者: {view.owner}  評価日時: {format_jst(view.evaluated_at)}"
        f"  評価モード: {view.mode.value}"
    )
    lines.append(_cash_line(view.cash))
    lines.append("本日の結論:")
    lines.append(f"{_CONCLUSION_NEW_LABEL}{_new_proposal_text(view)}")
    lines.append(f"{_CONCLUSION_LEGACY_LABEL}{_legacy_state_text(view)}")
    groups: tuple[tuple[str, list[str]], ...] = (
        (_H_RISK, [_sell_row(i) for i in view.risk_exit_items]),
        (_H_VALUE, [_sell_row(i) for i in view.value_exit_items]),
        (_H_PROFIT, [_sell_row(i) for i in view.profit_protection_items]),
        (_H_LEGACY, [_legacy_row(i) for i in view.legacy_items]),
        (_H_ROTATION, [_rotation_row(i) for i in view.rotation_items]),
        (_H_BUY, [_buy_row(i) for i in view.buy_items]),
    )
    for header, group_rows in groups:
        if group_rows:
            lines.append(header)
            lines.extend(group_rows)
    if view.reasons:
        lines.append(_H_REASONS)
        lines.extend(f"・{reason.label}（{reason.value}）" for reason in view.reasons)
    remaining = _remaining_cash_line(view)
    if remaining is not None:
        lines.append(remaining)
    if view.unchanged_holdings_count is not None:
        lines.append(f"保有銘柄に変更なし: {view.unchanged_holdings_count}銘柄")
        if verbose and unchanged_holding_names:
            lines.extend(f"  ・{name}" for name in unchanged_holding_names)
    return "\n".join(lines)


# --- JSON -------------------------------------------------------------------------------------


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _coded(code: str, label: str) -> dict[str, object]:
    return {"code": code, "label": label}


def _basis_json(basis: PriceBasis | None) -> dict[str, object] | None:
    if basis is None:
        return None
    return {"price": _dec(basis.price), "as_of": to_jst(basis.as_of).isoformat()}


def _reasons_json(reasons: Sequence[ViewReason]) -> list[dict[str, object]]:
    return [_coded(reason.value, reason.label) for reason in reasons]


def _sell_json(item: SellItemView) -> dict[str, object]:
    return {
        "stock_code": item.stock_code,
        "stock_name": item.stock_name,
        "sell_action": _coded(item.sell_action.value, item.sell_action.label),
        "exit_reason": _coded(item.exit_reason.value, item.exit_reason.label),
        "shares": item.shares,
        "estimated_amount": _dec(item.estimated_amount),
        "price_basis": _basis_json(item.price_basis),
        "summary_reasons": list(item.summary_reasons),
    }


def _legacy_json(item: LegacyDecisionItemView) -> dict[str, object]:
    return {
        "stock_code": item.stock_code,
        "stock_name": item.stock_name,
        "legacy_label": item.legacy_label,
        "shares": item.shares,
        "note": item.note,
    }


def _leg_json(leg: RotationLeg) -> dict[str, object]:
    return {
        "stock_code": leg.stock_code,
        "stock_name": leg.stock_name,
        "shares": leg.shares,
        "estimated_amount": _dec(leg.estimated_amount),
        "price_basis": _basis_json(leg.price_basis),
    }


def _rotation_json(item: RotationItemView) -> dict[str, object]:
    return {
        "sell_leg": _leg_json(item.sell_leg),
        "buy_leg": _leg_json(item.buy_leg),
        "numbers_confirmed": item.numbers_confirmed,
        "improvement": _dec(item.improvement),
        "counted_ratio": _dec(item.counted_ratio),
        "condition_text": item.condition_text,
    }


def _buy_json(item: BuyItemView) -> dict[str, object]:
    skip_reason: dict[str, object] | None = None
    if item.skip_reason_code is not None and item.skip_reason_label is not None:
        skip_reason = _coded(item.skip_reason_code, item.skip_reason_label)
    return {
        "stock_code": item.stock_code,
        "stock_name": item.stock_name,
        "standalone_judgement": item.standalone_judgement,
        "final_action": _coded(item.final_action.value, _FINAL_ACTION_LABELS[item.final_action]),
        "skip_reason": skip_reason,
        "shares": item.shares,
        "estimated_amount": _dec(item.estimated_amount),
        "price_basis": _basis_json(item.price_basis),
        "price_tier": item.price_tier,
    }


def today_actions_to_json_dict(view: TodayActionsView) -> dict[str, object]:
    """V605-v1 の dict。キーは固定。金額は文字列、未確定は null。"""
    cash = view.cash
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "mode": view.mode.value,
        "evaluated_at": to_jst(view.evaluated_at).isoformat(),
        "owner": view.owner,
        "status": view.status.value,
        "reasons": _reasons_json(view.reasons),
        "contract_versions": dict(view.contract_versions),
        "model_versions": list(view.model_versions),
        "comparison_keys": list(view.comparison_keys),
        "rotation_evaluated": view.rotation_evaluated,
        "cash": {
            "amount": _dec(cash.amount),
            "registered": cash.registered,
            "last_reconciled_at": (
                None
                if cash.last_reconciled_at is None
                else to_jst(cash.last_reconciled_at).isoformat()
            ),
            "reconciliation_state": cash.reconciliation_state.value,
        },
        "sections": {
            "risk_exit": [_sell_json(i) for i in view.risk_exit_items],
            "value_exit": [_sell_json(i) for i in view.value_exit_items],
            "profit_protection": [_sell_json(i) for i in view.profit_protection_items],
            "legacy": [_legacy_json(i) for i in view.legacy_items],
            "rotation": [_rotation_json(i) for i in view.rotation_items],
            "buy": [_buy_json(i) for i in view.buy_items],
        },
        "projected_remaining_cash": _dec(view.projected_remaining_cash),
        "remaining_cash_basis": (
            None if view.remaining_cash_basis is None else view.remaining_cash_basis.value
        ),
        "unchanged_holdings_count": view.unchanged_holdings_count,
    }


def render_today_actions_json(view: TodayActionsView) -> str:
    """V605-v1 の JSON 文字列(日本語はそのまま。インデント 2)。"""
    return json.dumps(today_actions_to_json_dict(view), ensure_ascii=False, indent=2)
