"""Issue #605 / 605-C: 本日の投資アクションの LINE renderer(分割つき)の契約テスト。

入力は 605-A の `TodayActionsView`。出力は LINE の本文(1 通 = 1 要素)の tuple。
純粋関数だけで、送信・計算・時計・永続化・AWS に触れない。**送信の呼び出し元は作らない**(U-16)。

## 何を固定するのか(USER の必須テスト)

```
7  分割で重要な情報の欠落・重複がない(L-2)。期待値は renderer の式から導かず、出力を独立に解析する
10 LINE renderer が実際の送信処理に接続されていない(AST: 送信系の import 0 / src から未参照)
1〜6 の LINE 面: NO_ACTION と UNAVAILABLE の区別・既存の判定を新分類へ変換しない・
     未確定数量を 0 / 全株にしない・Rotation の対・Shadow 中は数値を出さない
```

## 分割の不変条件

```
L-1 全メッセージの文字数 <= budget        L-2 項目の識別の多重集合が view と一致(欠落 0・重複 0)
L-3 群の順序が U-11 のとおり(通をまたいでも)  L-4 1 通目の件数が実際の項目数と一致
L-5 決定的                              L-6 (i/N) が 1..N で連番・N が正しい
L-7 項目が極端に多くても L-1〜L-6          L-8 『ほか』『他N件』『…など』を出さない
L-9 1 項目が予算を超える異常時だけ分割し、項目の識別は 1 回だけ数える
```

## ★ 空振りの罠(サブちゃんの申し送り #128 …6101393320)

出力を『文字列全体に含まれるか』(`in`)で見ると何も固定されない。通ごと・群ごとに分けて assert する。
表示例の銘柄・所有者・金額はすべて架空値である。
"""

from __future__ import annotations

import ast
import datetime as dt
import random
import re
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.notification import today_actions_cli_renderer as cli
from jstock_advisor.domain.notification import today_actions_line_renderer as ln
from jstock_advisor.domain.notification import today_actions_view as tv

D = Decimal
_REPO = Path(__file__).resolve().parents[2]
_MODULE_PATH = (
    _REPO / "src" / "jstock_advisor" / "domain" / "notification" / "today_actions_line_renderer.py"
)
_JST = dt.timezone(dt.timedelta(hours=9))
_NOW = dt.datetime(2026, 10, 12, 9, 5, tzinfo=_JST)
_EARLIER = dt.datetime(2026, 10, 5, 15, 0, tzinfo=_JST)
_PRICE_TIME = dt.datetime(2026, 10, 12, 8, 40, tzinfo=_JST)

HEADERS = [
    cli.HEADER_RISK,
    cli.HEADER_VALUE,
    cli.HEADER_PROFIT,
    cli.HEADER_LEGACY,
    cli.HEADER_ROTATION,
    cli.HEADER_BUY,
    cli.HEADER_REASONS,
]
ITEM_HEADERS = HEADERS[:6]
KEY_OF = {
    cli.HEADER_RISK: "risk",
    cli.HEADER_VALUE: "value",
    cli.HEADER_PROFIT: "profit",
    cli.HEADER_LEGACY: "legacy",
    cli.HEADER_ROTATION: "rotation",
    cli.HEADER_BUY: "buy",
}
TITLE_RE = re.compile(r"^【本日の投資アクション】(\S+) \((\d+)/(\d+)\)$")
COUNT_RE = re.compile(
    r"^件数: リスク回避(\d+)・割安解消(\d+)・利益保全(\d+)・既存の判定(\d+)・資金入替(\d+)"
    r"・購入欄(\d+)（うち購入(\d+)）・通数(\d+)$"
)
OMISSION_RE = re.compile(r"ほか|他\d+件|…など|その他\d+")


# --- 部品(架空値)-----------------------------------------------------------------------------


def basis(price: str = "2340", as_of: dt.datetime = _PRICE_TIME) -> tv.PriceBasis:
    return tv.PriceBasis(price=D(price), as_of=as_of)


def cash(**over: object) -> tv.CashView:
    base: dict[str, object] = {
        "amount": D("812400"),
        "registered": True,
        "last_reconciled_at": _EARLIER,
        "reconciliation_state": tv.CashReconciliationState.CONFIRMED,
    }
    base.update(over)
    return tv.CashView(**base)  # type: ignore[arg-type]


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


def render(view: tv.TodayActionsView, budget: int = ln.LINE_TEXT_CHAR_BUDGET) -> tuple[str, ...]:
    return ln.render_today_actions_line_messages(view, budget=budget)


# --- 出力の独立な解析(期待値を renderer の式から導かない)------------------------------------


def lines_of(message: str) -> list[str]:
    return message.split("\n")


def section_of(line: str) -> str | None:
    """『■ 見出し』『■ 見出し（続き）』から見出しを返す。見出しでなければ None。"""
    if not line.startswith("■ "):
        return None
    return line.removesuffix("（続き）")


def parse_items(messages: tuple[str, ...]) -> list[tuple[str, tuple[str, ...]]]:
    """(群, 識別〔括弧内のコード〕) を出現順に返す。『・』で始まる行が項目の先頭。"""
    found: list[tuple[str, tuple[str, ...]]] = []
    current: str | None = None
    for message in messages:
        for line in lines_of(message):
            header = section_of(line)
            if header is not None:
                current = header
                continue
            if current in ITEM_HEADERS and line.startswith("・"):
                found.append((KEY_OF[current], tuple(re.findall(r"\(([0-9A-Za-z]+)\)", line))))
    return found


def expected_items(view: tv.TodayActionsView) -> list[tuple[str, tuple[str, ...]]]:
    out: list[tuple[str, tuple[str, ...]]] = []
    out += [("risk", (i.stock_code,)) for i in view.risk_exit_items]
    out += [("value", (i.stock_code,)) for i in view.value_exit_items]
    out += [("profit", (i.stock_code,)) for i in view.profit_protection_items]
    out += [("legacy", (i.stock_code,)) for i in view.legacy_items]
    out += [
        ("rotation", (i.sell_leg.stock_code, i.buy_leg.stock_code)) for i in view.rotation_items
    ]
    out += [("buy", (i.stock_code,)) for i in view.buy_items]
    return out


def headers_in_order(messages: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for message in messages:
        for line in lines_of(message):
            header = section_of(line)
            if header is not None:
                out.append(header)
    return out


def item_blocks(message: str, header: str) -> list[list[str]]:
    """1 通の中の、指定した群の項目ブロック(『・』の行 + 続く 2 空白の行)。"""
    blocks: list[list[str]] = []
    inside = False
    for line in lines_of(message):
        found = section_of(line)
        if found is not None:
            inside = found == header
            continue
        if inside and line.startswith("・"):
            blocks.append([line])
        elif inside and line.startswith("  ") and blocks:
            blocks[-1].append(line)
    return blocks


def all_blocks(messages: tuple[str, ...], header: str) -> list[list[str]]:
    return [block for m in messages for block in item_blocks(m, header)]


# --- 1 API・予算 ------------------------------------------------------------------------------


def test_the_budget_matches_the_application_budget_and_fits_the_protocol_limit() -> None:
    from jstock_advisor.infrastructure.line.client import LINE_MAX_TEXT_CHARS
    from jstock_advisor.services.line_notification_service import NOTIFICATION_TEXT_CHAR_BUDGET

    assert ln.LINE_TEXT_CHAR_BUDGET == NOTIFICATION_TEXT_CHAR_BUDGET == 4500
    assert ln.LINE_TEXT_CHAR_BUDGET < LINE_MAX_TEXT_CHARS


def test_the_renderer_returns_a_tuple_of_strings() -> None:
    out = render(full_view())
    assert isinstance(out, tuple) and out
    assert all(isinstance(m, str) and m for m in out)


@pytest.mark.parametrize("bad", [0, 199, -1, True, 4500.0, "4500", None])
def test_an_unusable_budget_is_rejected(bad: object) -> None:
    with pytest.raises(ValueError):
        ln.render_today_actions_line_messages(build(), budget=bad)  # type: ignore[arg-type]


def test_the_smallest_accepted_budget_still_satisfies_the_limit() -> None:
    out = render(full_view(), budget=200)
    assert all(len(m) <= 200 for m in out)


# --- 2 タイトル・SHADOW 帯 ---------------------------------------------------------------------


def test_every_message_starts_with_a_numbered_title() -> None:
    out = render(full_view(), budget=400)
    assert len(out) > 1
    for index, message in enumerate(out, start=1):
        match = TITLE_RE.match(lines_of(message)[0])
        assert match is not None, message[:60]
        assert match.group(1) == "owner-a"
        assert (int(match.group(2)), int(match.group(3))) == (index, len(out))


def test_a_single_message_is_numbered_one_of_one() -> None:
    (message,) = render(build())
    assert lines_of(message)[0] == "【本日の投資アクション】owner-a (1/1)"


def test_the_shadow_banner_is_on_every_message() -> None:
    out = render(full_view(), budget=400)
    assert len(out) > 1
    for message in out:
        assert lines_of(message)[1] == cli.SHADOW_BANNER


def test_active_mode_has_no_banner() -> None:
    view = build(mode=tv.ViewMode.ACTIVE, allow_active=True)
    assert all(cli.SHADOW_BANNER not in m for m in render(view))


# --- 3 1 通目: 見出し・買付余力・結論・件数(L-4)---------------------------------------------


def test_the_first_message_carries_the_heading_the_cash_and_the_conclusion() -> None:
    first = lines_of(render(full_view())[0])
    assert cli.heading_line(full_view()) in first
    assert cli.cash_line(full_view().cash) in first
    start = first.index("本日の結論:")
    assert tuple(first[start : start + 3]) == cli.conclusion_lines(full_view())


def test_the_conclusion_uses_the_same_words_as_the_table_for_every_status() -> None:
    views = [
        build(),
        build(allocation=allocation_no_action()),
        build(allocation=allocation_unavailable()),
        build(allocation=None),
        build(
            allocation=allocation_no_action(),
            legacy_items=(legacy("緊急確認"), legacy("利益保全注意", "0013")),
        ),
    ]
    for view in views:
        first = lines_of(render(view)[0])
        start = first.index("本日の結論:")
        assert tuple(first[start : start + 3]) == cli.conclusion_lines(view)


def test_the_conclusion_only_appears_in_the_first_message() -> None:
    out = render(full_view(), budget=600)
    assert sum("本日の結論:" in lines_of(m) for m in out) == 1
    assert "本日の結論:" in lines_of(out[0])


def count_line(message: str) -> re.Match[str]:
    found = [x for x in lines_of(message) if x.startswith("件数:")]
    assert len(found) == 1
    match = COUNT_RE.match(found[0])
    assert match is not None, found[0]
    return match


def test_the_count_line_matches_the_items_actually_present() -> None:
    view = full_view()
    out = render(view, budget=400)
    match = count_line(out[0])
    items = parse_items(out)
    by_key = {k: sum(1 for key, _ in items if key == k) for k in KEY_OF.values()}
    expected = (
        by_key["risk"],
        by_key["value"],
        by_key["profit"],
        by_key["legacy"],
        by_key["rotation"],
        by_key["buy"],
    )
    assert tuple(int(match.group(i)) for i in range(1, 7)) == expected
    assert int(match.group(7)) == sum(
        1 for b in view.buy_items if b.final_action is tv.BuyFinalAction.PURCHASE
    )
    assert int(match.group(8)) == len(out)


def test_only_the_first_message_has_a_count_line() -> None:
    out = render(full_view(), budget=400)
    assert sum(any(x.startswith("件数:") for x in lines_of(m)) for m in out) == 1


# --- 4 群の表示順・見出し(U-11 / U-13)---------------------------------------------------------


def test_the_groups_appear_in_the_agreed_order_in_a_single_message() -> None:
    (message,) = render(full_view())
    headers = [section_of(x) for x in lines_of(message) if section_of(x)]
    assert headers == [
        cli.HEADER_RISK,
        cli.HEADER_VALUE,
        cli.HEADER_PROFIT,
        cli.HEADER_LEGACY,
        cli.HEADER_ROTATION,
        cli.HEADER_BUY,
    ]


def test_empty_groups_are_not_shown() -> None:
    (message,) = render(build())
    assert [section_of(x) for x in lines_of(message) if section_of(x)] == [cli.HEADER_BUY]


def test_the_reasons_section_lists_label_and_code() -> None:
    view = build(allocation=allocation_unavailable(tv.ViewReason.POLICY_INCOMPLETE))
    (message,) = render(view)
    assert cli.HEADER_REASONS in lines_of(message)
    assert "・配分の方針が未設定です（POLICY_INCOMPLETE）" in lines_of(message)


def test_the_header_words_are_the_ones_of_the_table() -> None:
    (message,) = render(full_view())
    for header in ITEM_HEADERS:
        assert header in lines_of(message)


# --- 5 項目の書式(群ごとに assert)----------------------------------------------------------


def blocks_of(view: tv.TodayActionsView, header: str) -> list[list[str]]:
    return all_blocks(render(view), header)


def test_a_sell_item_pairs_the_japanese_name_with_the_internal_code() -> None:
    view = build(risk_exit_items=(sell("0001"),))
    (block,) = blocks_of(view, cli.HEADER_RISK)
    assert block[0] == "・銘柄0001(0001)"
    assert block[1] == "  全部売却（FULL）／リスク回避（RISK_EXIT）"
    assert block[2] == "  100株 約234,000円 価格 2,340円（2026-10-12 08:40）"
    assert block[3] == "  要点: 要点"


def test_a_partial_sell_is_labelled_partial() -> None:
    view = build(risk_exit_items=(sell("0001", sell_action=tv.SellAction.PARTIAL, shares=300),))
    (block,) = blocks_of(view, cli.HEADER_RISK)
    assert "一部売却（PARTIAL）" in block[1] and block[2].startswith("  300株")


def test_an_undetermined_quantity_is_unconfirmed_not_zero_or_all() -> None:
    item = sell(
        "0001",
        sell_action=tv.SellAction.UNDETERMINED,
        shares=None,
        estimated_amount=None,
        price_basis=None,
    )
    (block,) = blocks_of(build(risk_exit_items=(item,)), cli.HEADER_RISK)
    text = "\n".join(block)
    assert "  売却株数：未確定" in block
    assert "売却(数量未確定)（UNDETERMINED）" in text
    assert "0株" not in text and "全株" not in text and "約" not in text


def test_the_legacy_label_is_shown_as_is_and_is_not_translated() -> None:
    view = build(allocation=allocation_no_action(), legacy_items=(legacy("緊急確認"),))
    (block,) = blocks_of(view, cli.HEADER_LEGACY)
    assert block[0] == "・銘柄0009(0009)"
    assert block[1] == "  緊急確認／売却株数：未確定"
    assert block[2] == "  売却理由は未分類"
    for forbidden in ("RISK_EXIT", "リスク回避", "VALUE_EXIT", "PROFIT_PROTECTION"):
        assert forbidden not in "\n".join(block)


def test_a_legacy_attention_is_not_listed_among_the_sell_groups() -> None:
    view = build(allocation=allocation_no_action(), legacy_items=(legacy("利益保全注意", "0013"),))
    out = render(view)
    for header in (cli.HEADER_RISK, cli.HEADER_VALUE, cli.HEADER_PROFIT):
        assert all_blocks(out, header) == []
    (block,) = all_blocks(out, cli.HEADER_LEGACY)
    assert "利益保全注意" in block[1]


def test_a_rotation_is_one_item_holding_both_legs() -> None:
    view = build(rotation=rotation_outcome(rotation_item("0005", "0006")))
    (block,) = blocks_of(view, cli.HEADER_ROTATION)
    assert block[0] == "・売却: 銘柄0005(0005) → 購入: 銘柄0006(0006)"
    assert block[1] == "  株数・金額・改善幅：未確定（試験運用）"


def test_confirmed_rotation_numbers_are_shown_only_in_active_mode() -> None:
    view = build(
        mode=tv.ViewMode.ACTIVE,
        allow_active=True,
        rotation=rotation_outcome(confirmed_rotation_item()),
    )
    (block,) = blocks_of(view, cli.HEADER_ROTATION)
    assert block[1] == "  売却 100株 約100,000円／購入 50株 約100,000円／改善幅 0.5・算入割合 0.8"


def test_unconfirmed_rotation_numbers_are_not_shown_in_shadow() -> None:
    view = build(rotation=rotation_outcome(rotation_item()))
    (block,) = blocks_of(view, cli.HEADER_ROTATION)
    assert not re.search(r"\d+株", "\n".join(block)) and "約" not in "\n".join(block)
    assert "改善幅 " not in "\n".join(block)


def test_a_purchase_item_separates_the_standalone_judgement_from_the_final_action() -> None:
    (block,) = blocks_of(build(), cli.HEADER_BUY)
    assert block[0] == "・銘柄0002(0002)"
    assert block[1] == "  銘柄単独の判断: 買い候補／最終アクション: 購入"
    assert block[2] == "  200株 約468,000円 価格 2,340円（2026-10-12 08:40）"


def test_a_skipped_candidate_shows_the_reason_label_and_code() -> None:
    view = build(allocation=allocated(buy("0002"), buy("0003", tv.BuyFinalAction.SKIP)))
    blocks = {b[0]: b for b in blocks_of(view, cli.HEADER_BUY)}
    skipped = blocks["・銘柄0003(0003)"]
    assert skipped[1] == "  銘柄単独の判断: 買い候補／最終アクション: 見送り"
    assert skipped[2] == "  理由: 買付余力が不足するため（INSUFFICIENT_CASH_REMAINING）"


def test_a_price_tier_is_shown_when_present() -> None:
    (block,) = blocks_of(build(allocation=allocated(buy(price_tier="第2段"))), cli.HEADER_BUY)
    assert block[2].endswith(" 段階: 第2段")


def test_a_price_time_in_utc_is_shown_in_jst() -> None:
    utc = dt.datetime(2026, 10, 11, 20, 0, tzinfo=dt.UTC)
    item = sell("0001", price_basis=basis(as_of=utc))
    (block,) = blocks_of(build(risk_exit_items=(item,)), cli.HEADER_RISK)
    assert "2,340円（2026-10-12 05:00）" in block[2]


# --- 6 フッタ ---------------------------------------------------------------------------------


def test_the_footer_lines_are_in_the_last_message() -> None:
    out = render(full_view(), budget=400)
    last = lines_of(out[-1])
    assert "見込み残高: 344,400円（目安）" in last
    assert "保有銘柄に変更なし: 9銘柄" in last
    for message in out[:-1]:
        assert "見込み残高: 344,400円（目安）" not in lines_of(message)


def test_the_remaining_cash_from_an_unconfirmed_cash_is_not_presented_as_confirmed() -> None:
    unconfirmed = cash(
        last_reconciled_at=None, reconciliation_state=tv.CashReconciliationState.UNKNOWN
    )
    view = build(cash=unconfirmed, projected_remaining_cash=D("344400"))
    (message,) = render(view)
    assert "見込み残高: 344,400円（未確認の残高に基づく目安）" in lines_of(message)


def test_the_unchanged_holdings_are_a_count_only() -> None:
    (message,) = render(build(unchanged_holdings_count=2))
    assert "保有銘柄に変更なし: 2銘柄" in lines_of(message)
    assert not any(x.startswith("  ・") for x in lines_of(message))


# --- 7 必須テスト 1〜6 の LINE 面 -----------------------------------------------------------


def test_no_action_and_unavailable_are_told_apart() -> None:
    no_action = lines_of(render(build(allocation=allocation_no_action()))[0])
    unavailable = lines_of(render(build(allocation=allocation_unavailable()))[0])
    assert "  新しい提案（購入・資金入替・新分類の売却）: なし" in no_action
    assert any(
        x.startswith("  新しい提案（購入・資金入替・新分類の売却）: 算出不能（理由: ")
        for x in unavailable
    )


def test_an_unavailable_day_keeps_the_legacy_information() -> None:
    view = build(allocation=allocation_unavailable(), legacy_items=(legacy("緊急確認"),))
    out = render(view)
    assert len(all_blocks(out, cli.HEADER_LEGACY)) == 1
    assert "  既存の売却関連の判定: あり — 緊急確認1件（理由は未分類）" in lines_of(out[0])


def test_the_legacy_counts_are_in_the_conclusion_and_not_translated() -> None:
    view = build(
        allocation=allocation_no_action(),
        legacy_items=(
            legacy("緊急確認", "0009"),
            legacy("利益保全注意", "0013"),
            legacy("緊急確認", "0014"),
        ),
    )
    first = lines_of(render(view)[0])
    assert "  既存の売却関連の判定: あり — 緊急確認2件・利益保全注意1件（理由は未分類）" in first
    assert not any(
        "RISK_EXIT" in x or "リスク回避" in x for x in first[first.index("本日の結論:") :][:3]
    )


# --- 8 分割の不変条件 L-1〜L-9(必須テスト 7)-------------------------------------------------


def synthetic_view(rng: random.Random) -> tv.TodayActionsView:
    counter = iter(range(1000, 100000))

    def code() -> str:
        return f"{next(counter)}"

    def name(c: str) -> str:
        return f"銘柄{c}" + "あ" * rng.randint(0, 40)

    def reasons() -> tuple[str, ...]:
        return tuple("理由" + "い" * rng.randint(0, 60) for _ in range(rng.randint(0, 3)))

    def sells(reason: tv.ExitReasonLabel, n: int) -> tuple[tv.SellItemView, ...]:
        out = []
        for _ in range(n):
            c = code()
            out.append(sell(c, reason, stock_name=name(c), summary_reasons=reasons()))
        return tuple(out)

    n_buy, n_skip = rng.randint(0, 40), rng.randint(0, 20)
    buys = []
    for _ in range(n_buy):
        c = code()
        buys.append(buy(c, stock_name=name(c)))
    for _ in range(n_skip):
        c = code()
        buys.append(buy(c, tv.BuyFinalAction.SKIP, stock_name=name(c)))
    if n_buy:
        allocation = allocated(*buys)
    elif n_skip:
        allocation = allocation_no_action(*buys)
    else:
        allocation = allocation_unavailable()
    rotations = []
    for _ in range(rng.randint(0, 25)):
        s, b = code(), code()
        rotations.append(
            tv.RotationItemView(
                sell_leg=leg(s, stock_name=name(s)), buy_leg=leg(b, stock_name=name(b))
            )
        )
    legacies = []
    for _ in range(rng.randint(0, 25)):
        c = code()
        legacies.append(
            legacy(
                rng.choice(["緊急確認", "利益保全注意", "一部売却", "未知の区分"]),
                c,
                stock_name=name(c),
            )
        )
    return build(
        allocation=allocation,
        rotation=rotation_outcome(*rotations),
        risk_exit_items=sells(tv.ExitReasonLabel.RISK_EXIT, rng.randint(0, 40)),
        value_exit_items=sells(tv.ExitReasonLabel.VALUE_EXIT, rng.randint(0, 30)),
        profit_protection_items=sells(tv.ExitReasonLabel.PROFIT_PROTECTION, rng.randint(0, 30)),
        legacy_items=tuple(legacies),
        projected_remaining_cash=D("344400") if rng.random() < 0.5 else None,
        unchanged_holdings_count=rng.choice([None, 0, 12]),
    )


def check_invariants(view: tv.TodayActionsView, out: tuple[str, ...], budget: int) -> None:
    # L-1
    assert all(len(m) <= budget for m in out), [len(m) for m in out]
    # L-2(欠落 0・重複 0)
    assert sorted(parse_items(out)) == sorted(expected_items(view))
    # L-3(群の順序。通をまたいでも)
    order = [ITEM_HEADERS.index(h) for h in headers_in_order(out) if h in ITEM_HEADERS]
    assert order == sorted(order)
    # 項目そのものの順序も、view に渡した順序のまま(群の中で入れ替えない)
    for key, items in (
        ("risk", view.risk_exit_items),
        ("value", view.value_exit_items),
        ("profit", view.profit_protection_items),
        ("legacy", view.legacy_items),
        ("buy", view.buy_items),
    ):
        got = [ident[0] for k, ident in parse_items(out) if k == key]
        assert got == [i.stock_code for i in items], key
    # L-4・L-6
    match = count_line(out[0])
    counted = {k: sum(1 for key, _ in parse_items(out) if key == k) for k in KEY_OF.values()}
    assert tuple(int(match.group(i)) for i in range(1, 7)) == tuple(
        counted[k] for k in ("risk", "value", "profit", "legacy", "rotation", "buy")
    )
    assert int(match.group(8)) == len(out)
    for index, message in enumerate(out, start=1):
        title = TITLE_RE.match(lines_of(message)[0])
        assert title is not None
        assert (int(title.group(2)), int(title.group(3))) == (index, len(out))
    # L-8
    assert not OMISSION_RE.search("\n".join(out))


@pytest.mark.parametrize("budget", [400, 1000, 4500])
def test_the_split_invariants_hold_for_random_views(budget: int) -> None:
    rng = random.Random(605)
    for _ in range(60):
        view = synthetic_view(rng)
        check_invariants(view, render(view, budget), budget)


def test_a_thousand_items_are_split_without_losing_any() -> None:
    items = tuple(sell(f"{2000 + i}", stock_name=f"銘柄{2000 + i}") for i in range(1000))
    view = build(allocation=allocation_no_action(), risk_exit_items=items)
    out = render(view)
    assert len(out) > 10
    check_invariants(view, out, ln.LINE_TEXT_CHAR_BUDGET)
    assert len(parse_items(out)) == 1000 + 1  # 見送りの購入候補 1 件


def test_the_split_is_deterministic() -> None:
    view = synthetic_view(random.Random(7))
    assert render(view, 400) == render(view, 400)


def test_the_view_is_not_mutated() -> None:
    view = full_view()
    before = repr(view)
    render(view, 400)
    assert repr(view) == before


def test_a_section_spanning_messages_is_continued_in_the_next_message() -> None:
    items = tuple(sell(f"{3000 + i}") for i in range(40))
    out = render(build(allocation=allocation_no_action(), risk_exit_items=items), budget=600)
    firsts = [lines_of(m) for m in out]
    risk_message_indexes = [
        i for i, ls in enumerate(firsts) if any(section_of(x) == cli.HEADER_RISK for x in ls)
    ]
    assert len(risk_message_indexes) > 1
    assert cli.HEADER_RISK in firsts[risk_message_indexes[0]]  # 最初の見出しは『（続き）』なし
    for i in risk_message_indexes[1:]:
        assert cli.HEADER_RISK + "（続き）" in firsts[i]
        assert cli.HEADER_RISK not in firsts[i]


def test_a_section_is_not_marked_as_continued_when_it_starts_in_a_new_message() -> None:
    out = render(full_view(), budget=400)
    for message in out:
        for line in lines_of(message):
            header = section_of(line)
            if header is not None and line.endswith("（続き）"):
                # 『（続き）』の見出しは、その群の項目を前の通で出し終えていない通にだけ付く
                previous = out[: out.index(message)]
                assert any(section_of(x) == header for m in previous for x in lines_of(m))


def test_no_item_block_is_split_across_messages_when_it_fits() -> None:
    items = tuple(sell(f"{4000 + i}") for i in range(30))
    out = render(build(allocation=allocation_no_action(), risk_exit_items=items), budget=500)
    for message in out:
        for block in item_blocks(message, cli.HEADER_RISK):
            assert len(block) == 4  # 名前 / 区分 / 数量 / 要点
            assert not any("（続" in line for line in block)


def test_the_omission_words_never_appear_even_with_a_huge_day() -> None:
    items = tuple(sell(f"{5000 + i}") for i in range(300))
    out = render(build(risk_exit_items=items), budget=800)
    assert not OMISSION_RE.search("\n".join(out))


# --- 9 1 項目が予算を超える異常時(L-9)-------------------------------------------------------


def test_an_oversized_item_is_split_and_counted_once() -> None:
    long_note = "ぬ" * 900
    huge = sell("0001", summary_reasons=(long_note,))
    view = build(allocation=allocation_no_action(), risk_exit_items=(huge,))
    out = render(view, budget=400)
    assert len(out) >= 3
    assert all(len(m) <= 400 for m in out)
    assert parse_items(out).count(("risk", ("0001",))) == 1  # 識別は 1 回だけ
    # 内容は失われない: 断片を全部足すと元の 900 文字(この view に他の『ぬ』は無い)
    assert sum(m.count("ぬ") for m in out) == 900


def test_continuation_markers_are_used_only_for_an_oversized_item() -> None:
    huge = sell("0001", summary_reasons=("ぬ" * 900,))
    out = render(build(allocation=allocation_no_action(), risk_exit_items=(huge,)), budget=400)
    assert any("（続く）" in m for m in out)
    normal = render(full_view())
    assert not any("（続く）" in m for m in normal)


# --- 10 純粋性・送信との分離(必須テスト 10。AST)--------------------------------------------

_ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "jstock_advisor.domain.notification.today_actions_cli_renderer",
    "jstock_advisor.domain.notification.today_actions_view",
    "typing",
}
_SEND_WORDS = ("send", "push", "post", "publish", "invoke", "deliver", "notify")


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
    for module in found:
        for forbidden in ("services", "infrastructure", "lambda_handlers", "cli", "line", "boto"):
            assert not module.startswith(f"jstock_advisor.{forbidden}"), module


def test_the_module_defines_and_calls_nothing_that_looks_like_sending() -> None:
    for node in ast.walk(_tree()):
        names: list[str] = []
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.append(node.name)
        elif isinstance(node, ast.Call):
            func = node.func
            names.append(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
        for name in names:
            assert not any(word in name.lower() for word in _SEND_WORDS), name


def test_the_module_reads_no_clock_does_no_io_and_uses_no_float() -> None:
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


_ALLOWED_IMPORTERS: set[str] = set()  # この module を import してよい src の module(現時点は無し)


def test_the_module_is_imported_from_nowhere_in_src() -> None:
    src = _REPO / "src" / "jstock_advisor"
    for path in src.rglob("*.py"):
        if path == _MODULE_PATH:
            continue
        if "today_actions_line_renderer" in path.read_text(encoding="utf-8"):
            assert path.relative_to(src).as_posix() in _ALLOWED_IMPORTERS, str(path)


# --- 追加: 件数ヘッダの『うち購入』・見出しの重複・異常時の分割の境界 --------------------


def test_the_purchase_count_in_the_header_excludes_the_skipped_candidates() -> None:
    skipped = (buy("0007", tv.BuyFinalAction.SKIP), buy("0008", tv.BuyFinalAction.SKIP))
    view = build(allocation=allocated(buy("0002"), buy("0003"), buy("0004"), *skipped))
    match = count_line(render(view)[0])
    assert int(match.group(6)) == 5  # 購入欄 = 購入 3 + 見送り 2
    assert int(match.group(7)) == 3  # うち購入 = 3(見送りを数えない)


def test_a_group_header_appears_once_per_message_however_many_items_it_holds() -> None:
    items = tuple(sell(f"{6000 + i}") for i in range(25))
    out = render(build(allocation=allocation_no_action(), risk_exit_items=items), budget=700)
    assert len(out) > 1
    for message in out:
        risk_headers = [x for x in lines_of(message) if section_of(x) == cli.HEADER_RISK]
        assert len(risk_headers) <= 1
    plain = sum(cli.HEADER_RISK in lines_of(m) for m in out)
    assert plain == 1  # 『（続き）』でない見出しは、群の最初の通に 1 回だけ


@pytest.mark.parametrize("budget", [200, 260, 400, 1000])
@pytest.mark.parametrize("note_length", [1, 120, 333, 900, 2500])
def test_an_oversized_item_never_breaks_the_limit_and_loses_nothing(
    budget: int, note_length: int
) -> None:
    huge = sell("0001", summary_reasons=("ぬ" * note_length,))
    view = build(allocation=allocation_no_action(), risk_exit_items=(huge,))
    out = render(view, budget=budget)
    assert all(len(m) <= budget for m in out), [len(m) for m in out]
    assert parse_items(out).count(("risk", ("0001",))) == 1
    assert sum(m.count("ぬ") for m in out) == note_length
