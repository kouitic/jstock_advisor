"""購入側 Shadow の画面モデル → 表の文字列 / JSON(Issue #605 最小 CLI。純粋・決定的)。

`allocation_shadow_cli_view.AllocationShadowScreen` を文字列にするだけ(計算・補完をしない)。

* 表(`render_table`): 日本語の見出し・理由(コード + 日本語)。★ 既定でラベル(表示名)を出す ★ ので、
  ラベルが 1 件でも出たときは末尾に『この表には表示名が含まれます』の注記を付ける(共有の注意)
* JSON(`render_json`): ★ 既定ではラベルを出さない(符号だけ)★。`with_labels=True` のときだけ
  表示名を含める(共有されうる出力に実名を載せない)。キーの追加は版を上げない追加のみ(V605-v1 の方針)
* 金額は文字列のまま(float にしない)。決定的(同じ画面は同じ出力)
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any, Final

from jstock_advisor.domain.notification.allocation_shadow_cli_view import (
    DAY_STATE_TEXT,
    UNKNOWN_TEXT,
    AllocationShadowScreen,
    CandidateRow,
    DayState,
    OwnerSection,
    OwnerStatus,
    Reason,
)

OUTPUT_SCHEMA_VERSION: Final = 1
DISCLAIMER: Final = "試験運用:投資判断には使いません(資金配分 Shadow。LINE には送られません)"
LABEL_NOTICE: Final = (
    "※ この表には表示名(ラベル)が含まれます。共有するときは --format json を使うか、"
    "表示名を伏せてください"
)
_DASH: Final = "-"


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, *, right: bool = False) -> str:
    gap = max(0, width - _width(text))
    return (" " * gap + text) if right else (text + " " * gap)


def _reason_text(reason: Reason | None) -> str:
    if reason is None:
        return _DASH
    return reason.text if reason.code == reason.text else f"{reason.text}({reason.code})"


def _opt(value: str | None) -> str:
    return _DASH if value is None else value


def _owner_title(owner: OwnerSection, *, show_labels: bool) -> str:
    if show_labels and owner.label:
        return f"{owner.label}({owner.display_ref})"
    return owner.display_ref


_ROW_HEADER: Final = (
    "順位",
    "銘柄",
    "銘柄単独の判断",
    "RAER",
    "結果",
    "推奨株数",
    "必要資金",
    "残余資金",
    "理由",
)
_RIGHT_ALIGNED: Final = frozenset({0, 3, 5, 6, 7})


def _row_cells(row: CandidateRow) -> tuple[str, ...]:
    if not row.readable:
        return (_DASH, UNKNOWN_TEXT, _DASH, _DASH, UNKNOWN_TEXT, _DASH, _DASH, _DASH, UNKNOWN_TEXT)
    raer = _opt(row.raer_value)
    if row.raer_state is not None:
        raer = (
            f"{raer} {row.raer_state.text}" if row.raer_value is not None else row.raer_state.text
        )
    funds = _opt(row.required_funds)
    if row.shortfall is not None:
        funds = f"{funds}(不足 {row.shortfall})"
    return (
        _opt(row.rank),
        row.stock_code,
        row.buy_action.text if row.buy_action else _DASH,
        raer,
        row.decision.text if row.decision else _DASH,
        _opt(row.shares),
        funds,
        _opt(row.remaining_cash),
        _reason_text(row.reason),
    )


def _table(rows: tuple[CandidateRow, ...]) -> list[str]:
    cells = [_ROW_HEADER] + [_row_cells(row) for row in rows]
    widths = [max(_width(line[i]) for line in cells) for i in range(len(_ROW_HEADER))]
    lines: list[str] = []
    for n, line in enumerate(cells):
        padded = [
            _pad(cell, widths[i], right=i in _RIGHT_ALIGNED and n > 0)
            for i, cell in enumerate(line)
        ]
        lines.append("| " + " | ".join(padded) + " |")
        if n == 0:
            lines.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    return lines


def _owner_lines(owner: OwnerSection, *, show_labels: bool) -> list[str]:
    title = _owner_title(owner, show_labels=show_labels)
    lines = [f"■ 所有者: {title}"]
    if owner.status is OwnerStatus.MISSING:
        return [*lines, "  ★ 索引にありますが、記録が見つかりません(再実行・予算切れの可能性)"]
    if owner.status is OwnerStatus.UNREADABLE:
        return [*lines, f"  ★ 記録が読めません({UNKNOWN_TEXT})"]
    if owner.status is OwnerStatus.UNSUPPORTED:
        return [*lines, "  ★ 対応していない記録の版です"]
    if owner.status is OwnerStatus.REFUSED:
        return [*lines, "  ★ 符号が鍵つきの形ではないため、読みません"]
    lines.append(
        "  結果: "
        f"{owner.outcome.text if owner.outcome else _DASH}"
        f" / {owner.run_status.text if owner.run_status else _DASH}"
    )
    if owner.skip_reason is not None:
        lines.append(f"  計算しなかった理由: {_reason_text(owner.skip_reason)}")
    if owner.unavailable_reason is not None:
        lines.append(f"  算出不能の理由: {_reason_text(owner.unavailable_reason)}")
    cash = _opt(owner.available_cash)
    if owner.last_reconciled_at:
        cash = f"{cash}(最終棚卸し {owner.last_reconciled_at})"
    lines.append(f"  買付余力: {cash}   ポートフォリオ評価額: {_opt(owner.portfolio_value)}")
    if owner.model_version or owner.policy_id or owner.contract_version:
        lines.append(
            f"  RAER の版: {_opt(owner.model_version)}(暫定式)  "
            f"方針: {_opt(owner.policy_id)}  契約: {_opt(owner.contract_version)}  "
            f"評価モード: {_opt(owner.shadow_mode)}"
        )
    if owner.allocated_count is not None:
        lines.append(
            f"  結論: 購入 {owner.allocated_count} 件 / "
            f"購入しない候補 {owner.not_allocated_count} 件"
            f"   見込み残高(目安): {_opt(owner.expected_remaining_cash)}"
        )
    if owner.incomplete_reasons:
        joined = "、".join(_reason_text(r) for r in owner.incomplete_reasons)
        lines.append(f"  入力が完全でない理由: {joined}")
    if owner.exclusions:
        joined = "、".join(f"{_reason_text(r)} {n} 件" for r, n in owner.exclusions)
        lines.append(f"  配分の対象から除いた候補: {joined}")
    if owner.rows:
        lines.append("  ■ 購入候補(RAER の高い順)")
        lines.extend("  " + line for line in _table(owner.rows))
    if owner.candidates_truncated:
        lines.append(
            f"  ※ 記録の上限を超えたため、{owner.candidates_truncated} 件の候補は省かれています"
        )
    if owner.unparsed:
        lines.append(f"  ★ 解釈できなかった項目が {owner.unparsed} 件あります({UNKNOWN_TEXT})")
    return lines


def _index_lines(screen: AllocationShadowScreen) -> list[str]:
    index = screen.index
    if index is None:
        return []
    lines = [
        "索引: 記録済み "
        f"{_opt(index.recorded)} 件 / 未記録 {_opt(index.unrecorded)} 件 / "
        f"試行 {_opt(index.attempted)} 件"
        f"(鍵の版 {_opt(index.key_version)})"
    ]
    if index.unrecorded_reasons:
        joined = "、".join(f"{_reason_text(r)} {n} 件" for r, n in index.unrecorded_reasons)
        lines.append(f"  未記録の理由: {joined}")
    if screen.state is DayState.PARTIAL:
        lines.append("  ※ 再実行で後から追加された記録は、先着の索引には載りません")
    if screen.over_cap_refs:
        lines.append(f"  ※ 索引の上限を超えた符号 {screen.over_cap_refs} 件は読んでいません")
    return lines


def _skip_lines(screen: AllocationShadowScreen) -> list[str]:
    skip = screen.skip
    if skip is None:
        return []
    parts = [f"日単位の skip: {_reason_text(skip.reason)}"]
    if skip.key_unavailable_reason is not None:  # 鍵の理由は reason = KEY_UNAVAILABLE のときだけ
        parts.append(f"鍵の取得の失敗: {_reason_text(skip.key_unavailable_reason)}")
    if skip.unrecorded_owner_count is not None:  # owner が分かる日だけ
        parts.append(f"記録できなかった所有者 {skip.unrecorded_owner_count} 件")
    return [" / ".join(parts)]


def render_table(screen: AllocationShadowScreen, *, show_labels: bool = True) -> str:
    lines = [
        DISCLAIMER,
        f"資金配分 Shadow  対象日: {screen.business_date}",
        f"状況: {DAY_STATE_TEXT[screen.state]}",
    ]
    if screen.schema_version_seen is not None and screen.state is DayState.UNSUPPORTED:
        lines.append(f"  (schema_version = {screen.schema_version_seen})")
    lines.extend(_skip_lines(screen))
    lines.extend(_index_lines(screen))
    if screen.missing_refs:
        lines.append(f"★ 索引にあるのに記録が無い符号: {len(screen.missing_refs)} 件")
        lines.extend(f"    {_short(ref)}" for ref in screen.missing_refs)
    for owner in screen.owners:
        lines.append("")
        lines.extend(_owner_lines(owner, show_labels=show_labels))
    if screen.unparsed:
        lines.append("")
        lines.append(f"★ 解釈できなかった項目: 合計 {screen.unparsed} 件({UNKNOWN_TEXT})")
    if show_labels and any(owner.label for owner in screen.owners):
        lines.append("")
        lines.append(LABEL_NOTICE)
    return "\n".join(lines) + "\n"


def _short(ref: str) -> str:
    """符号の表示(先頭 12 桁 + 鍵の版)。符号は不透明な識別子で、鍵なしでは owner に結べない。"""
    version, _, digest = ref.partition(":")
    return f"{version}:{digest[:12]}"


def _reason_json(reason: Reason | None) -> dict[str, str] | None:
    return None if reason is None else {"code": reason.code, "text": reason.text}


def _row_json(row: CandidateRow) -> dict[str, Any]:
    return {
        "readable": row.readable,
        "seq": row.seq,
        "stock_code": row.stock_code,
        "buy_action": _reason_json(row.buy_action),
        "raer": {
            "value": row.raer_value,
            "state": _reason_json(row.raer_state),
            "confidence": row.confidence,
        },
        "rank": row.rank,
        "decision": _reason_json(row.decision),
        "shares": row.shares,
        "required_funds": row.required_funds,
        "shortfall": row.shortfall,
        "remaining_cash": row.remaining_cash,
        "reason": _reason_json(row.reason),
    }


def _owner_json(owner: OwnerSection, *, with_labels: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "ref": owner.ref,
        "display_ref": owner.display_ref,
        "status": owner.status.value,
        "outcome": _reason_json(owner.outcome),
        "run_status": _reason_json(owner.run_status),
        "skip_reason": _reason_json(owner.skip_reason),
        "unavailable_reason": _reason_json(owner.unavailable_reason),
        "available_cash": owner.available_cash,
        "last_reconciled_at": owner.last_reconciled_at,
        "portfolio_value": owner.portfolio_value,
        "expected_remaining_cash": owner.expected_remaining_cash,
        "allocated_count": owner.allocated_count,
        "not_allocated_count": owner.not_allocated_count,
        "incomplete_reasons": [_reason_json(r) for r in owner.incomplete_reasons],
        "exclusions": [{"reason": _reason_json(r), "count": n} for r, n in owner.exclusions],
        "model_version": owner.model_version,
        "policy_id": owner.policy_id,
        "contract_version": owner.contract_version,
        "shadow_mode": owner.shadow_mode,
        "candidates_truncated": owner.candidates_truncated,
        "unparsed": owner.unparsed,
        "candidates": [_row_json(row) for row in owner.rows],
    }
    if with_labels:
        body["label"] = owner.label
    return body


def screen_to_dict(screen: AllocationShadowScreen, *, with_labels: bool = False) -> dict[str, Any]:
    index = screen.index
    skip = screen.skip
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "disclaimer": DISCLAIMER,
        "business_date": screen.business_date,
        "state": screen.state.value,
        "state_text": DAY_STATE_TEXT[screen.state],
        "complete": screen.complete,
        "index": (
            None
            if index is None
            else {
                "key_version": index.key_version,
                "attempted_owner_count": index.attempted,
                "recorded_owner_count": index.recorded,
                "unrecorded_owner_count": index.unrecorded,
                "unrecorded_reasons": [
                    {"reason": _reason_json(r), "count": n} for r, n in index.unrecorded_reasons
                ],
                "owner_refs_truncated": index.owner_refs_truncated,
            }
        ),
        "skip": (
            None
            if skip is None
            else {
                "reason": _reason_json(skip.reason),
                "key_unavailable_reason": _reason_json(skip.key_unavailable_reason),
                "unrecorded_owner_count": skip.unrecorded_owner_count,
            }
        ),
        "owners": [_owner_json(o, with_labels=with_labels) for o in screen.owners],
        "missing_refs": list(screen.missing_refs),
        "schema_version_seen": screen.schema_version_seen,
        "unparsed": screen.unparsed,
        "over_cap_refs": screen.over_cap_refs,
    }


def render_json(screen: AllocationShadowScreen, *, with_labels: bool = False) -> str:
    return (
        json.dumps(screen_to_dict(screen, with_labels=with_labels), ensure_ascii=False, indent=2)
        + "\n"
    )
