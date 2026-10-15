"""候補ごとの詳細の画面モデル → 表の文字列 / JSON(Issue #605 追加 CLI。純粋・決定的)。

`allocation_shadow_detail_view.DetailScreen` を文字列にするだけ(計算・補完をしない)。

* 表: 日本語の見出し。候補ごとに『属性・価格・RAER(入力・出力・寄与)・配分』を並べる。
  ★ 既定でラベル(表示名)を出す ★ ので、ラベルが 1 件でも出たときは末尾に注意を付ける(`today` と同じ)
* JSON: ★ 既定ではラベルを出さない(符号だけ)★。`with_labels=True` のときだけ含める
* ★ `include_optional` ★: 出すかを切り替えられる項目
  (△: 保有評価額・方針の値・経過時間と読取の状態)。
  関数の既定は False(モデルには常に持つ)。CLI は USER の回答に基づき True で呼ぶ
* 金額・値は記録の文字列のまま(float にしない)。★ 値の文字列化は、検査を通った記録の値だけ ★
  (モデルが『不明』へ畳んだものは『不明』)。決定的(同じ画面は同じ出力)
"""

from __future__ import annotations

import json
from typing import Any, Final

from jstock_advisor.domain.notification.allocation_shadow_cli_renderer import (
    DISCLAIMER,
    LABEL_NOTICE,
    _opt,
    _reason_text,
    _row_json,
)
from jstock_advisor.domain.notification.allocation_shadow_cli_view import (
    DAY_STATE_TEXT,
    UNKNOWN_TEXT,
    DayState,
    OwnerStatus,
)
from jstock_advisor.domain.notification.allocation_shadow_detail_view import (
    CandidateDetail,
    DetailList,
    DetailMap,
    DetailScreen,
    DetailValue,
    OwnerDetail,
)

OUTPUT_SCHEMA_VERSION: Final = 1
_DASH: Final = "-"

#: 項目名 → 日本語。★ 出さない項目(△)は OPTIONAL_KEYS ★
_LABELS: Final[dict[str, str]] = {
    "kind": "種別",
    "price_tier": "価格帯",
    "fair_value_confidence": "適正価格の確信度",
    "quality_band": "質の帯",
    "sector": "業種",
    "high_risk": "high-risk",
    "current_position_value": "保有評価額",
    "current_price": "現在値",
    "price_as_of_date": "価格日",
    "freshness_verdict": "鮮度の判定",
    "upside_pct": "上昇余地(%)",
    "income_pct": "インカム(%)",
    "quality_score": "質のスコア",
    "quality_coverage_ratio": "質の被覆",
    "quality_min_coverage": "質の最低被覆",
    "trend_score": "トレンドのスコア",
    "trend_partial_coverage": "トレンドの被覆",
    "trend_components_used": "使ったトレンドの構成要素",
    "trend_weights": "トレンドの重み",
    "risk_flags_observed": "リスクフラグの観測",
    "company_quality_model_version": "質のモデル版",
    "earnings_trend_model_version": "決算トレンドのモデル版",
    "model_version": "RAER のモデル版",
    "comparison_key": "比較キー",
    "policy_comparison_key": "方針の比較キー",
    "risk_flags_available": "リスクフラグを観測できた",
    "applied_axes": "適用した軸",
    "unapplied_axes": "未適用の軸",
    "risk_flags": "リスクフラグ",
    "reason_codes": "理由コード",
    "coefficient_set_id": "係数セット",
    "contributions": "寄与度",
    "risk_contributions": "リスクの寄与",
    "base": "土台",
    "spec_flags": "仕様フラグ",
    "cap_limited_shares": "上限で制限された株数",
    "price_tier_used": "使った価格帯の段階",
    "cash_shortfall": "現金不足",
    "policy_id": "方針 ID",
    "min_raer_to_allocate": "RAER の最低基準",
    "max_single_stock_amount": "1 銘柄の金額上限",
    "max_single_stock_ratio": "1 銘柄の比率上限",
    "max_sector_ratio": "業種の比率上限",
    "max_high_risk_ratio": "high-risk の比率上限",
    "high_risk_flags": "high-risk のフラグ",
    "tier_quantity_caps": "価格帯ごとの株数上限",
    "minimum_trading_unit": "売買単位",
    "elapsed_ms": "経過時間(ms)",
    "read_states": "読取の状態",
}
#: 出すかを切り替えられる項目(△。USER の回答で CLI は出す)。モデルには常に持つ
OPTIONAL_KEYS: Final = frozenset(
    {
        "current_position_value",
        "policy_id",
        "min_raer_to_allocate",
        "max_single_stock_amount",
        "max_single_stock_ratio",
        "max_sector_ratio",
        "max_high_risk_ratio",
        "high_risk_flags",
        "tier_quantity_caps",
        "minimum_trading_unit",
        "policy_comparison_key",
        "elapsed_ms",
        "read_states",
    }
)


def _label(key: str) -> str:
    return _LABELS.get(key, key)


def _text(item: DetailValue | DetailList | DetailMap) -> str:
    if isinstance(item, DetailValue):
        return _opt(item.text)
    if isinstance(item, DetailList):
        if not item.present:
            return _DASH
        shown = ", ".join(item.items) if item.items else "(なし)"
        return shown
    if not item.present:
        return _DASH
    return ", ".join(f"{k}={v}" for k, v in item.pairs) if item.pairs else "(なし)"


def _visible(
    items: tuple[DetailValue | DetailList | DetailMap, ...], *, include_optional: bool
) -> list[DetailValue | DetailList | DetailMap]:
    return [i for i in items if include_optional or i.key not in OPTIONAL_KEYS]


def _pairs_line(
    items: tuple[DetailValue | DetailList | DetailMap, ...], *, include_optional: bool
) -> str:
    parts = [
        f"{_label(i.key)}={_text(i)}" for i in _visible(items, include_optional=include_optional)
    ]
    return " / ".join(parts) if parts else _DASH


def _candidate_lines(candidate: CandidateDetail, *, include_optional: bool) -> list[str]:
    row = candidate.row
    if not row.readable:
        return [f"  [候補 #{row.seq}] 銘柄 {UNKNOWN_TEXT}(記録が読めません)"]
    decision = row.decision.text if row.decision else _DASH
    rank = f"(順位 {row.rank})" if row.rank is not None else ""
    lines = [
        f"  [候補 #{row.seq}] 銘柄 {row.stock_code} / 判断 = {decision}{rank}"
        f" / 買い判定 = {row.buy_action.text if row.buy_action else _DASH}"
    ]
    lines.append(
        "    属性: " + _pairs_line(candidate.attributes, include_optional=include_optional)
    )
    lines.append("    価格: " + _pairs_line(candidate.price, include_optional=include_optional))
    raer = _opt(row.raer_value)
    state = row.raer_state.text if row.raer_state else _DASH
    lines.append(f"    RAER: 値 {raer} / 状態 {state} / 確信度 {_opt(row.confidence)}")
    lines.append("      入力: " + _pairs_line(candidate.raer_in, include_optional=include_optional))
    lines.append(
        "      出力: " + _pairs_line(candidate.raer_out, include_optional=include_optional)
    )
    lines.append(
        "      寄与: " + _pairs_line(candidate.raer_extra, include_optional=include_optional)
    )
    funds = _opt(row.required_funds)
    if row.shortfall is not None:
        funds = f"{funds}(不足 {row.shortfall})"
    lines.append(
        f"    配分: 株数 {_opt(row.shares)} / 必要資金 {funds}"
        + " / "
        + _pairs_line(candidate.allocation, include_optional=include_optional)
    )
    lines.append(f"    理由: {_reason_text(row.reason)}")
    return lines


def _owner_lines(
    owner: OwnerDetail, status: OwnerStatus | None, *, show_labels: bool, include_optional: bool
) -> list[str]:
    title = _owner_title_of(owner, show_labels=show_labels)
    lines = [f"■ 所有者: {title}"]
    if status is not None and status is not OwnerStatus.OK:
        return [*lines, f"  ★ 詳細を表示できません(状態: {status.value})"]
    lines.append(
        f"  買付余力: {_opt(owner.available_cash)} / 基準日時: {_opt(owner.last_reconciled_at)}"
        f" / ポートフォリオ評価額: {_opt(owner.portfolio_value)}"
    )
    if include_optional and owner.policy is not None:
        lines.append("  方針: " + _pairs_line(owner.policy.values, include_optional=True))
    if include_optional and owner.run:
        lines.append("  実行: " + _pairs_line(owner.run, include_optional=True))
    for candidate in owner.candidates:
        lines.extend(_candidate_lines(candidate, include_optional=include_optional))
    if owner.unparsed:
        lines.append(f"  ★ 解釈できなかった項目: {owner.unparsed} 件({UNKNOWN_TEXT})")
    return lines


def _owner_title_of(owner: OwnerDetail, *, show_labels: bool) -> str:
    if show_labels and owner.label:
        return f"{owner.label}({owner.display_ref})"
    return owner.display_ref


NOTICE_NO_PUBLIC_SURFACE: Final = (
    "この出力は利用者の端末だけで確認してください(Issue・PR などの公開面へ貼らない)"
)
NOTICE_NOT_INVESTMENT_ADVICE: Final = "これは Shadow の試験結果であり、実際の投資判断ではありません"
# 表の末尾・JSON の `notices` の両方が使う(USER の確定 #122: 出力方法によらず 2 つとも明記する)
NOTICES: Final = (NOTICE_NO_PUBLIC_SURFACE, NOTICE_NOT_INVESTMENT_ADVICE)


def render_detail_table(
    detail: DetailScreen, *, show_labels: bool = True, include_optional: bool = False
) -> str:
    screen = detail.screen
    lines = [
        DISCLAIMER,
        f"資金配分 Shadow 詳細  対象日: {screen.business_date}",
        f"状況: {DAY_STATE_TEXT[screen.state]}",
    ]
    if detail.filter_text is not None:
        lines.append(
            f"絞り込み: {detail.filter_text}"
            f"(表示 {detail.shown_candidates} / 全 {detail.total_candidates} 件)"
        )
    statuses = {section.ref: section.status for section in screen.owners}
    for owner in detail.owners:
        lines.append("")
        lines.extend(
            _owner_lines(
                owner,
                statuses.get(owner.ref),
                show_labels=show_labels,
                include_optional=include_optional,
            )
        )
    if detail.shown_candidates == 0 and screen.state in (DayState.COMPLETE, DayState.PARTIAL):
        lines.append("")
        if detail.filter_text is not None:
            lines.append(f"該当する候補がありません(絞り込み: {detail.filter_text})")
        else:
            lines.append("表示できる候補がありません")
    if detail.unparsed:
        lines.append("")
        lines.append(f"★ 解釈できなかった項目: 合計 {detail.unparsed} 件({UNKNOWN_TEXT})")
    lines.append("")
    lines.extend(f"※ {notice}" for notice in NOTICES)
    if show_labels and any(owner.label for owner in detail.owners):
        lines.append(LABEL_NOTICE)
    return "\n".join(lines) + "\n"


# --- JSON --------------------------------------------------------------------------------------


def _item_json(item: DetailValue | DetailList | DetailMap) -> Any:
    if isinstance(item, DetailValue):
        return item.text
    if isinstance(item, DetailList):
        return list(item.items) if item.present else None
    return dict(item.pairs) if item.present else None


def _section_json(
    items: tuple[DetailValue | DetailList | DetailMap, ...], *, include_optional: bool
) -> dict[str, Any]:
    return {i.key: _item_json(i) for i in _visible(items, include_optional=include_optional)}


def _candidate_json(candidate: CandidateDetail, *, include_optional: bool) -> dict[str, Any]:
    body = _row_json(candidate.row)
    body["attributes"] = _section_json(candidate.attributes, include_optional=include_optional)
    body["price"] = _section_json(candidate.price, include_optional=include_optional)
    body["raer_in"] = _section_json(candidate.raer_in, include_optional=include_optional)
    body["raer_out"] = _section_json(candidate.raer_out, include_optional=include_optional)
    body["raer_extra"] = _section_json(candidate.raer_extra, include_optional=include_optional)
    body["allocation_detail"] = _section_json(
        candidate.allocation, include_optional=include_optional
    )
    body["unparsed"] = candidate.unparsed
    return body


def detail_to_dict(
    detail: DetailScreen, *, with_labels: bool = False, include_optional: bool = False
) -> dict[str, Any]:
    screen = detail.screen
    statuses = {section.ref: section.status for section in screen.owners}
    owners: list[dict[str, Any]] = []
    for owner in detail.owners:
        body: dict[str, Any] = {
            "ref": owner.ref,
            "display_ref": owner.display_ref,
            "status": (statuses[owner.ref].value if owner.ref in statuses else None),
            "unparsed": owner.unparsed,
            "available_cash": owner.available_cash,
            "last_reconciled_at": owner.last_reconciled_at,
            "portfolio_value": owner.portfolio_value,
            "candidates": [
                _candidate_json(c, include_optional=include_optional) for c in owner.candidates
            ],
        }
        if include_optional:
            body["policy"] = (
                None
                if owner.policy is None
                else _section_json(owner.policy.values, include_optional=True)
            )
            body["run"] = _section_json(owner.run, include_optional=True)
        if with_labels:
            body["label"] = owner.label
        owners.append(body)
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "disclaimer": DISCLAIMER,
        "notices": list(NOTICES),
        "business_date": screen.business_date,
        "state": screen.state.value,
        "state_text": DAY_STATE_TEXT[screen.state],
        "complete": screen.complete,
        "filter": detail.filter_text,
        "total_candidates": detail.total_candidates,
        "shown_candidates": detail.shown_candidates,
        "owners": owners,
        "unparsed": detail.unparsed,
    }


def render_detail_json(
    detail: DetailScreen, *, with_labels: bool = False, include_optional: bool = False
) -> str:
    body = detail_to_dict(detail, with_labels=with_labels, include_optional=include_optional)
    return json.dumps(body, ensure_ascii=False, indent=2) + "\n"
