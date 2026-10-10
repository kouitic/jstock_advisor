"""Issue #605 / 605-B: 本日の投資アクションの CLI 用 renderer(表・JSON)の契約テスト。

入力は 605-A の `TodayActionsView`。出力は文字列(表)と dict / JSON 文字列(V605-v1)。
純粋関数だけで、計算・時計・永続化・AWS・LINE 送信に触れない。

## 何を固定するのか

```
表   SHADOW 帯・買付余力・本日の結論(ACTIONS / NO_ACTION / UNAVAILABLE で文言を区別)・群の表示順・
     群ごとの行・『日本語名称(内部コード)』の併記・数量未確定は『売却株数:未確定』・
     Rotation は対で一体・Shadow 中は数値を出さない・見込み残高の根拠・
     変更なしの件数(--verbose のときだけ一覧)
JSON V605-v1: キー固定・金額は文字列・コードと表示名は別 field・null は null(0 にしない)・
     理由コードの互換性
```

## ★ 空振りの罠への対処(サブちゃんの申し送り #128 …6101393320)

renderer の出力を『文字列全体に含まれるか』(`in`)で見ると、どの群の主張も固定されない。
このテストは、**群ごと**(見出しから次の見出しまでの `| ` で始まる行)に分けて assert する。
表示例の銘柄・所有者・金額はすべて架空値である。
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.notification import today_actions_cli_renderer as r
from jstock_advisor.domain.notification import today_actions_view as tv

D = Decimal
_REPO = Path(__file__).resolve().parents[2]
_MODULE_PATH = (
    _REPO / "src" / "jstock_advisor" / "domain" / "notification" / "today_actions_cli_renderer.py"
)
_JST = dt.timezone(dt.timedelta(hours=9))
_NOW = dt.datetime(2026, 10, 12, 9, 5, tzinfo=_JST)
_EARLIER = dt.datetime(2026, 10, 5, 15, 0, tzinfo=_JST)
_PRICE_TIME = dt.datetime(2026, 10, 12, 8, 40, tzinfo=_JST)

H_RISK = "■ リスク回避の売却"
H_VALUE = "■ 割安解消の売却"
H_PROFIT = "■ 利益保全の売却"
H_LEGACY = "■ 既存の売却関連の判定（売却理由は未分類）"
H_ROTATION = "■ 資金入替（売却と購入は一体。売却しない場合、この購入は提案されません）"
H_BUY = "■ 購入（最終アクション）"
H_REASONS = "■ 算出不能の理由"
ALL_HEADERS = [H_RISK, H_VALUE, H_PROFIT, H_LEGACY, H_ROTATION, H_BUY, H_REASONS]


# --- 部品(架空値)-----------------------------------------------------------------------------


def basis(price: str = "2340") -> tv.PriceBasis:
    return tv.PriceBasis(price=D(price), as_of=_PRICE_TIME)


def cash(**over: object) -> tv.CashView:
    base: dict[str, object] = {
        "amount": D("812400"),
        "registered": True,
        "last_reconciled_at": _EARLIER,
        "reconciliation_state": tv.CashReconciliationState.CONFIRMED,
    }
    base.update(over)
    return tv.CashView(**base)  # type: ignore[arg-type]


def unconfirmed_cash() -> tv.CashView:
    return cash(last_reconciled_at=None, reconciliation_state=tv.CashReconciliationState.UNKNOWN)


def unregistered_cash() -> tv.CashView:
    return tv.CashView(
        amount=None,
        registered=False,
        last_reconciled_at=None,
        reconciliation_state=tv.CashReconciliationState.UNKNOWN,
    )


def sell(
    code: str = "0001",
    reason: tv.ExitReasonLabel = tv.ExitReasonLabel.RISK_EXIT,
    **over: object,
) -> tv.SellItemView:
    base: dict[str, object] = {
        "stock_code": code,
        "stock_name": f"銘柄{code}",
        "sell_action": tv.SellAction.FULL,
        "exit_reason": reason,
        "shares": 100,
        "estimated_amount": D("234000"),
        "price_basis": basis(),
        "summary_reasons": ("要点",),
    }
    base.update(over)
    return tv.SellItemView(**base)  # type: ignore[arg-type]


def legacy(
    label: str = "緊急確認", code: str = "0009", **over: object
) -> tv.LegacyDecisionItemView:
    base: dict[str, object] = {
        "stock_code": code,
        "stock_name": f"銘柄{code}",
        "legacy_label": label,
        "shares": None,
        "note": "売却理由は未分類",
    }
    base.update(over)
    return tv.LegacyDecisionItemView(**base)  # type: ignore[arg-type]


def leg(code: str, **over: object) -> tv.RotationLeg:
    base: dict[str, object] = {
        "stock_code": code,
        "stock_name": f"銘柄{code}",
        "shares": None,
        "estimated_amount": None,
        "price_basis": None,
    }
    base.update(over)
    return tv.RotationLeg(**base)  # type: ignore[arg-type]


def rotation_item(sell_code: str = "0005", buy_code: str = "0006") -> tv.RotationItemView:
    return tv.RotationItemView(sell_leg=leg(sell_code), buy_leg=leg(buy_code))


def confirmed_rotation_item() -> tv.RotationItemView:
    return tv.RotationItemView(
        sell_leg=leg("0005", shares=100, estimated_amount=D("100000"), price_basis=basis("1000")),
        buy_leg=leg("0006", shares=50, estimated_amount=D("100000"), price_basis=basis("2000")),
        numbers_confirmed=True,
        improvement=D("0.5"),
        counted_ratio=D("0.8"),
    )


def buy(
    code: str = "0002",
    final: tv.BuyFinalAction = tv.BuyFinalAction.PURCHASE,
    **over: object,
) -> tv.BuyItemView:
    base: dict[str, object] = {
        "stock_code": code,
        "stock_name": f"銘柄{code}",
        "standalone_judgement": "買い候補",
        "final_action": final,
        "skip_reason_code": None,
        "skip_reason_label": None,
        "shares": 200,
        "estimated_amount": D("468000"),
        "price_basis": basis(),
        "price_tier": None,
    }
    if final is tv.BuyFinalAction.SKIP:
        base.update(
            skip_reason_code="INSUFFICIENT_CASH_REMAINING",
            skip_reason_label="買付余力が不足するため",
            shares=None,
            estimated_amount=None,
            price_basis=None,
        )
    base.update(over)
    return tv.BuyItemView(**base)  # type: ignore[arg-type]


def allocated(*buys: tv.BuyItemView) -> tv.AllocationOutcome:
    return tv.AllocationOutcome(
        status=tv.AllocationOutcomeStatus.ALLOCATED_SOME, buys=buys or (buy(),)
    )


def allocation_no_action(*skips: tv.BuyItemView) -> tv.AllocationOutcome:
    return tv.AllocationOutcome(
        status=tv.AllocationOutcomeStatus.NO_ACTION,
        buys=skips or (buy("0003", tv.BuyFinalAction.SKIP),),
    )


def allocation_unavailable(*reasons: tv.ViewReason) -> tv.AllocationOutcome:
    return tv.AllocationOutcome(
        status=tv.AllocationOutcomeStatus.UNAVAILABLE,
        unavailable_reasons=reasons or (tv.ViewReason.POLICY_INCOMPLETE,),
    )


def rotation_outcome(*items: tv.RotationItemView) -> tv.RotationOutcome:
    if items:
        return tv.RotationOutcome(status=tv.RotationOutcomeStatus.ROTATION_PROPOSED, items=items)
    return tv.RotationOutcome(status=tv.RotationOutcomeStatus.NO_ROTATION)


def build(**over: object) -> tv.TodayActionsView:
    base: dict[str, object] = {
        "mode": tv.ViewMode.SHADOW,
        "owner": "owner-a",
        "evaluated_at": _NOW,
        "cash": cash(),
        "allocation": allocated(),
        "rotation": rotation_outcome(),
    }
    base.update(over)
    return tv.build_today_actions_view(**base)  # type: ignore[arg-type]


def full_view() -> tv.TodayActionsView:
    """全ての群に 1 件ずつ入った日(群の取り違えを見つけるために、コードは群ごとに別)。"""
    return build(
        allocation=allocated(buy("0002")),
        rotation=rotation_outcome(rotation_item("0005", "0006")),
        risk_exit_items=(sell("0001", tv.ExitReasonLabel.RISK_EXIT),),
        value_exit_items=(sell("0011", tv.ExitReasonLabel.VALUE_EXIT),),
        profit_protection_items=(sell("0012", tv.ExitReasonLabel.PROFIT_PROTECTION),),
        legacy_items=(legacy("緊急確認", "0009"),),
        projected_remaining_cash=D("344400"),
        unchanged_holdings_count=9,
    )


# --- 表の読み取り補助(群ごとに分ける)---------------------------------------------------------


def lines_of(text: str) -> list[str]:
    return text.split("\n")


def rows(text: str, header: str) -> list[str]:
    """見出し行の直後から続く『| 』で始まる行だけ(次の見出し・フッタで終わる)。"""
    out: list[str] = []
    seen = False
    for line in lines_of(text):
        if line == header:
            seen = True
            continue
        if seen:
            if line.startswith("| "):
                out.append(line)
            else:
                break
    return out


def has_header(text: str, header: str) -> bool:
    return header in lines_of(text)


def conclusion(text: str) -> list[str]:
    return [line for line in lines_of(text) if line.startswith("本日の結論")]


# --- 1 SHADOW 帯・見出し ----------------------------------------------------------------------


def test_the_shadow_banner_is_always_the_first_line() -> None:
    text = r.render_today_actions_table(build())
    assert lines_of(text)[0] == "試験運用：投資判断には使いません"
    assert r.SHADOW_BANNER == "試験運用：投資判断には使いません"


def test_the_banner_is_shown_for_every_shadow_view_and_not_for_active() -> None:
    for view in (build(allocation=None), build(allocation=allocation_no_action()), full_view()):
        assert lines_of(r.render_today_actions_table(view))[0] == r.SHADOW_BANNER
    active = build(mode=tv.ViewMode.ACTIVE, allow_active=True)
    assert r.SHADOW_BANNER not in r.render_today_actions_table(active)


def test_the_heading_line_names_the_owner_the_time_and_the_mode() -> None:
    heading = lines_of(r.render_today_actions_table(build()))[1]
    assert "owner-a" in heading
    assert "2026-10-12 09:05 JST" in heading
    assert "SHADOW" in heading


# --- 2 買付余力 -------------------------------------------------------------------------------


def cash_line(view: tv.TodayActionsView) -> str:
    found = [x for x in lines_of(r.render_today_actions_table(view)) if x.startswith("買付余力")]
    assert len(found) == 1
    return found[0]


def test_a_confirmed_cash_shows_the_amount_and_the_reconciliation_date() -> None:
    line = cash_line(build())
    assert "812,400円" in line
    assert "最終棚卸し 2026-10-05" in line


def test_an_unconfirmed_cash_says_so() -> None:
    line = cash_line(build(cash=unconfirmed_cash()))
    assert "812,400円" in line
    assert "最終棚卸し：未確認" in line


def test_an_unregistered_cash_is_not_shown_as_zero_yen() -> None:
    line = cash_line(build(cash=unregistered_cash(), allocation=allocation_unavailable()))
    assert "未登録" in line
    assert "0円" not in line


def test_a_registered_zero_cash_is_shown_as_zero_yen() -> None:
    line = cash_line(build(cash=cash(amount=D("0"))))
    assert "0円" in line
    assert "未登録" not in line


def test_amounts_are_formatted_without_exponents_or_float_rounding() -> None:
    line = cash_line(build(cash=cash(amount=D("1E+6"))))
    assert "1,000,000円" in line
    assert "E+" not in line


# --- 3 本日の結論(必須テスト 1)---------------------------------------------------------------


def test_there_is_exactly_one_conclusion_line() -> None:
    for view in (
        build(),
        build(allocation=None),
        build(allocation=allocation_no_action()),
        full_view(),
    ):
        assert len(conclusion(r.render_today_actions_table(view))) == 1


def test_an_actions_day_counts_the_proposals_by_kind() -> None:
    view = build(
        allocation=allocated(buy("0002"), buy("0004")),
        rotation=rotation_outcome(rotation_item()),
        risk_exit_items=(sell("0001"),),
        value_exit_items=(sell("0011", tv.ExitReasonLabel.VALUE_EXIT),),
    )
    (line,) = conclusion(r.render_today_actions_table(view))
    assert line == "本日の結論: 提案あり（売却2件・資金入替1件・購入2件）"


def test_a_purchase_only_day_says_zero_for_the_other_kinds() -> None:
    (line,) = conclusion(r.render_today_actions_table(build()))
    assert line == "本日の結論: 提案あり（売却0件・資金入替0件・購入1件）"


def test_no_action_without_legacy_decisions_says_skip() -> None:
    view = build(allocation=allocation_no_action())
    assert view.status is tv.ViewStatus.NO_ACTION
    (line,) = conclusion(r.render_today_actions_table(view))
    assert line == "本日の結論: 見送り"


def test_an_unavailable_day_never_says_skip_and_names_every_reason() -> None:
    view = build(
        allocation=allocation_unavailable(
            tv.ViewReason.POLICY_INCOMPLETE, tv.ViewReason.CASH_NOT_REGISTERED
        )
    )
    (line,) = conclusion(r.render_today_actions_table(view))
    assert line.startswith("本日の結論: 算出不能（理由: ")
    assert "見送り" not in line
    assert tv.ViewReason.POLICY_INCOMPLETE.label in line
    assert tv.ViewReason.CASH_NOT_REGISTERED.label in line


@pytest.mark.parametrize("reason", list(tv.ViewReason))
def test_every_reason_is_shown_with_its_label_and_its_code(reason: tv.ViewReason) -> None:
    text = r.render_today_actions_table(build(allocation=allocation_unavailable(reason)))
    assert has_header(text, H_REASONS)
    reason_lines = [
        line for line in lines_of(text) if line.startswith("・") and reason.value in line
    ]
    assert reason_lines == [f"・{reason.label}（{reason.value}）"]


def test_an_unevaluated_rotation_day_is_unavailable_with_its_own_reason() -> None:
    view = build(allocation=allocation_no_action(), rotation=None)
    text = r.render_today_actions_table(view)
    (line,) = conclusion(text)
    assert line.startswith("本日の結論: 算出不能")
    assert tv.ViewReason.ROTATION_NOT_EVALUATED.label in line
    assert "見送り" not in line


def test_an_unavailable_day_has_no_proposal_sections_but_keeps_legacy_information() -> None:
    view = build(allocation=allocation_unavailable(), legacy_items=(legacy("緊急確認", "0009"),))
    text = r.render_today_actions_table(view)
    assert not has_header(text, H_BUY)
    assert not has_header(text, H_RISK)
    assert len(rows(text, H_LEGACY)) == 1


# --- 3b 暫定: NO_ACTION かつ既存の判定あり(U-17。USER の決定まで定数 1 箇所へ隔離)-------------


def test_no_action_with_a_legacy_decision_never_says_skip() -> None:
    # ★ U-17: 『見送り』と『緊急確認』が同じ画面に並ぶと、緊急確認が不要に読める ★
    view = build(allocation=allocation_no_action(), legacy_items=(legacy("緊急確認"),))
    assert view.status is tv.ViewStatus.NO_ACTION
    (line,) = conclusion(r.render_today_actions_table(view))
    assert "見送り" not in line
    assert line == f"本日の結論: {r.PROVISIONAL_NO_ACTION_WITH_LEGACY_CONCLUSION}"
    assert "見送り" not in r.PROVISIONAL_NO_ACTION_WITH_LEGACY_CONCLUSION


@pytest.mark.parametrize("label", ["緊急確認", "利益保全注意", "一部売却", "全部売却", "売却"])
def test_every_legacy_label_uses_the_same_provisional_conclusion(label: str) -> None:
    view = build(allocation=allocation_no_action(), legacy_items=(legacy(label),))
    (line,) = conclusion(r.render_today_actions_table(view))
    assert "見送り" not in line


def test_only_the_legacy_case_uses_the_provisional_conclusion() -> None:
    plain = conclusion(r.render_today_actions_table(build(allocation=allocation_no_action())))
    assert r.PROVISIONAL_NO_ACTION_WITH_LEGACY_CONCLUSION not in plain[0]
    actions = conclusion(r.render_today_actions_table(full_view()))
    assert r.PROVISIONAL_NO_ACTION_WITH_LEGACY_CONCLUSION not in actions[0]


# --- 4 群の表示順・群ごとの行(U-11 / U-13)---------------------------------------------------


def test_the_groups_appear_in_the_agreed_order() -> None:
    text = r.render_today_actions_table(full_view())
    order = [
        lines_of(text).index(h) for h in (H_RISK, H_VALUE, H_PROFIT, H_LEGACY, H_ROTATION, H_BUY)
    ]
    assert order == sorted(order)


def test_empty_groups_are_not_shown() -> None:
    text = r.render_today_actions_table(build())
    for header in (H_RISK, H_VALUE, H_PROFIT, H_LEGACY, H_ROTATION, H_REASONS):
        assert not has_header(text, header)
    assert has_header(text, H_BUY)


def test_each_item_appears_only_in_the_rows_of_its_own_group() -> None:
    text = r.render_today_actions_table(full_view())
    own = {
        H_RISK: "0001",
        H_VALUE: "0011",
        H_PROFIT: "0012",
        H_LEGACY: "0009",
        H_ROTATION: "0005",
        H_BUY: "0002",
    }
    for header, code in own.items():
        group_rows = rows(text, header)
        assert len(group_rows) == 1, header
        assert f"({code})" in group_rows[0], header
        for other_header, other_code in own.items():
            if other_header != header:
                assert f"({other_code})" not in group_rows[0], (header, other_header)


def test_a_profit_protection_sell_is_not_listed_under_the_other_exit_groups() -> None:
    view = build(profit_protection_items=(sell("0012", tv.ExitReasonLabel.PROFIT_PROTECTION),))
    text = r.render_today_actions_table(view)
    assert rows(text, H_RISK) == [] and rows(text, H_VALUE) == []
    (row,) = rows(text, H_PROFIT)
    assert "利益保全（PROFIT_PROTECTION）" in row


# --- 5 売却の行(必須テスト 2・3・4)-----------------------------------------------------------


def test_a_sell_row_pairs_the_japanese_name_with_the_internal_code() -> None:
    (row,) = rows(r.render_today_actions_table(build(risk_exit_items=(sell("0001"),))), H_RISK)
    assert "銘柄0001(0001)" in row
    assert "全部売却（FULL）" in row
    assert "リスク回避（RISK_EXIT）" in row
    assert "100株" in row
    assert "約234,000円" in row
    assert "2,340円（2026-10-12 08:40）" in row
    assert "要点" in row


def test_a_partial_sell_is_labelled_partial() -> None:
    view = build(risk_exit_items=(sell("0001", sell_action=tv.SellAction.PARTIAL, shares=300),))
    (row,) = rows(r.render_today_actions_table(view), H_RISK)
    assert "一部売却（PARTIAL）" in row and "300株" in row


def test_an_undetermined_quantity_is_shown_as_unconfirmed_not_zero_or_all() -> None:
    item = sell(
        "0001",
        sell_action=tv.SellAction.UNDETERMINED,
        shares=None,
        estimated_amount=None,
        price_basis=None,
    )
    (row,) = rows(r.render_today_actions_table(build(risk_exit_items=(item,))), H_RISK)
    assert "売却株数：未確定" in row
    assert "売却(数量未確定)（UNDETERMINED）" in row
    assert "0株" not in row
    assert "全株" not in row
    assert "約" not in row  # 金額も作らない


def test_the_legacy_label_is_shown_as_is_and_is_not_translated() -> None:
    text = r.render_today_actions_table(
        build(allocation=allocation_no_action(), legacy_items=(legacy("緊急確認"),))
    )
    (row,) = rows(text, H_LEGACY)
    assert "緊急確認" in row
    assert "売却株数：未確定" in row
    assert "売却理由は未分類" in row
    for forbidden in ("RISK_EXIT", "リスク回避", "VALUE_EXIT", "PROFIT_PROTECTION"):
        assert forbidden not in row


def test_a_legacy_item_with_a_quantity_shows_it() -> None:
    item = legacy("一部売却", shares=300)
    text = r.render_today_actions_table(
        build(allocation=allocation_no_action(), legacy_items=(item,))
    )
    (row,) = rows(text, H_LEGACY)
    assert "300株" in row and "売却株数：未確定" not in row


def test_a_profit_protection_attention_is_not_listed_as_a_sell_proposal() -> None:
    item = legacy("利益保全注意", "0013")
    text = r.render_today_actions_table(
        build(allocation=allocation_no_action(), legacy_items=(item,))
    )
    for header in (H_RISK, H_VALUE, H_PROFIT):
        assert rows(text, header) == []
    (row,) = rows(text, H_LEGACY)
    assert "利益保全注意" in row


# --- 6 資金入替(必須テスト 5・6)--------------------------------------------------------------


def test_a_rotation_is_one_row_holding_both_legs() -> None:
    text = r.render_today_actions_table(
        build(rotation=rotation_outcome(rotation_item("0005", "0006")))
    )
    (row,) = rows(text, H_ROTATION)
    assert "売却: 銘柄0005(0005)" in row
    assert "購入: 銘柄0006(0006)" in row
    assert row.index("売却:") < row.index("購入:")


def test_unconfirmed_rotation_numbers_are_not_shown_in_shadow() -> None:
    text = r.render_today_actions_table(build(rotation=rotation_outcome(rotation_item())))
    (row,) = rows(text, H_ROTATION)
    assert "株数・金額・改善幅：未確定（試験運用）" in row
    assert not re.search(r"\d+株", row)
    assert "約" not in row
    assert "改善幅 " not in row and "算入割合" not in row


def test_the_rotation_header_carries_the_condition_text() -> None:
    text = r.render_today_actions_table(build(rotation=rotation_outcome(rotation_item())))
    assert tv.ROTATION_CONDITION_TEXT in H_ROTATION
    assert has_header(text, H_ROTATION)


def test_confirmed_rotation_numbers_are_shown_only_in_active_mode() -> None:
    view = build(
        mode=tv.ViewMode.ACTIVE,
        allow_active=True,
        rotation=rotation_outcome(confirmed_rotation_item()),
    )
    (row,) = rows(r.render_today_actions_table(view), H_ROTATION)
    assert "100株" in row and "50株" in row
    assert "改善幅 0.5" in row and "算入割合 0.8" in row
    assert "未確定（試験運用）" not in row


def test_a_shadow_only_rotation_is_not_shown_as_a_proposal() -> None:
    outcome = tv.RotationOutcome(status=tv.RotationOutcomeStatus.SHADOW_ONLY)
    text = r.render_today_actions_table(build(allocation=allocation_no_action(), rotation=outcome))
    assert not has_header(text, H_ROTATION)


# --- 7 購入の行(U-3)--------------------------------------------------------------------------


def test_a_purchase_row_separates_the_standalone_judgement_from_the_final_action() -> None:
    (row,) = rows(r.render_today_actions_table(build()), H_BUY)
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[0] == "銘柄0002(0002)"
    assert cells[1] == "買い候補"
    assert cells[2] == "購入"
    assert "200株" in cells[3]
    assert "約468,000円" in cells[4]
    assert "2,340円（2026-10-12 08:40）" in cells[5]


def test_a_skipped_candidate_shows_the_reason_label_and_code() -> None:
    text = r.render_today_actions_table(
        build(allocation=allocated(buy("0002"), buy("0003", tv.BuyFinalAction.SKIP)))
    )
    skip_rows = [row for row in rows(text, H_BUY) if "(0003)" in row]
    assert len(skip_rows) == 1
    cells = [c.strip() for c in skip_rows[0].strip("|").split("|")]
    assert cells[1] == "買い候補"
    assert cells[2] == "見送り"
    assert cells[3] == cells[4] == cells[5] == "-"
    assert "買付余力が不足するため（INSUFFICIENT_CASH_REMAINING）" in cells[6]


def test_a_price_tier_is_shown_when_present() -> None:
    (row,) = rows(
        r.render_today_actions_table(build(allocation=allocated(buy(price_tier="第2段")))), H_BUY
    )
    assert "段階: 第2段" in row


# --- 8 見込み残高・変更なし(U-10 / U-14)-------------------------------------------------------


def test_the_remaining_cash_is_an_estimate_and_says_so() -> None:
    text = r.render_today_actions_table(build(projected_remaining_cash=D("344400")))
    assert "見込み残高: 344,400円（目安）" in lines_of(text)


def test_the_remaining_cash_from_an_unconfirmed_cash_is_not_presented_as_confirmed() -> None:
    view = build(cash=unconfirmed_cash(), projected_remaining_cash=D("344400"))
    assert "見込み残高: 344,400円（未確認の残高に基づく目安）" in lines_of(
        r.render_today_actions_table(view)
    )


def test_no_remaining_cash_line_when_there_is_no_projection() -> None:
    assert not any(
        x.startswith("見込み残高") for x in lines_of(r.render_today_actions_table(build()))
    )


def test_the_unchanged_holdings_are_shown_as_a_count_only_by_default() -> None:
    view = build(unchanged_holdings_count=9)
    text = r.render_today_actions_table(view, unchanged_holding_names=("銘柄X", "銘柄Y"))
    assert "保有銘柄に変更なし: 9銘柄" in lines_of(text)
    assert "銘柄X" not in text and "銘柄Y" not in text


def test_the_unchanged_holdings_are_listed_only_with_verbose() -> None:
    view = build(unchanged_holdings_count=2)
    text = r.render_today_actions_table(
        view, verbose=True, unchanged_holding_names=("銘柄X", "銘柄Y")
    )
    assert "保有銘柄に変更なし: 2銘柄" in lines_of(text)
    assert "  ・銘柄X" in lines_of(text) and "  ・銘柄Y" in lines_of(text)


def test_verbose_without_names_still_shows_only_the_count() -> None:
    text = r.render_today_actions_table(build(unchanged_holdings_count=2), verbose=True)
    assert "保有銘柄に変更なし: 2銘柄" in lines_of(text)
    assert not any(x.startswith("  ・") for x in lines_of(text))


def test_no_unchanged_line_when_the_count_is_unknown() -> None:
    assert not any(
        x.startswith("保有銘柄に変更なし") for x in lines_of(r.render_today_actions_table(build()))
    )


# --- 9 JSON(必須テスト 8)---------------------------------------------------------------------

_TOP_LEVEL_KEYS = {
    "schema_version",
    "mode",
    "evaluated_at",
    "owner",
    "status",
    "reasons",
    "contract_versions",
    "model_versions",
    "comparison_keys",
    "rotation_evaluated",
    "cash",
    "sections",
    "projected_remaining_cash",
    "remaining_cash_basis",
    "unchanged_holdings_count",
}
_SECTION_KEYS = {"risk_exit", "value_exit", "profit_protection", "legacy", "rotation", "buy"}


def test_the_top_level_keys_are_fixed() -> None:
    # 破壊的変更(キーの追加・削除・改名)は schema_version の更新とこのテストの更新を同時に要する
    assert set(r.today_actions_to_json_dict(build())) == _TOP_LEVEL_KEYS
    assert set(r.today_actions_to_json_dict(full_view())) == _TOP_LEVEL_KEYS
    assert r.JSON_SCHEMA_VERSION == tv.SCHEMA_VERSION == "V605-v1"


def test_the_header_fields_are_machine_readable() -> None:
    data = r.today_actions_to_json_dict(build(contract_versions={"allocation": "C603-v1.2"}))
    assert data["schema_version"] == "V605-v1"
    assert data["mode"] == "SHADOW"
    assert data["owner"] == "owner-a"
    assert data["status"] == "ACTIONS"
    assert data["evaluated_at"] == "2026-10-12T09:05:00+09:00"
    assert data["contract_versions"] == {"allocation": "C603-v1.2"}
    assert data["rotation_evaluated"] is True


def test_the_time_is_converted_to_jst_whatever_the_input_offset() -> None:
    utc = dt.datetime(2026, 10, 12, 0, 5, tzinfo=dt.UTC)
    assert r.today_actions_to_json_dict(build(evaluated_at=utc))["evaluated_at"] == (
        "2026-10-12T09:05:00+09:00"
    )


def test_all_sections_are_always_present_even_when_empty() -> None:
    sections = r.today_actions_to_json_dict(build())["sections"]
    assert isinstance(sections, dict) and set(sections) == _SECTION_KEYS
    assert sections["risk_exit"] == [] and sections["legacy"] == [] and sections["rotation"] == []
    assert len(sections["buy"]) == 1


def test_the_cash_block_keeps_unregistered_apart_from_zero() -> None:
    zero = r.today_actions_to_json_dict(build(cash=cash(amount=D("0"))))["cash"]
    none = r.today_actions_to_json_dict(
        build(cash=unregistered_cash(), allocation=allocation_unavailable())
    )["cash"]
    assert zero == {
        "amount": "0",
        "registered": True,
        "last_reconciled_at": "2026-10-05T15:00:00+09:00",
        "reconciliation_state": "CONFIRMED",
    }
    assert none == {
        "amount": None,
        "registered": False,
        "last_reconciled_at": None,
        "reconciliation_state": "UNKNOWN",
    }


def test_an_unconfirmed_cash_has_a_null_reconciliation_time() -> None:
    block = r.today_actions_to_json_dict(build(cash=unconfirmed_cash()))["cash"]
    assert isinstance(block, dict)
    assert block["reconciliation_state"] == "UNKNOWN" and block["last_reconciled_at"] is None


def test_codes_and_labels_are_separate_fields() -> None:
    data = r.today_actions_to_json_dict(full_view())
    sections = data["sections"]
    assert isinstance(sections, dict)
    item = sections["risk_exit"][0]
    assert item["sell_action"] == {"code": "FULL", "label": "全部売却"}
    assert item["exit_reason"] == {"code": "RISK_EXIT", "label": "リスク回避"}
    assert item["stock_code"] == "0001" and item["stock_name"] == "銘柄0001"
    assert item["shares"] == 100
    assert item["estimated_amount"] == "234000"
    assert item["price_basis"] == {"price": "2340", "as_of": "2026-10-12T08:40:00+09:00"}


def test_an_undetermined_quantity_is_null_in_json_never_zero() -> None:
    item = sell(
        "0001",
        sell_action=tv.SellAction.UNDETERMINED,
        shares=None,
        estimated_amount=None,
        price_basis=None,
    )
    sections = r.today_actions_to_json_dict(build(risk_exit_items=(item,)))["sections"]
    assert isinstance(sections, dict)
    out = sections["risk_exit"][0]
    assert out["shares"] is None and out["estimated_amount"] is None and out["price_basis"] is None
    assert out["sell_action"] == {"code": "UNDETERMINED", "label": "売却(数量未確定)"}


def test_legacy_items_keep_the_label_and_carry_no_classification() -> None:
    view = build(allocation=allocation_no_action(), legacy_items=(legacy("緊急確認"),))
    sections = r.today_actions_to_json_dict(view)["sections"]
    assert isinstance(sections, dict)
    assert sections["legacy"] == [
        {
            "stock_code": "0009",
            "stock_name": "銘柄0009",
            "legacy_label": "緊急確認",
            "shares": None,
            "note": "売却理由は未分類",
        }
    ]
    assert sections["risk_exit"] == []


def test_a_shadow_rotation_item_has_null_numbers_and_the_fixed_condition() -> None:
    sections = r.today_actions_to_json_dict(build(rotation=rotation_outcome(rotation_item())))[
        "sections"
    ]
    assert isinstance(sections, dict)
    (item,) = sections["rotation"]
    assert item["numbers_confirmed"] is False
    assert item["improvement"] is None and item["counted_ratio"] is None
    assert item["sell_leg"]["shares"] is None and item["buy_leg"]["estimated_amount"] is None
    assert item["condition_text"] == tv.ROTATION_CONDITION_TEXT
    assert item["sell_leg"]["stock_code"] == "0005" and item["buy_leg"]["stock_code"] == "0006"


def test_buy_items_carry_the_standalone_judgement_and_the_skip_reason_as_codes() -> None:
    view = build(allocation=allocated(buy("0002"), buy("0003", tv.BuyFinalAction.SKIP)))
    sections = r.today_actions_to_json_dict(view)["sections"]
    assert isinstance(sections, dict)
    purchase, skipped = sections["buy"]
    assert purchase["final_action"] == {"code": "PURCHASE", "label": "購入"}
    assert purchase["standalone_judgement"] == "買い候補"
    assert purchase["skip_reason"] is None
    assert skipped["final_action"] == {"code": "SKIP", "label": "見送り"}
    assert skipped["skip_reason"] == {
        "code": "INSUFFICIENT_CASH_REMAINING",
        "label": "買付余力が不足するため",
    }
    assert skipped["shares"] is None


@pytest.mark.parametrize("reason", list(tv.ViewReason))
def test_every_reason_code_is_exported_with_its_label(reason: tv.ViewReason) -> None:
    data = r.today_actions_to_json_dict(build(allocation=allocation_unavailable(reason)))
    assert data["status"] == "UNAVAILABLE"
    assert data["reasons"] == [{"code": reason.value, "label": reason.label}]


@pytest.mark.parametrize("status", list(tv.ViewStatus))
def test_every_status_code_is_exported(status: tv.ViewStatus) -> None:
    views = {
        tv.ViewStatus.ACTIONS: build(),
        tv.ViewStatus.NO_ACTION: build(allocation=allocation_no_action()),
        tv.ViewStatus.UNAVAILABLE: build(allocation=None),
    }
    assert r.today_actions_to_json_dict(views[status])["status"] == status.value


def test_active_mode_is_exported_as_active() -> None:
    view = build(mode=tv.ViewMode.ACTIVE, allow_active=True)
    assert r.today_actions_to_json_dict(view)["mode"] == "ACTIVE"


def test_the_remaining_cash_and_its_basis_are_exported() -> None:
    data = r.today_actions_to_json_dict(
        build(
            cash=unconfirmed_cash(),
            projected_remaining_cash=D("344400"),
            unchanged_holdings_count=9,
        )
    )
    assert data["projected_remaining_cash"] == "344400"
    assert data["remaining_cash_basis"] == "ESTIMATE_FROM_UNCONFIRMED_CASH"
    assert data["unchanged_holdings_count"] == 9
    plain = r.today_actions_to_json_dict(build())
    assert plain["projected_remaining_cash"] is None and plain["remaining_cash_basis"] is None
    assert plain["unchanged_holdings_count"] is None


def _walk(value: object) -> list[object]:
    out: list[object] = [value]
    if isinstance(value, dict):
        for v in value.values():
            out.extend(_walk(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_walk(v))
    return out


def test_the_json_holds_no_float_and_no_decimal_object() -> None:
    for view in (
        build(),
        full_view(),
        build(cash=unconfirmed_cash(), projected_remaining_cash=D("1.5")),
    ):
        for node in _walk(r.today_actions_to_json_dict(view)):
            assert not isinstance(node, float | Decimal), node


def test_the_json_text_round_trips_to_the_dict() -> None:
    view = full_view()
    text = r.render_today_actions_json(view)
    assert json.loads(text) == r.today_actions_to_json_dict(view)
    assert "試験" not in text  # 日本語の見出し・帯は JSON に含めない
    assert "■" not in text


def test_the_json_text_keeps_japanese_labels_readable() -> None:
    text = r.render_today_actions_json(build(allocation=allocation_unavailable()))
    assert "配分の方針が未設定です" in text  # ensure_ascii = False
    assert "\\u" not in text


def test_the_json_key_order_is_stable() -> None:
    first = r.render_today_actions_json(full_view())
    assert r.render_today_actions_json(full_view()) == first
    assert list(json.loads(first))[0] == "schema_version"


# --- 10 純粋性・決定性・休眠(AST)------------------------------------------------------------


def test_the_renderers_are_deterministic_and_do_not_mutate_the_view() -> None:
    view = full_view()
    before = repr(view)
    assert r.render_today_actions_table(view) == r.render_today_actions_table(view)
    assert repr(view) == before


def test_the_renderers_return_plain_strings() -> None:
    assert isinstance(r.render_today_actions_table(build()), str)
    assert isinstance(r.render_today_actions_json(build()), str)


_ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "datetime",
    "decimal",
    "json",
    "jstock_advisor.domain.jst",
    "jstock_advisor.domain.notification.today_actions_view",
    "typing",
}


def _tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def test_the_module_imports_nothing_that_can_send_or_persist() -> None:
    found: set[str] = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    assert found <= _ALLOWED_IMPORTS, found - _ALLOWED_IMPORTS


def test_the_module_reads_no_clock_and_does_no_io_and_uses_no_float() -> None:
    forbidden_calls = {"now", "utcnow", "today", "open", "print", "input"}
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert name not in forbidden_calls, name
            assert name != "float"
        if isinstance(node, ast.Constant):
            assert not isinstance(node.value, float)
        if isinstance(node, ast.Name):
            assert node.id != "logger"


def test_the_module_is_not_imported_from_anywhere_in_src() -> None:
    src = _REPO / "src" / "jstock_advisor"
    for path in src.rglob("*.py"):
        if path == _MODULE_PATH:
            continue
        assert "today_actions_cli_renderer" not in path.read_text(encoding="utf-8"), str(path)
