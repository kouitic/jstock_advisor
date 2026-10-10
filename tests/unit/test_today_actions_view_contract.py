"""Issue #605 / 605-A: 本日の投資アクションの表示モデル(V605-v1)の契約テスト。

純粋なデータ型と、出力の形(配分・入替の結果)から表示の状態を決める純粋関数だけを固定する。
renderer(605-B / C)・CLI(605-D)・永続化・通知・AWS は含まない。

## 何を固定するのか(USER の必須テスト 10 点のうち 605-A が負うもの)

```
1 NO_ACTION と UNAVAILABLE の厳密な区別(『見送り』を出せるのは評価した結果だけ)
2 既存の『緊急確認』等を RISK_EXIT などへ自動変換しない
3 『利益保全注意』を売却提案に変換しない
4 未確定の数量を 0・全株に置き換えない
5 Rotation の SELL と BUY を分離しない
6 Shadow 中の未承認の数値を確定した提案として持たない
```

型の制約(構築時に拒否する)と、builder の状態の写像を、表で固定する。
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import itertools
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.notification import today_actions_view as tv

D = Decimal
_REPO = Path(__file__).resolve().parents[2]
_MODULE_PATH = (
    _REPO / "src" / "jstock_advisor" / "domain" / "notification" / "today_actions_view.py"
)
_NOW = dt.datetime(2026, 10, 12, 9, 5, tzinfo=dt.timezone(dt.timedelta(hours=9)))
_EARLIER = dt.datetime(2026, 10, 5, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
_LEGACY_LABELS = ("緊急確認", "利益保全注意", "一部売却", "全部売却", "売却")


# --- 部品の作成 -------------------------------------------------------------------------------


def basis(price: str = "2340") -> tv.PriceBasis:
    return tv.PriceBasis(price=D(price), as_of=_NOW)


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


def sell(**over: object) -> tv.SellItemView:
    base: dict[str, object] = {
        "stock_code": "0001",
        "stock_name": "銘柄A",
        "sell_action": tv.SellAction.FULL,
        "exit_reason": tv.ExitReasonLabel.RISK_EXIT,
        "shares": 100,
        "estimated_amount": D("234000"),
        "price_basis": basis(),
        "summary_reasons": ("要点",),
    }
    base.update(over)
    return tv.SellItemView(**base)  # type: ignore[arg-type]


def legacy(label: str = "緊急確認", **over: object) -> tv.LegacyDecisionItemView:
    base: dict[str, object] = {
        "stock_code": "0009",
        "stock_name": "銘柄I",
        "legacy_label": label,
        "shares": None,
        "note": "売却理由は未分類",
    }
    base.update(over)
    return tv.LegacyDecisionItemView(**base)  # type: ignore[arg-type]


def leg(code: str = "0001", **over: object) -> tv.RotationLeg:
    base: dict[str, object] = {
        "stock_code": code,
        "stock_name": f"銘柄{code}",
        "shares": None,
        "estimated_amount": None,
        "price_basis": None,
    }
    base.update(over)
    return tv.RotationLeg(**base)  # type: ignore[arg-type]


def rotation_item(**over: object) -> tv.RotationItemView:
    base: dict[str, object] = {"sell_leg": leg("0001"), "buy_leg": leg("0002")}
    base.update(over)
    return tv.RotationItemView(**base)  # type: ignore[arg-type]


def buy(final: tv.BuyFinalAction = tv.BuyFinalAction.PURCHASE, **over: object) -> tv.BuyItemView:
    base: dict[str, object] = {
        "stock_code": "0002",
        "stock_name": "銘柄B",
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
        buys=skips or (buy(tv.BuyFinalAction.SKIP),),
    )


def allocation_unavailable(*reasons: tv.ViewReason) -> tv.AllocationOutcome:
    return tv.AllocationOutcome(
        status=tv.AllocationOutcomeStatus.UNAVAILABLE,
        unavailable_reasons=reasons or (tv.ViewReason.POLICY_INCOMPLETE,),
    )


def build(**over: object) -> tv.TodayActionsView:
    base: dict[str, object] = {
        "mode": tv.ViewMode.SHADOW,
        "owner": "owner-a",
        "evaluated_at": _NOW,
        "cash": cash(),
        "allocation": allocated(),
        # 既定は『入替も評価済みで提案なし』。未評価(None)を試すときは rotation=None を明示する
        "rotation": tv.RotationOutcome(status=tv.RotationOutcomeStatus.NO_ROTATION),
    }
    base.update(over)
    return tv.build_today_actions_view(**base)  # type: ignore[arg-type]


# --- 1 列挙と表示名 ---------------------------------------------------------------------------


def test_the_schema_version_is_fixed() -> None:
    assert tv.SCHEMA_VERSION == "V605-v1"


def test_the_exit_reason_labels_have_the_agreed_japanese_names() -> None:
    assert tv.ExitReasonLabel.RISK_EXIT.label == "リスク回避"
    assert tv.ExitReasonLabel.VALUE_EXIT.label == "割安の解消"
    assert tv.ExitReasonLabel.PROFIT_PROTECTION.label == "利益保全"
    assert tv.ExitReasonLabel.CAPITAL_ROTATION.label == "資金入替"


@pytest.mark.parametrize("enum_type", [tv.ViewReason, tv.SellAction, tv.ExitReasonLabel])
def test_every_member_has_a_stable_code_and_a_japanese_label(enum_type: type) -> None:
    for member in enum_type:
        assert member.value == member.name  # 内部コードは名前と同じ(機械処理用)
        assert member.label and member.label != member.value


def test_the_sell_actions_are_the_three_agreed_ones() -> None:
    assert {m.name for m in tv.SellAction} == {"PARTIAL", "FULL", "UNDETERMINED"}
    assert tv.SellAction.UNDETERMINED.label == "売却(数量未確定)"


def test_the_three_view_statuses_exist_and_are_distinct() -> None:
    assert {m.name for m in tv.ViewStatus} == {"ACTIONS", "NO_ACTION", "UNAVAILABLE"}


# --- 2 資金・価格の基準 -----------------------------------------------------------------------


def test_registered_cash_must_be_a_non_negative_decimal() -> None:
    assert cash(amount=D("0")).amount == D("0")  # 登録済みの 0 円は有効
    for bad in (D("-1"), 1000, 1000.5, "1000", True, D("NaN")):
        with pytest.raises(tv.TodayActionsViewError):
            cash(amount=bad)


def test_unregistered_cash_has_no_amount_and_is_unconfirmed() -> None:
    got = tv.CashView(
        amount=None,
        registered=False,
        last_reconciled_at=None,
        reconciliation_state=tv.CashReconciliationState.UNKNOWN,
    )
    assert got.amount is None
    for kwargs in (
        {"amount": D("0")},  # 未登録を 0 円にしない
        {"reconciliation_state": tv.CashReconciliationState.CONFIRMED},
        {"last_reconciled_at": _EARLIER},
    ):
        with pytest.raises(tv.TodayActionsViewError):
            tv.CashView(
                **{
                    "amount": None,
                    "registered": False,
                    "last_reconciled_at": None,
                    "reconciliation_state": tv.CashReconciliationState.UNKNOWN,
                    **kwargs,
                }  # type: ignore[arg-type]
            )


def test_reconciliation_state_and_timestamp_must_agree() -> None:
    with pytest.raises(tv.TodayActionsViewError):
        cash(last_reconciled_at=None)  # CONFIRMED なのに日時が無い
    with pytest.raises(tv.TodayActionsViewError):
        cash(reconciliation_state=tv.CashReconciliationState.UNKNOWN)  # UNKNOWN なのに日時がある
    with pytest.raises(tv.TodayActionsViewError):
        cash(last_reconciled_at=dt.datetime(2026, 10, 5, 15, 0))  # タイムゾーン無し


def test_price_basis_needs_a_positive_decimal_price_and_an_aware_time() -> None:
    for bad in (D("0"), D("-1"), 2340, 2340.5, D("NaN")):
        with pytest.raises(tv.TodayActionsViewError):
            tv.PriceBasis(price=bad, as_of=_NOW)  # type: ignore[arg-type]
    with pytest.raises(tv.TodayActionsViewError):
        tv.PriceBasis(price=D("2340"), as_of=dt.datetime(2026, 10, 12, 8, 40))


# --- 3 売却項目(必須テスト 3・4)-------------------------------------------------------------


def test_an_undetermined_sell_keeps_the_quantity_unknown() -> None:
    item = sell(sell_action=tv.SellAction.UNDETERMINED, shares=None, estimated_amount=None)
    assert item.shares is None and item.estimated_amount is None  # 0 でも全株でもない


@pytest.mark.parametrize(
    "over",
    [
        {"sell_action": tv.SellAction.UNDETERMINED, "shares": 100, "estimated_amount": None},
        {"sell_action": tv.SellAction.UNDETERMINED, "shares": None, "estimated_amount": D("1")},
        {"sell_action": tv.SellAction.FULL, "shares": None},  # 全部売却なのに株数が無い
        {"sell_action": tv.SellAction.PARTIAL, "shares": None},
        {"shares": 0},
        {"shares": -100},
        {"shares": True},
        {"shares": 100.0},
        {"estimated_amount": 234000},
        {"estimated_amount": 234000.5},
        {"estimated_amount": D("-1")},
        {"exit_reason": tv.ExitReasonLabel.CAPITAL_ROTATION},  # 入替は RotationItemView だけ
        {"stock_code": ""},
        {"summary_reasons": ["list"]},
    ],
)
def test_inconsistent_sell_items_are_rejected(over: dict[str, object]) -> None:
    with pytest.raises(tv.TodayActionsViewError):
        sell(**over)


def test_a_partial_sell_keeps_its_own_quantity() -> None:
    item = sell(sell_action=tv.SellAction.PARTIAL, shares=300)
    assert item.sell_action is tv.SellAction.PARTIAL and item.shares == 300


def test_the_exit_reason_is_a_separate_field_from_the_sell_action() -> None:
    fields = {f.name for f in dataclasses.fields(tv.SellItemView)}
    assert {"sell_action", "exit_reason"} <= fields


# --- 4 既存の判定(必須テスト 2・3)-----------------------------------------------------------


@pytest.mark.parametrize("label", _LEGACY_LABELS)
def test_legacy_decisions_stay_in_the_legacy_group_and_are_never_reclassified(
    label: str,
) -> None:
    view = build(allocation=allocation_no_action(), legacy_items=(legacy(label),))
    assert [i.legacy_label for i in view.legacy_items] == [label]
    assert view.risk_exit_items == () and view.value_exit_items == ()
    assert view.profit_protection_items == ()
    assert view.rotation_items == ()


def test_a_legacy_item_has_no_sell_action_or_exit_reason() -> None:
    fields = {f.name for f in dataclasses.fields(tv.LegacyDecisionItemView)}
    assert "sell_action" not in fields and "exit_reason" not in fields
    assert fields == {"stock_code", "stock_name", "legacy_label", "shares", "note"}


def test_a_legacy_item_does_not_make_the_day_an_actionable_day() -> None:
    # 利益保全注意などの既存の判定は注意喚起であり、それだけでは提案ありにならない
    view = build(allocation=allocation_no_action(), legacy_items=(legacy("利益保全注意"),))
    assert view.status is tv.ViewStatus.NO_ACTION
    assert len(view.legacy_items) == 1


def test_legacy_items_are_kept_when_the_evaluation_is_unavailable() -> None:
    view = build(allocation=allocation_unavailable(), legacy_items=(legacy("緊急確認"),))
    assert view.status is tv.ViewStatus.UNAVAILABLE
    assert [i.legacy_label for i in view.legacy_items] == ["緊急確認"]  # 情報を落とさない


def test_the_module_has_no_table_from_legacy_labels_to_exit_reasons() -> None:
    # 分類を要する既存のラベルの文字列を、このモジュールのコードが持たない(= 対応表も分岐も作れない)
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    literals = {
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    }
    # 『一部売却』『全部売却』は SellAction の表示名として正当に現れる。変換の元になりうる、
    # 分類を要する既存のラベルだけを禁止する
    for label in ("緊急確認", "利益保全注意"):
        assert label not in literals


# --- 5 状態の区別(必須テスト 1)---------------------------------------------------------------


def test_actions_need_at_least_one_actionable_item() -> None:
    with pytest.raises(tv.TodayActionsViewError):
        view_with(status=tv.ViewStatus.ACTIONS)


def view_with(**over: object) -> tv.TodayActionsView:
    base: dict[str, object] = {
        "mode": tv.ViewMode.SHADOW,
        "owner": "owner-a",
        "evaluated_at": _NOW,
        "cash": cash(),
        "status": tv.ViewStatus.NO_ACTION,
        "reasons": (),
        "rotation_evaluated": True,
    }
    base.update(over)
    return tv.TodayActionsView(**base)  # type: ignore[arg-type]


def test_no_action_cannot_carry_a_proposal_or_a_reason() -> None:
    for over in (
        {"buy_items": (buy(),)},
        {"risk_exit_items": (sell(),)},
        {"value_exit_items": (sell(exit_reason=tv.ExitReasonLabel.VALUE_EXIT),)},
        {"profit_protection_items": (sell(exit_reason=tv.ExitReasonLabel.PROFIT_PROTECTION),)},
        {"rotation_items": (rotation_item(),)},
        {"reasons": (tv.ViewReason.POLICY_INCOMPLETE,)},
    ):
        with pytest.raises(tv.TodayActionsViewError):
            view_with(status=tv.ViewStatus.NO_ACTION, **over)


def test_no_action_may_carry_skipped_buys_and_legacy_items_as_information() -> None:
    view = view_with(
        status=tv.ViewStatus.NO_ACTION,
        buy_items=(buy(tv.BuyFinalAction.SKIP),),
        legacy_items=(legacy(),),
    )
    assert view.status is tv.ViewStatus.NO_ACTION


def test_unavailable_needs_reasons_and_cannot_carry_a_proposal() -> None:
    with pytest.raises(tv.TodayActionsViewError):
        view_with(status=tv.ViewStatus.UNAVAILABLE, reasons=())
    for over in (
        {"buy_items": (buy(),)},
        {"risk_exit_items": (sell(),)},
        {"rotation_items": (rotation_item(),)},
    ):
        with pytest.raises(tv.TodayActionsViewError):
            view_with(
                status=tv.ViewStatus.UNAVAILABLE,
                reasons=(tv.ViewReason.NOT_EVALUATED,),
                **over,
            )


def test_a_missing_evaluation_is_unavailable_never_a_skip() -> None:
    view = build(allocation=None)
    assert view.status is tv.ViewStatus.UNAVAILABLE
    assert view.reasons == (tv.ViewReason.NOT_EVALUATED,)


@pytest.mark.parametrize("reason", list(tv.ViewReason))
def test_an_unavailable_allocation_is_unavailable_for_every_reason(reason: tv.ViewReason) -> None:
    view = build(allocation=allocation_unavailable(reason))
    assert view.status is tv.ViewStatus.UNAVAILABLE
    assert view.reasons == (reason,)
    assert view.buy_items == ()


def test_a_policy_that_is_not_set_is_unavailable_and_never_no_action() -> None:
    view = build(allocation=allocation_unavailable(tv.ViewReason.POLICY_INCOMPLETE))
    assert view.status is not tv.ViewStatus.NO_ACTION


def test_an_evaluated_day_with_nothing_to_buy_is_no_action() -> None:
    view = build(allocation=allocation_no_action())
    assert view.status is tv.ViewStatus.NO_ACTION
    assert view.reasons == ()
    assert [b.final_action for b in view.buy_items] == [tv.BuyFinalAction.SKIP]


def test_a_purchase_makes_the_day_an_actions_day() -> None:
    view = build(allocation=allocated())
    assert view.status is tv.ViewStatus.ACTIONS
    assert [b.final_action for b in view.buy_items] == [tv.BuyFinalAction.PURCHASE]


def test_skipped_candidates_are_kept_beside_purchases() -> None:
    view = build(allocation=allocated(buy(), buy(tv.BuyFinalAction.SKIP, stock_code="0003")))
    assert [b.final_action for b in view.buy_items] == [
        tv.BuyFinalAction.PURCHASE,
        tv.BuyFinalAction.SKIP,
    ]


def test_exit_items_make_an_actions_day_even_when_allocation_is_unavailable() -> None:
    view = build(
        allocation=allocation_unavailable(tv.ViewReason.CASH_NOT_REGISTERED),
        risk_exit_items=(sell(),),
    )
    assert view.status is tv.ViewStatus.ACTIONS
    assert view.reasons == (tv.ViewReason.CASH_NOT_REGISTERED,)  # 部分的に不能な事実を残す


def test_an_unavailable_rotation_does_not_turn_a_no_action_day_into_a_skip() -> None:
    rotation = tv.RotationOutcome(
        status=tv.RotationOutcomeStatus.UNAVAILABLE,
        unavailable_reasons=(tv.ViewReason.BATCH_NOT_COMPLETED,),
    )
    view = build(allocation=allocation_no_action(), rotation=rotation)
    assert view.status is tv.ViewStatus.UNAVAILABLE
    assert view.reasons == (tv.ViewReason.BATCH_NOT_COMPLETED,)


def test_the_reasons_of_allocation_and_rotation_are_merged_without_duplicates() -> None:
    rotation = tv.RotationOutcome(
        status=tv.RotationOutcomeStatus.UNAVAILABLE,
        unavailable_reasons=(tv.ViewReason.POLICY_INCOMPLETE, tv.ViewReason.RAER_NOT_COMPARABLE),
    )
    view = build(
        allocation=allocation_unavailable(
            tv.ViewReason.POLICY_INCOMPLETE, tv.ViewReason.CASH_NOT_REGISTERED
        ),
        rotation=rotation,
    )
    assert view.reasons == (
        tv.ViewReason.POLICY_INCOMPLETE,
        tv.ViewReason.CASH_NOT_REGISTERED,
        tv.ViewReason.RAER_NOT_COMPARABLE,
    )


@pytest.mark.parametrize(
    ("alloc_kind", "rotation_status"),
    list(
        itertools.product(
            ["none", "allocated", "no_action", "unavailable"],
            [None, *tv.RotationOutcomeStatus],
        )
    ),
)
def test_the_state_matrix_never_shows_a_skip_for_an_unevaluated_day(
    alloc_kind: str, rotation_status: tv.RotationOutcomeStatus | None
) -> None:
    allocation = {
        "none": None,
        "allocated": allocated(),
        "no_action": allocation_no_action(),
        "unavailable": allocation_unavailable(),
    }[alloc_kind]
    rotation = None
    if rotation_status is not None:
        items = (
            (rotation_item(),)
            if rotation_status is tv.RotationOutcomeStatus.ROTATION_PROPOSED
            else ()
        )
        reasons = (
            (tv.ViewReason.NOT_EVALUATED,)
            if rotation_status is tv.RotationOutcomeStatus.UNAVAILABLE
            else ()
        )
        rotation = tv.RotationOutcome(
            status=rotation_status, items=items, unavailable_reasons=reasons
        )
    view = build(allocation=allocation, rotation=rotation)
    if view.status is tv.ViewStatus.NO_ACTION:
        # 『見送り』を出せるのは、配分と入替の両方が評価済みで、どちらも不能でないときだけ
        assert alloc_kind in {"no_action"}
        assert rotation_status in {
            tv.RotationOutcomeStatus.NO_ROTATION,
            tv.RotationOutcomeStatus.SHADOW_ONLY,
        }
    if rotation_status is None:
        assert tv.ViewReason.ROTATION_NOT_EVALUATED in view.reasons
        assert view.rotation_evaluated is False
    if alloc_kind in {"none", "unavailable"} and not view.rotation_items:
        assert view.status is tv.ViewStatus.UNAVAILABLE


def test_outcome_types_reject_inconsistent_construction() -> None:
    alloc, rot = tv.AllocationOutcome, tv.RotationOutcome
    a_st, r_st = tv.AllocationOutcomeStatus, tv.RotationOutcomeStatus
    for make in (
        lambda: alloc(status=a_st.ALLOCATED_SOME, buys=()),
        lambda: alloc(status=a_st.ALLOCATED_SOME, buys=(buy(tv.BuyFinalAction.SKIP),)),
        lambda: alloc(status=a_st.NO_ACTION, buys=(buy(),)),
        lambda: alloc(
            status=a_st.NO_ACTION, buys=(), unavailable_reasons=(tv.ViewReason.NOT_EVALUATED,)
        ),
        lambda: alloc(status=a_st.UNAVAILABLE, unavailable_reasons=()),
        lambda: alloc(
            status=a_st.UNAVAILABLE,
            unavailable_reasons=(tv.ViewReason.NOT_EVALUATED,),
            buys=(buy(),),
        ),
        lambda: rot(status=r_st.ROTATION_PROPOSED, items=()),
        lambda: rot(status=r_st.SHADOW_ONLY, items=(rotation_item(),)),
        lambda: rot(status=r_st.NO_ROTATION, items=(rotation_item(),)),
        lambda: rot(status=r_st.UNAVAILABLE, unavailable_reasons=()),
    ):
        with pytest.raises(tv.TodayActionsViewError):
            make()


# --- 6 購入項目 -------------------------------------------------------------------------------


def test_a_buy_item_keeps_the_standalone_judgement_apart_from_the_final_action() -> None:
    item = buy(tv.BuyFinalAction.SKIP)
    assert item.standalone_judgement == "買い候補"
    assert item.final_action is tv.BuyFinalAction.SKIP
    assert item.skip_reason_code and item.skip_reason_label


@pytest.mark.parametrize(
    "over",
    [
        {"final": tv.BuyFinalAction.SKIP, "skip_reason_code": None},
        {"final": tv.BuyFinalAction.SKIP, "skip_reason_label": None},
        {"final": tv.BuyFinalAction.SKIP, "shares": 100},
        {"final": tv.BuyFinalAction.PURCHASE, "skip_reason_code": "X", "skip_reason_label": "x"},
        {"final": tv.BuyFinalAction.PURCHASE, "shares": None},
        {"final": tv.BuyFinalAction.PURCHASE, "shares": 0},
        {"final": tv.BuyFinalAction.PURCHASE, "shares": True},
        {"final": tv.BuyFinalAction.PURCHASE, "estimated_amount": 468000},
        {"final": tv.BuyFinalAction.PURCHASE, "standalone_judgement": ""},
    ],
)
def test_inconsistent_buy_items_are_rejected(over: dict[str, object]) -> None:
    final = over.pop("final")
    with pytest.raises(tv.TodayActionsViewError):
        buy(final, **over)  # type: ignore[arg-type]


# --- 7 資金入替(必須テスト 5・6)--------------------------------------------------------------


def test_a_rotation_item_always_pairs_a_sell_leg_with_a_buy_leg() -> None:
    item = rotation_item()
    assert item.sell_leg.stock_code == "0001" and item.buy_leg.stock_code == "0002"
    for bad in ({"sell_leg": None}, {"buy_leg": None}, {"sell_leg": "0001"}):
        with pytest.raises(tv.TodayActionsViewError):
            rotation_item(**bad)


def test_the_rotation_condition_text_is_fixed() -> None:
    assert rotation_item().condition_text == "売却しない場合、この購入は提案されません"
    with pytest.raises(tv.TodayActionsViewError):
        rotation_item(condition_text="別の文")


def test_unconfirmed_rotation_numbers_are_not_held() -> None:
    # numbers_confirmed が False の間は、改善幅・算入割合・売買数量を持てない
    assert rotation_item().numbers_confirmed is False
    for over in (
        {"improvement": D("0.5")},
        {"counted_ratio": D("0.8")},
        {"sell_leg": leg("0001", shares=100)},
        {"buy_leg": leg("0002", estimated_amount=D("1000"))},
    ):
        with pytest.raises(tv.TodayActionsViewError):
            rotation_item(**over)


def test_confirmed_rotation_numbers_need_both_quantities() -> None:
    ok = rotation_item(
        numbers_confirmed=True,
        improvement=D("0.5"),
        counted_ratio=D("0.8"),
        sell_leg=leg("0001", shares=100, estimated_amount=D("100000"), price_basis=basis("1000")),
        buy_leg=leg("0002", shares=50, estimated_amount=D("100000"), price_basis=basis("2000")),
    )
    assert ok.numbers_confirmed is True
    with pytest.raises(tv.TodayActionsViewError):
        rotation_item(numbers_confirmed=True, improvement=D("0.5"), counted_ratio=D("0.8"))
    with pytest.raises(tv.TodayActionsViewError):
        rotation_item(numbers_confirmed=True, improvement=0.5, counted_ratio=D("0.8"))  # float


def test_shadow_mode_rejects_confirmed_rotation_numbers() -> None:
    confirmed = rotation_item(
        numbers_confirmed=True,
        improvement=D("0.5"),
        counted_ratio=D("0.8"),
        sell_leg=leg("0001", shares=100, estimated_amount=D("100000"), price_basis=basis("1000")),
        buy_leg=leg("0002", shares=50, estimated_amount=D("100000"), price_basis=basis("2000")),
    )
    outcome = tv.RotationOutcome(
        status=tv.RotationOutcomeStatus.ROTATION_PROPOSED, items=(confirmed,)
    )
    with pytest.raises(tv.TodayActionsViewError):
        build(rotation=outcome)


def test_a_proposed_rotation_is_shown_as_a_pair_in_one_item() -> None:
    outcome = tv.RotationOutcome(
        status=tv.RotationOutcomeStatus.ROTATION_PROPOSED, items=(rotation_item(),)
    )
    view = build(allocation=allocation_no_action(), rotation=outcome)
    assert view.status is tv.ViewStatus.ACTIONS
    assert len(view.rotation_items) == 1
    assert view.rotation_items[0].sell_leg.stock_code != view.rotation_items[0].buy_leg.stock_code


def test_a_shadow_only_rotation_is_not_a_proposal() -> None:
    outcome = tv.RotationOutcome(status=tv.RotationOutcomeStatus.SHADOW_ONLY)
    view = build(allocation=allocation_no_action(), rotation=outcome)
    assert view.rotation_items == ()
    assert view.status is tv.ViewStatus.NO_ACTION
    assert view.rotation_evaluated is True


def test_an_unevaluated_rotation_adds_its_own_reason_code() -> None:
    # 既存の NOT_EVALUATED(該当日の評価記録なし)とは別の理由コード・別の日本語ラベル
    reason = tv.ViewReason.ROTATION_NOT_EVALUATED
    assert reason is not tv.ViewReason.NOT_EVALUATED
    assert reason.label != tv.ViewReason.NOT_EVALUATED.label
    assert "入替" in reason.label
    view = build(allocation=allocated(), rotation=None)
    assert view.status is tv.ViewStatus.ACTIONS  # 提案があれば提案は出す
    assert view.reasons == (reason,)  # ただし未評価の旨を残す


def test_no_action_needs_both_the_allocation_and_the_rotation_to_be_evaluated() -> None:
    # 配分が NO_ACTION でも、入替が未評価なら見送りにしない(U-7: 未評価を見送りと混同しない)
    unevaluated = build(allocation=allocation_no_action(), rotation=None)
    assert unevaluated.status is tv.ViewStatus.UNAVAILABLE
    assert unevaluated.reasons == (tv.ViewReason.ROTATION_NOT_EVALUATED,)
    evaluated = build(
        allocation=allocation_no_action(),
        rotation=tv.RotationOutcome(status=tv.RotationOutcomeStatus.NO_ROTATION),
    )
    assert evaluated.status is tv.ViewStatus.NO_ACTION
    assert evaluated.reasons == ()


def test_both_unevaluated_reasons_are_kept_when_nothing_was_evaluated() -> None:
    both = build(allocation=allocation_unavailable(tv.ViewReason.POLICY_INCOMPLETE), rotation=None)
    assert both.status is tv.ViewStatus.UNAVAILABLE
    assert both.reasons == (
        tv.ViewReason.POLICY_INCOMPLETE,
        tv.ViewReason.ROTATION_NOT_EVALUATED,
    )
    missing = build(allocation=None, rotation=None)
    assert missing.reasons == (
        tv.ViewReason.NOT_EVALUATED,
        tv.ViewReason.ROTATION_NOT_EVALUATED,
    )


def test_a_no_action_view_cannot_be_built_with_an_unevaluated_rotation() -> None:
    with pytest.raises(tv.TodayActionsViewError):
        view_with(status=tv.ViewStatus.NO_ACTION, rotation_evaluated=False)


def test_a_rotation_that_was_not_evaluated_is_recorded_as_such() -> None:
    assert build(rotation=None).rotation_evaluated is False


# --- 8 モード ---------------------------------------------------------------------------------


def test_active_mode_cannot_be_built_without_the_explicit_permission() -> None:
    with pytest.raises(tv.TodayActionsViewError):
        build(mode=tv.ViewMode.ACTIVE)
    assert build(mode=tv.ViewMode.ACTIVE, allow_active=True).mode is tv.ViewMode.ACTIVE


def test_the_view_object_itself_cannot_be_active_without_the_permission() -> None:
    with pytest.raises(tv.TodayActionsViewError):
        view_with(mode=tv.ViewMode.ACTIVE)


# --- 9 見込み残高(U-10)----------------------------------------------------------------------


def test_the_remaining_cash_basis_follows_the_reconciliation_state() -> None:
    confirmed = build(projected_remaining_cash=D("344400"))
    assert confirmed.remaining_cash_basis is tv.RemainingCashBasis.ESTIMATE
    unconfirmed = build(cash=unconfirmed_cash(), projected_remaining_cash=D("344400"))
    assert unconfirmed.remaining_cash_basis is tv.RemainingCashBasis.ESTIMATE_FROM_UNCONFIRMED_CASH
    assert build().remaining_cash_basis is None


def test_a_remaining_cash_must_be_a_non_negative_decimal() -> None:
    for bad in (D("-1"), 344400, 344400.5):
        with pytest.raises(tv.TodayActionsViewError):
            build(projected_remaining_cash=bad)


def test_the_unchanged_holdings_count_is_a_non_negative_int_or_none() -> None:
    assert build(unchanged_holdings_count=9).unchanged_holdings_count == 9
    assert build().unchanged_holdings_count is None
    for bad in (-1, True, 1.5):
        with pytest.raises(tv.TodayActionsViewError):
            build(unchanged_holdings_count=bad)


# --- 10 識別子・時刻・不変性 ------------------------------------------------------------------


def test_the_owner_and_time_are_validated() -> None:
    for bad in ("", " owner-a", "owner a"):
        with pytest.raises(tv.TodayActionsViewError):
            build(owner=bad)
    with pytest.raises(tv.TodayActionsViewError):
        build(evaluated_at=dt.datetime(2026, 10, 12, 9, 5))


def test_the_contract_versions_and_keys_are_kept_for_machine_use() -> None:
    view = build(
        contract_versions={"allocation": "C603-v1.2", "rotation": "C604-v1.1"},
        model_versions=("m1",),
        comparison_keys=("k1",),
    )
    assert view.schema_version == "V605-v1"
    assert dict(view.contract_versions) == {"allocation": "C603-v1.2", "rotation": "C604-v1.1"}
    assert view.model_versions == ("m1",) and view.comparison_keys == ("k1",)


def test_views_are_immutable_and_tuples_only() -> None:
    view = build()
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.owner = "owner-b"  # type: ignore[misc]
    for name in ("risk_exit_items", "buy_items", "rotation_items", "legacy_items", "reasons"):
        assert isinstance(getattr(view, name), tuple)
    with pytest.raises(tv.TodayActionsViewError):
        build(risk_exit_items=[sell()])  # list は拒否


def test_the_item_order_given_by_the_caller_is_preserved() -> None:
    buys = (buy(stock_code="0007"), buy(stock_code="0003"), buy(stock_code="0005"))
    view = build(allocation=allocated(*buys))
    assert [b.stock_code for b in view.buy_items] == ["0007", "0003", "0005"]


def test_the_builder_is_deterministic() -> None:
    assert build() == build()


# --- 11 純粋性・休眠(AST)-------------------------------------------------------------------

_ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
    "typing",
}


def _module_tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def test_the_module_imports_only_the_standard_library_pieces_it_needs() -> None:
    found: set[str] = set()
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    assert found <= _ALLOWED_IMPORTS, found - _ALLOWED_IMPORTS


def test_the_module_reads_no_clock_and_uses_no_float_or_io() -> None:
    forbidden_calls = {"now", "utcnow", "today", "open", "print", "input"}
    for node in ast.walk(_module_tree()):
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
        text = path.read_text(encoding="utf-8")
        assert "today_actions_view" not in text, str(path)


def test_each_exit_group_only_holds_items_of_its_own_reason() -> None:
    wrong = sell(exit_reason=tv.ExitReasonLabel.VALUE_EXIT)
    for group in ("risk_exit_items", "profit_protection_items"):
        with pytest.raises(tv.TodayActionsViewError):
            build(**{group: (wrong,)})
    with pytest.raises(tv.TodayActionsViewError):
        build(value_exit_items=(sell(),))  # RISK_EXIT を割安の解消の群へ入れない


def test_an_item_of_the_wrong_type_is_rejected_in_every_group() -> None:
    with pytest.raises(tv.TodayActionsViewError):
        build(allocation=allocated(), legacy_items=(sell(),))  # type: ignore[arg-type]
    with pytest.raises(tv.TodayActionsViewError):
        build(risk_exit_items=(legacy(),))  # type: ignore[arg-type]
    with pytest.raises(tv.TodayActionsViewError):
        view_with(
            status=tv.ViewStatus.NO_ACTION,
            buy_items=(sell(),),  # type: ignore[arg-type]
        )
    with pytest.raises(tv.TodayActionsViewError):
        build(model_versions=(1,))  # type: ignore[arg-type]
    with pytest.raises(tv.TodayActionsViewError):
        build(comparison_keys=("k", None))  # type: ignore[arg-type]


def test_the_reasons_must_not_repeat_and_the_schema_version_is_fixed() -> None:
    twice = (tv.ViewReason.NOT_EVALUATED, tv.ViewReason.NOT_EVALUATED)
    with pytest.raises(tv.TodayActionsViewError):
        view_with(status=tv.ViewStatus.UNAVAILABLE, reasons=twice)
    with pytest.raises(tv.TodayActionsViewError):
        view_with(schema_version="V605-v2")
    with pytest.raises(tv.TodayActionsViewError):
        view_with(contract_versions=(("allocation",),))  # type: ignore[arg-type]


def test_a_confirmed_counted_ratio_is_a_ratio() -> None:
    common = {
        "numbers_confirmed": True,
        "improvement": D("0.5"),
        "sell_leg": leg("0001", shares=100, estimated_amount=D("100000")),
        "buy_leg": leg("0002", shares=50, estimated_amount=D("100000")),
    }
    assert rotation_item(counted_ratio=D("1"), **common).counted_ratio == D("1")
    for bad in (D("1.01"), D("-0.1")):
        with pytest.raises(tv.TodayActionsViewError):
            rotation_item(counted_ratio=bad, **common)


def test_a_rotation_item_implies_that_the_rotation_was_evaluated() -> None:
    with pytest.raises(tv.TodayActionsViewError):
        view_with(
            status=tv.ViewStatus.ACTIONS,
            rotation_items=(rotation_item(),),
            rotation_evaluated=False,
        )


def test_an_allocation_with_purchases_carries_no_unavailable_reason() -> None:
    with pytest.raises(tv.TodayActionsViewError):
        tv.AllocationOutcome(
            status=tv.AllocationOutcomeStatus.ALLOCATED_SOME,
            buys=(buy(),),
            unavailable_reasons=(tv.ViewReason.NOT_EVALUATED,),
        )


def test_the_remaining_cash_basis_cannot_disagree_with_the_cash_state() -> None:
    with pytest.raises(tv.TodayActionsViewError):
        view_with(
            projected_remaining_cash=D("1"),
            remaining_cash_basis=tv.RemainingCashBasis.ESTIMATE_FROM_UNCONFIRMED_CASH,
        )  # 棚卸し済みなのに『未確認の残高に基づく』
    with pytest.raises(tv.TodayActionsViewError):
        view_with(
            cash=unconfirmed_cash(),
            projected_remaining_cash=D("1"),
            remaining_cash_basis=tv.RemainingCashBasis.ESTIMATE,
        )  # 未確認なのに通常の見込み
    with pytest.raises(tv.TodayActionsViewError):
        view_with(remaining_cash_basis=tv.RemainingCashBasis.ESTIMATE)  # 値なしで根拠だけ


@pytest.mark.parametrize(
    ("group", "reason"),
    [
        ("risk_exit_items", tv.ExitReasonLabel.RISK_EXIT),
        ("value_exit_items", tv.ExitReasonLabel.VALUE_EXIT),
        ("profit_protection_items", tv.ExitReasonLabel.PROFIT_PROTECTION),
    ],
)
def test_each_exit_group_alone_makes_an_actions_day(group: str, reason: tv.ExitReasonLabel) -> None:
    view = build(allocation=allocation_no_action(), **{group: (sell(exit_reason=reason),)})
    assert view.status is tv.ViewStatus.ACTIONS
    assert len(getattr(view, group)) == 1
