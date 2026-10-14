"""Issue #603 / PR-A: 配分(Portfolio Allocation)の型・方針の完全性の契約テスト(C603-v1.3)。

型と不変条件だけを固定する。計算(optimizer)・閾値の決定・専用 YAML・入力の組み立て・
資金入替は含まない(PR-C / PR-E / PR-F / PR-D)。どこからも import されない(dormant)。

## 何を固定するのか

```
・数値の方針(hurdle・各上限・段階の割合)と比較可能性の鍵が未決定(None)のあいだ、
  方針は『不完全』で、配分を持てない(USER の決定: None = POLICY_INCOMPLETE → 配分しない)。
  完全性は AllocationPolicy.missing() が判定する
・結果の型は矛盾した組合せを作れない: ALLOCATED <=> shares > 0 <=> reason = ALLOCATED /
  UNAVAILABLE のとき配分なし(方針不完全のときだけ『順位だけの shadow』)/
  方針が不完全なら配分 0 の結果しか作れない
・入力の検証(重複でない stock_code・価格 > 0・BUY 系の action のみ・負の評価額の拒否・
  比率は (0, 1])と、例外のメッセージに値(金額・株数)を含めないこと(#909 の SHOULD-1 の教訓)
```
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import itertools
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import BUY_FAMILY_ACTIONS, BuyAction, ConfidenceLevel
from jstock_advisor.domain.signals import portfolio_allocation_types as pt
from jstock_advisor.domain.valuation.raer import (
    MODEL_VERSION,
    AxisCode,
    RaerResult,
    RaerState,
    RiskFlag,
    make_comparison_key,
)

D = Decimal
_REPO = Path(__file__).resolve().parents[2]
_MODULE_PATH = (
    _REPO / "src" / "jstock_advisor" / "domain" / "signals" / "portfolio_allocation_types.py"
)
_JST = dt.timezone(dt.timedelta(hours=9))
_AS_OF = dt.datetime(2026, 10, 12, 8, 40, tzinfo=_JST)
_ALL_AXES = frozenset(a.value for a in AxisCode)
# 値が他の文字列と衝突しない数字列(メッセージに値が現れないことの検査に使う)
_DISTINCT = "424242.5"


# --- 部品 -------------------------------------------------------------------------------------


def raer(
    state: RaerState = RaerState.AVAILABLE,
    value: str = "5",
    confidence: ConfidenceLevel | None = ConfidenceLevel.HIGH,
    risk_flags: frozenset[str] = frozenset(),
) -> RaerResult:
    """型が許す合成データ。5 軸の分割と comparison_key の規則を満たす(UNAVAILABLE は値なし)。"""
    if state is RaerState.UNAVAILABLE:
        return RaerResult(
            state=state,
            value=None,
            confidence=None,
            risk_flags=frozenset(),
            risk_flags_available=False,
            model_version=MODEL_VERSION,
            comparison_key=None,
            axes_applied=frozenset(),
            axes_not_applied=_ALL_AXES,
            reason_codes=("COMPOSITION_RULE_NOT_DECIDED",),
        )
    if state is RaerState.AVAILABLE:
        applied, not_applied = _ALL_AXES, frozenset()
    else:
        not_applied = frozenset({AxisCode.EARNINGS_TREND.value})
        applied = _ALL_AXES - not_applied
    return RaerResult(
        state=state,
        value=D(value),
        confidence=confidence,
        risk_flags=risk_flags,
        risk_flags_available=AxisCode.BUSINESS_RISK.value in applied,
        model_version=MODEL_VERSION,
        comparison_key=make_comparison_key(MODEL_VERSION, applied),
        axes_applied=applied,
        axes_not_applied=not_applied,
        reason_codes=() if state is RaerState.AVAILABLE else ("AXIS_NOT_IMPLEMENTED",),
    )


def candidate(**over: object) -> pt.AllocationCandidate:
    base: dict[str, object] = {
        "stock_code": "0002",
        "kind": pt.CandidateKind.NEW_BUY,
        "buy_action": BuyAction.BUY,
        "current_price": D("2340"),
        "price_as_of": _AS_OF,
        "price_tier": pt.PriceTier.STANDARD,
        "raer": raer(),
        "current_position_value": D("0"),
        "sector": "電気機器",
    }
    base.update(over)
    return pt.AllocationCandidate(**base)  # type: ignore[arg-type]


def constraints(**over: object) -> pt.AllocationConstraints:
    base: dict[str, object] = {
        "max_single_stock_amount": D("500000"),
        "max_single_stock_ratio": D("0.2"),
        "max_sector_ratio": D("0.4"),
        "max_high_risk_ratio": D("0.1"),
        "high_risk_flags": frozenset({RiskFlag.CYCLICAL_INDUSTRY.value}),
        "minimum_trading_unit": 100,
    }
    base.update(over)
    return pt.AllocationConstraints(**base)  # type: ignore[arg-type]


def complete_policy(**over: object) -> pt.AllocationPolicy:
    base: dict[str, object] = {
        "min_raer_to_allocate": D("3"),
        "comparison_key": raer().comparison_key,
        "tier_quantity_caps": {
            pt.PriceTier.ENTRY: D("0.3"),
            pt.PriceTier.STANDARD: D("0.6"),
            pt.PriceTier.STRONG: D("1"),
        },
    }
    base.update(over)
    return pt.AllocationPolicy(**base)  # type: ignore[arg-type]


def not_allocated(
    code: str = "0002", reason: pt.ReasonCode = pt.ReasonCode.BELOW_HURDLE, **over: object
) -> pt.CandidateAllocation:
    base: dict[str, object] = {
        "stock_code": code,
        "decision": pt.CandidateDecision.NOT_ALLOCATED,
        "shares": 0,
        "amount": D("0"),
        "price_tier_used": None,
        "reason_code": reason,
        "rank": None,
        "cap_limited_shares": 0,
        "cash_shortfall": D("0"),
    }
    if reason not in (
        pt.ReasonCode.RAER_UNAVAILABLE,
        pt.ReasonCode.BELOW_HURDLE,
        pt.ReasonCode.RAER_NOT_COMPARABLE,
    ):
        base["rank"] = 1
    base.update(over)
    return pt.CandidateAllocation(**base)  # type: ignore[arg-type]


def allocated(
    code: str = "0002", shares: int = 200, rank: int = 1, **over: object
) -> pt.CandidateAllocation:
    base: dict[str, object] = {
        "stock_code": code,
        "decision": pt.CandidateDecision.ALLOCATED,
        "shares": shares,
        "amount": D("2340") * shares if isinstance(shares, int) else D("1"),
        "price_tier_used": pt.PriceTier.STANDARD,
        "reason_code": pt.ReasonCode.ALLOCATED,
        "rank": rank,
        "cap_limited_shares": shares,
        "cash_shortfall": D("0"),
    }
    base.update(over)
    return pt.CandidateAllocation(**base)  # type: ignore[arg-type]


def result(**over: object) -> pt.AllocationResult:
    items = (allocated("0002", 200, 1), not_allocated("0003", pt.ReasonCode.BELOW_HURDLE))
    base: dict[str, object] = {
        "owner": "owner-a",
        "status": pt.AllocationStatus.ALLOCATED_SOME,
        "unavailable_reason": None,
        "allocations": items,
        "total_allocated": D("468000"),
        "remaining_cash": D("300000"),
        "policy_missing": (),
    }
    base.update(over)
    return pt.AllocationResult(**base)  # type: ignore[arg-type]


# --- 1 列挙 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "enum_type",
    [
        pt.CandidateKind,
        pt.PriceTier,
        pt.AllocationStatus,
        pt.UnavailableReason,
        pt.CandidateDecision,
        pt.ReasonCode,
    ],
)
def test_every_enum_member_has_a_stable_code_equal_to_its_name(enum_type: type) -> None:
    for member in enum_type:
        assert member.value == member.name


def test_the_enums_have_exactly_the_agreed_members() -> None:
    assert {m.name for m in pt.CandidateKind} == {"NEW_BUY", "ADDITIONAL_BUY"}
    assert {m.name for m in pt.PriceTier} == {"ENTRY", "STANDARD", "STRONG"}
    assert {m.name for m in pt.AllocationStatus} == {"ALLOCATED_SOME", "NO_ACTION", "UNAVAILABLE"}
    assert {m.name for m in pt.UnavailableReason} == {
        "CASH_NOT_REGISTERED",
        "PORTFOLIO_VALUE_UNAVAILABLE",
        "RAER_NOT_COMPARABLE",
        "POLICY_INCOMPLETE",
    }
    assert {m.name for m in pt.CandidateDecision} == {"ALLOCATED", "NOT_ALLOCATED"}
    assert {m.name for m in pt.ReasonCode} == {
        "ALLOCATED",
        "RAER_UNAVAILABLE",
        "POLICY_INCOMPLETE",
        "BELOW_HURDLE",
        "INSUFFICIENT_CASH_REMAINING",
        "BELOW_MINIMUM_TRADING_UNIT",
        "POSITION_AMOUNT_CAP_APPLIED",
        "POSITION_RATIO_CAP_APPLIED",
        "SECTOR_RATIO_CAP_APPLIED",
        "SECTOR_UNKNOWN",
        "HIGH_RISK_ALLOCATION_CAP_APPLIED",
        "HIGH_RISK_UNKNOWN",
        "TIER_CAP_APPLIED",
        "RAER_NOT_COMPARABLE",  # #603 段 4(診断用。順位ではない)
    }


def test_the_contract_version_is_fixed() -> None:
    # v1.3 = ReasonCode に RAER_NOT_COMPARABLE を追加(語彙の追加は版を上げる規約)
    assert pt.CONTRACT_VERSION == "C603-v1.3"
    assert result().contract_version == "C603-v1.3"


def test_the_error_is_a_value_error() -> None:
    assert issubclass(pt.AllocationContractError, ValueError)


# --- 2 候補 -----------------------------------------------------------------------------------


def test_a_valid_candidate_keeps_its_fields() -> None:
    c = candidate()
    assert c.stock_code == "0002" and c.kind is pt.CandidateKind.NEW_BUY
    assert c.raer.state is RaerState.AVAILABLE


@pytest.mark.parametrize("action", sorted(BUY_FAMILY_ACTIONS, key=str))
def test_every_buy_family_action_is_accepted(action: BuyAction) -> None:
    assert candidate(buy_action=action).buy_action is action


@pytest.mark.parametrize("action", [a for a in BuyAction if a not in BUY_FAMILY_ACTIONS])
def test_a_non_buy_action_is_rejected(action: BuyAction) -> None:
    # 適格性を optimizer が覆さない(買い判定の結果だけを受ける)
    with pytest.raises(pt.AllocationContractError):
        candidate(buy_action=action)


_BAD_CANDIDATES: list[tuple[str, dict[str, object]]] = [
    ("empty code", {"stock_code": ""}),
    ("blank code", {"stock_code": "  "}),
    ("code not a str", {"stock_code": 2}),
    ("kind wrong type", {"kind": "NEW_BUY"}),
    ("action wrong type", {"buy_action": "BUY"}),
    ("price zero", {"current_price": D("0")}),
    ("price negative", {"current_price": D("-424242.5")}),
    ("price float", {"current_price": 2340.5}),
    ("price int", {"current_price": 2340}),
    ("price bool", {"current_price": True}),
    ("price NaN", {"current_price": D("NaN")}),
    ("price infinite", {"current_price": D("Infinity")}),
    ("as_of naive", {"price_as_of": dt.datetime(2026, 10, 12, 8, 40)}),
    ("as_of not a datetime", {"price_as_of": "2026-10-12"}),
    ("tier wrong type", {"price_tier": "standard"}),
    ("raer wrong type", {"raer": "x"}),
    ("position negative", {"current_position_value": D("-424242.5")}),
    ("position float", {"current_position_value": 1000.5}),
    ("position NaN", {"current_position_value": D("NaN")}),
    ("sector empty", {"sector": ""}),
    ("sector not a str", {"sector": 5}),
    ("new buy with a position", {"current_position_value": D("1000")}),
]


@pytest.mark.parametrize(("label", "over"), _BAD_CANDIDATES, ids=[b[0] for b in _BAD_CANDIDATES])
def test_a_malformed_candidate_is_rejected(label: str, over: dict[str, object]) -> None:
    with pytest.raises(pt.AllocationContractError):
        candidate(**over)


def test_optional_candidate_fields_may_be_none() -> None:
    c = candidate(price_as_of=None, price_tier=None, sector=None)
    assert c.price_as_of is None and c.price_tier is None and c.sector is None


def test_an_additional_buy_may_carry_a_position() -> None:
    c = candidate(kind=pt.CandidateKind.ADDITIONAL_BUY, current_position_value=D("120000"))
    assert c.current_position_value == D("120000")


def test_an_unavailable_raer_can_be_carried_by_a_candidate() -> None:
    # 除外して理由を残すのは optimizer(PR-C)。型は UNAVAILABLE の RAER を持てる
    assert candidate(raer=raer(RaerState.UNAVAILABLE)).raer.state is RaerState.UNAVAILABLE


# --- 3 制約 -----------------------------------------------------------------------------------


def test_valid_constraints_keep_their_fields() -> None:
    k = constraints()
    assert k.minimum_trading_unit == 100 and k.max_single_stock_ratio == D("0.2")


def test_constraints_may_leave_every_number_undecided() -> None:
    k = pt.AllocationConstraints(
        max_single_stock_amount=None,
        max_single_stock_ratio=None,
        max_sector_ratio=None,
        max_high_risk_ratio=None,
        high_risk_flags=frozenset(),
        minimum_trading_unit=100,
    )
    assert k.max_single_stock_amount is None


_BAD_CONSTRAINTS: list[tuple[str, dict[str, object]]] = [
    ("amount negative", {"max_single_stock_amount": D("-424242.5")}),
    ("amount float", {"max_single_stock_amount": 500000.5}),
    ("amount NaN", {"max_single_stock_amount": D("NaN")}),
    ("single ratio zero", {"max_single_stock_ratio": D("0")}),
    ("single ratio above one", {"max_single_stock_ratio": D("1.0001")}),
    ("single ratio float", {"max_single_stock_ratio": 0.2}),
    ("sector ratio zero", {"max_sector_ratio": D("0")}),
    ("sector ratio above one", {"max_sector_ratio": D("1.5")}),
    ("high risk ratio negative", {"max_high_risk_ratio": D("-0.1")}),
    ("high risk ratio above one", {"max_high_risk_ratio": D("2")}),
    ("flags not a frozenset", {"high_risk_flags": ["cyclical_industry"]}),
    ("flag outside the vocabulary", {"high_risk_flags": frozenset({"unknown_flag"})}),
    ("unit zero", {"minimum_trading_unit": 0}),
    ("unit negative", {"minimum_trading_unit": -100}),
    ("unit bool", {"minimum_trading_unit": True}),
    ("unit float", {"minimum_trading_unit": 100.0}),
]


@pytest.mark.parametrize(("label", "over"), _BAD_CONSTRAINTS, ids=[b[0] for b in _BAD_CONSTRAINTS])
def test_malformed_constraints_are_rejected(label: str, over: dict[str, object]) -> None:
    with pytest.raises(pt.AllocationContractError):
        constraints(**over)


def test_a_ratio_of_exactly_one_is_a_valid_decided_value() -> None:
    k = constraints(
        max_single_stock_ratio=D("1"), max_sector_ratio=D("1"), max_high_risk_ratio=D("1")
    )
    assert k.max_sector_ratio == D("1")


def test_every_risk_flag_of_the_vocabulary_is_accepted() -> None:
    k = constraints(high_risk_flags=frozenset(f.value for f in RiskFlag))
    assert len(k.high_risk_flags) == len(list(RiskFlag))


# --- 4 方針と完全性(USER: None = POLICY_INCOMPLETE → 配分しない)---------------------------------


def test_a_complete_policy_has_nothing_missing() -> None:
    assert complete_policy().missing(constraints()) == ()


def test_a_policy_with_nothing_decided_lists_every_undecided_item_in_a_fixed_order() -> None:
    empty = pt.AllocationPolicy(
        min_raer_to_allocate=None, comparison_key=None, tier_quantity_caps=None
    )
    undecided = pt.AllocationConstraints(
        max_single_stock_amount=None,
        max_single_stock_ratio=None,
        max_sector_ratio=None,
        max_high_risk_ratio=None,
        high_risk_flags=frozenset(),
        minimum_trading_unit=100,
    )
    assert empty.missing(undecided) == (
        "min_raer_to_allocate",
        "comparison_key",
        "max_single_stock_amount",
        "max_single_stock_ratio",
        "max_sector_ratio",
        "max_high_risk_ratio",
        "tier_quantity_caps",
    )


@pytest.mark.parametrize(
    ("policy_over", "constraint_over", "expected"),
    [
        ({"min_raer_to_allocate": None}, {}, "min_raer_to_allocate"),
        ({"comparison_key": None}, {}, "comparison_key"),
        ({"tier_quantity_caps": None}, {}, "tier_quantity_caps"),
        ({}, {"max_single_stock_amount": None}, "max_single_stock_amount"),
        ({}, {"max_single_stock_ratio": None}, "max_single_stock_ratio"),
        ({}, {"max_sector_ratio": None}, "max_sector_ratio"),
        ({}, {"max_high_risk_ratio": None}, "max_high_risk_ratio"),
        # high-risk の上限を決めたのに、何を high-risk と数えるかが空 = 未決定
        ({}, {"high_risk_flags": frozenset()}, "high_risk_flags"),
    ],
)
def test_each_single_undecided_item_alone_makes_the_policy_incomplete(
    policy_over: dict[str, object], constraint_over: dict[str, object], expected: str
) -> None:
    assert complete_policy(**policy_over).missing(constraints(**constraint_over)) == (expected,)


def test_a_high_risk_cap_of_one_needs_no_flags() -> None:
    # 上限が 100%(= 制約なしと決めた)なら、何を high-risk と数えるかは問わない
    k = constraints(max_high_risk_ratio=D("1"), high_risk_flags=frozenset())
    assert complete_policy().missing(k) == ()


def test_is_complete_agrees_with_missing() -> None:
    assert complete_policy().is_complete(constraints()) is True
    assert complete_policy(min_raer_to_allocate=None).is_complete(constraints()) is False


def test_the_tier_caps_must_cover_every_tier_with_a_ratio() -> None:
    for bad in (
        {pt.PriceTier.ENTRY: D("0.3")},  # 段階が足りない
        {t: D("0") for t in pt.PriceTier},  # 0 は上限として無意味
        {t: D("1.5") for t in pt.PriceTier},
        {t: 0.5 for t in pt.PriceTier},  # float
        {"entry": D("0.3"), "standard": D("0.5"), "strong": D("1")},  # キーが列挙でない
    ):
        with pytest.raises(pt.AllocationContractError):
            complete_policy(tier_quantity_caps=bad)


def test_the_tier_caps_are_copied_and_immutable() -> None:
    source = {t: D("0.5") for t in pt.PriceTier}
    policy = complete_policy(tier_quantity_caps=source)
    source[pt.PriceTier.ENTRY] = D("0.9")  # 渡した dict を後から変えても方針は変わらない
    assert policy.tier_quantity_caps is not None
    assert policy.tier_quantity_caps[pt.PriceTier.ENTRY] == D("0.5")
    with pytest.raises(TypeError):
        policy.tier_quantity_caps[pt.PriceTier.ENTRY] = D("0.1")  # type: ignore[index]


def test_a_policy_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        complete_policy().min_raer_to_allocate = D("1")  # type: ignore[misc]


@pytest.mark.parametrize(
    "over",
    [
        {"min_raer_to_allocate": 3},
        {"min_raer_to_allocate": 3.5},
        {"min_raer_to_allocate": True},
        {"min_raer_to_allocate": D("NaN")},
        {"min_raer_to_allocate": D("Infinity")},
        {"comparison_key": ""},
        {"comparison_key": "  "},
        {"comparison_key": 5},
    ],
)
def test_a_malformed_policy_value_is_rejected(over: dict[str, object]) -> None:
    with pytest.raises(pt.AllocationContractError):
        complete_policy(**over)


def test_a_negative_hurdle_is_allowed() -> None:
    # RAER は負になりうる。hurdle は有限であればよい
    assert complete_policy(min_raer_to_allocate=D("-1")).min_raer_to_allocate == D("-1")


# --- 5 候補ごとの配分 --------------------------------------------------------------------------


def test_an_allocated_candidate_is_consistent() -> None:
    a = allocated("0002", 200, 1)
    assert a.shares == 200 and a.decision is pt.CandidateDecision.ALLOCATED
    assert a.reason_code is pt.ReasonCode.ALLOCATED


@pytest.mark.parametrize("reason", [r for r in pt.ReasonCode if r is not pt.ReasonCode.ALLOCATED])
def test_a_not_allocated_candidate_is_accepted_for_every_other_reason(
    reason: pt.ReasonCode,
) -> None:
    extra: dict[str, object] = {}
    if reason is pt.ReasonCode.INSUFFICIENT_CASH_REMAINING:
        extra = {"cap_limited_shares": 300, "cash_shortfall": D("100000")}
    assert not_allocated("0003", reason, **extra).reason_code is reason


_BAD_ALLOCATIONS: list[tuple[str, dict[str, object]]] = [
    ("allocated with zero shares", {"shares": 0, "amount": D("0"), "cap_limited_shares": 0}),
    ("allocated with another reason", {"reason_code": pt.ReasonCode.BELOW_HURDLE}),
    ("allocated with a zero amount", {"amount": D("0")}),
    (
        "allocated with zero shares but an amount",
        {"shares": 0, "amount": D("1"), "cap_limited_shares": 0},
    ),
    ("allocated without a rank", {"rank": None}),
    ("allocated amount negative", {"amount": D("-424242.5")}),
    ("allocated above its cap-limited shares", {"cap_limited_shares": 100}),
    ("shares bool", {"shares": True}),
    ("shares float", {"shares": 200.0, "amount": D("468000")}),
    ("shares negative", {"shares": -200}),
    ("amount float", {"amount": 468000.5}),
    ("rank zero", {"rank": 0}),
    ("rank bool", {"rank": True}),
    ("code empty", {"stock_code": ""}),
    ("tier wrong type", {"price_tier_used": "standard"}),
    ("shortfall on an allocated candidate", {"cash_shortfall": D("1")}),
    ("shortfall negative", {"cash_shortfall": D("-1")}),
    ("cap-limited shares negative", {"cap_limited_shares": -1}),
]


@pytest.mark.parametrize(("label", "over"), _BAD_ALLOCATIONS, ids=[b[0] for b in _BAD_ALLOCATIONS])
def test_an_inconsistent_allocated_candidate_is_rejected(
    label: str, over: dict[str, object]
) -> None:
    with pytest.raises(pt.AllocationContractError):
        allocated(**over)


_BAD_NOT_ALLOCATED: list[tuple[str, pt.ReasonCode, dict[str, object]]] = [
    ("shares on a not-allocated candidate", pt.ReasonCode.BELOW_HURDLE, {"shares": 100}),
    ("amount on a not-allocated candidate", pt.ReasonCode.BELOW_HURDLE, {"amount": D("1")}),
    (
        "tier used on a not-allocated candidate",
        pt.ReasonCode.BELOW_HURDLE,
        {"price_tier_used": pt.PriceTier.ENTRY},
    ),
    ("reason ALLOCATED on a not-allocated candidate", pt.ReasonCode.ALLOCATED, {}),
    ("rank on an unavailable raer", pt.ReasonCode.RAER_UNAVAILABLE, {"rank": 1}),
    ("rank on a below-hurdle candidate", pt.ReasonCode.BELOW_HURDLE, {"rank": 1}),
    (
        "no rank on a ranked candidate",
        pt.ReasonCode.INSUFFICIENT_CASH_REMAINING,
        {"rank": None, "cap_limited_shares": 300, "cash_shortfall": D("1")},
    ),
    (
        "shortfall without the cash reason",
        pt.ReasonCode.BELOW_MINIMUM_TRADING_UNIT,
        {"cash_shortfall": D("1")},
    ),
    (
        "cash reason without a shortfall",
        pt.ReasonCode.INSUFFICIENT_CASH_REMAINING,
        {"cap_limited_shares": 300},
    ),
]


@pytest.mark.parametrize(
    ("label", "reason", "over"), _BAD_NOT_ALLOCATED, ids=[b[0] for b in _BAD_NOT_ALLOCATED]
)
def test_an_inconsistent_not_allocated_candidate_is_rejected(
    label: str, reason: pt.ReasonCode, over: dict[str, object]
) -> None:
    with pytest.raises(pt.AllocationContractError):
        if reason is pt.ReasonCode.ALLOCATED:
            pt.CandidateAllocation(
                stock_code="0002",
                decision=pt.CandidateDecision.NOT_ALLOCATED,
                shares=0,
                amount=D("0"),
                price_tier_used=None,
                reason_code=reason,
                rank=1,
                cap_limited_shares=0,
                cash_shortfall=D("0"),
            )
        else:
            not_allocated("0002", reason, **over)


def test_the_cash_shortfall_pairs_with_the_cash_reason_and_the_unmet_demand() -> None:
    a = not_allocated(
        "0003",
        pt.ReasonCode.INSUFFICIENT_CASH_REMAINING,
        cap_limited_shares=300,
        cash_shortfall=D("702000"),
    )
    assert a.cap_limited_shares == 300 and a.cash_shortfall == D("702000")


# --- 6 結果 -----------------------------------------------------------------------------------


def test_an_allocated_some_result_is_consistent() -> None:
    r = result()
    assert r.status is pt.AllocationStatus.ALLOCATED_SOME
    assert r.total_allocated == D("468000") and r.remaining_cash == D("300000")


def test_a_no_action_result_has_no_allocation_a_known_remainder_and_a_complete_policy() -> None:
    r = result(
        status=pt.AllocationStatus.NO_ACTION,
        allocations=(not_allocated("0003", pt.ReasonCode.BELOW_HURDLE),),
        total_allocated=D("0"),
        remaining_cash=D("768000"),
    )
    assert r.status is pt.AllocationStatus.NO_ACTION


def test_an_empty_candidate_list_is_a_valid_no_action() -> None:
    r = result(
        status=pt.AllocationStatus.NO_ACTION,
        allocations=(),
        total_allocated=D("0"),
        remaining_cash=D("768000"),
    )
    assert r.allocations == ()


@pytest.mark.parametrize(
    "reason",
    [
        pt.UnavailableReason.CASH_NOT_REGISTERED,
        pt.UnavailableReason.PORTFOLIO_VALUE_UNAVAILABLE,
        pt.UnavailableReason.RAER_NOT_COMPARABLE,
    ],
)
def test_an_unavailable_result_holds_no_allocation_and_no_remainder(
    reason: pt.UnavailableReason,
) -> None:
    r = result(
        status=pt.AllocationStatus.UNAVAILABLE,
        unavailable_reason=reason,
        allocations=(),
        total_allocated=D("0"),
        remaining_cash=None,
    )
    assert r.allocations == () and r.remaining_cash is None


def test_a_policy_incomplete_result_is_unavailable_and_keeps_a_ranking_only_shadow() -> None:
    shadow = (
        not_allocated("0002", pt.ReasonCode.POLICY_INCOMPLETE, rank=1, cap_limited_shares=400),
        not_allocated("0003", pt.ReasonCode.POLICY_INCOMPLETE, rank=2, cap_limited_shares=100),
        not_allocated("0004", pt.ReasonCode.RAER_UNAVAILABLE),
    )
    r = result(
        status=pt.AllocationStatus.UNAVAILABLE,
        unavailable_reason=pt.UnavailableReason.POLICY_INCOMPLETE,
        allocations=shadow,
        total_allocated=D("0"),
        remaining_cash=None,
        policy_missing=("min_raer_to_allocate", "comparison_key"),
    )
    assert [a.stock_code for a in r.allocations] == ["0002", "0003", "0004"]
    assert all(a.shares == 0 for a in r.allocations)


_BAD_RESULTS: list[tuple[str, dict[str, object]]] = [
    ("owner empty", {"owner": ""}),
    ("status wrong type", {"status": "ALLOCATED_SOME"}),
    (
        "allocated some without an allocation",
        {
            "allocations": (not_allocated("0003"),),
            "total_allocated": D("0"),
            "remaining_cash": D("768000"),
        },
    ),
    (
        "allocated some with a reason",
        {"unavailable_reason": pt.UnavailableReason.CASH_NOT_REGISTERED},
    ),
    ("allocated some with a missing policy", {"policy_missing": ("min_raer_to_allocate",)}),
    ("allocated some without a remainder", {"remaining_cash": None}),
    ("negative remainder", {"remaining_cash": D("-1")}),
    ("total differs from the allocations", {"total_allocated": D("1")}),
    (
        "duplicate stock code",
        {"allocations": (allocated("0002", 200, 1), allocated("0002", 100, 2))},
    ),
    (
        "allocations out of rank order",
        {"allocations": (allocated("0003", 100, 2), allocated("0002", 200, 1))},
    ),
    (
        "duplicate ranks",
        {
            "allocations": (allocated("0002", 200, 1), allocated("0009", 100, 1)),
            "total_allocated": D("702000"),
        },
    ),
    (
        "rank gap",
        {
            "allocations": (allocated("0002", 200, 1), allocated("0009", 100, 3)),
            "total_allocated": D("702000"),
        },
    ),
    (
        "ranks not starting at one",
        {"allocations": (allocated("0002", 200, 2),), "total_allocated": D("468000")},
    ),
    (
        "unranked before ranked",
        {
            "allocations": (
                not_allocated("0001", pt.ReasonCode.RAER_UNAVAILABLE),
                allocated("0002", 200, 1),
            )
        },
    ),
    ("contract version changed", {"contract_version": "C603-v9"}),
    ("allocations not a tuple", {"allocations": [allocated("0002", 200, 1)]}),
    ("total float", {"total_allocated": 468000.5}),
    ("policy_missing not a tuple", {"policy_missing": ["comparison_key"]}),
]


@pytest.mark.parametrize(("label", "over"), _BAD_RESULTS, ids=[b[0] for b in _BAD_RESULTS])
def test_an_inconsistent_result_is_rejected(label: str, over: dict[str, object]) -> None:
    with pytest.raises(pt.AllocationContractError):
        result(**over)


_BAD_UNAVAILABLE: list[tuple[str, dict[str, object]]] = [
    ("no reason", {"unavailable_reason": None}),
    ("a remainder", {"remaining_cash": D("1")}),
    ("a total", {"total_allocated": D("1")}),
    (
        "an allocation for a non-policy reason",
        {"allocations": (not_allocated("0002", pt.ReasonCode.BELOW_HURDLE),)},
    ),
]


@pytest.mark.parametrize(("label", "over"), _BAD_UNAVAILABLE, ids=[b[0] for b in _BAD_UNAVAILABLE])
def test_an_inconsistent_unavailable_result_is_rejected(
    label: str, over: dict[str, object]
) -> None:
    base: dict[str, object] = {
        "status": pt.AllocationStatus.UNAVAILABLE,
        "unavailable_reason": pt.UnavailableReason.CASH_NOT_REGISTERED,
        "allocations": (),
        "total_allocated": D("0"),
        "remaining_cash": None,
    }
    base.update(over)
    with pytest.raises(pt.AllocationContractError):
        result(**base)


def test_a_policy_incomplete_result_must_name_what_is_missing_and_may_not_allocate() -> None:
    base: dict[str, object] = {
        "status": pt.AllocationStatus.UNAVAILABLE,
        "unavailable_reason": pt.UnavailableReason.POLICY_INCOMPLETE,
        "allocations": (not_allocated("0002", pt.ReasonCode.POLICY_INCOMPLETE, rank=1),),
        "total_allocated": D("0"),
        "remaining_cash": None,
        "policy_missing": ("comparison_key",),
    }
    assert result(**base).policy_missing == ("comparison_key",)
    for bad in (
        {"policy_missing": ()},  # 何が未決定かを残さない
        {
            "allocations": (allocated("0002", 200, 1),),
            "total_allocated": D("468000"),
        },  # 方針不完全で配分
        {
            "allocations": (not_allocated("0002", pt.ReasonCode.BELOW_HURDLE),)
        },  # 方針不完全の shadow は別の理由を持てない
        {"policy_missing": ("not_an_item",)},  # 語彙外の項目名
    ):
        with pytest.raises(pt.AllocationContractError):
            result(**{**base, **bad})


def test_a_no_action_result_cannot_carry_an_allocation() -> None:
    with pytest.raises(pt.AllocationContractError):
        result(
            status=pt.AllocationStatus.NO_ACTION,
            allocations=(allocated("0002", 200, 1),),
            total_allocated=D("468000"),
            remaining_cash=D("300000"),
        )


def test_the_policy_item_vocabulary_is_exactly_what_missing_can_return() -> None:
    assert set(pt.POLICY_ITEMS) == {
        "min_raer_to_allocate",
        "comparison_key",
        "max_single_stock_amount",
        "max_single_stock_ratio",
        "max_sector_ratio",
        "max_high_risk_ratio",
        "high_risk_flags",
        "tier_quantity_caps",
    }


# --- 7 例外のメッセージに値を含めない(M2 の SHOULD-1 / #909 の教訓)------------------------------


def _bad_constructions() -> list[object]:
    return [
        lambda: candidate(current_price=D("-" + _DISTINCT)),
        lambda: candidate(current_price=424242.5),
        lambda: candidate(current_position_value=D("-" + _DISTINCT)),
        lambda: candidate(stock_code=424242),
        lambda: candidate(sector=424242),
        lambda: constraints(max_single_stock_amount=D("-" + _DISTINCT)),
        lambda: constraints(max_single_stock_ratio=D("1.5551")),
        lambda: constraints(max_sector_ratio=D("-0.7771")),
        lambda: constraints(minimum_trading_unit=-4242),
        lambda: constraints(high_risk_flags=frozenset({"424242"})),
        lambda: complete_policy(min_raer_to_allocate=424242),
        lambda: complete_policy(comparison_key=424242),
        lambda: complete_policy(tier_quantity_caps={t: D("1.5551") for t in pt.PriceTier}),
        lambda: allocated(shares=424242.5, amount=D("1")),
        lambda: allocated(amount=D("-" + _DISTINCT)),
        lambda: result(owner=424242),
        lambda: result(total_allocated=D("424242")),
        lambda: result(remaining_cash=D("-" + _DISTINCT)),
    ]


@pytest.mark.parametrize("index", range(len(_bad_constructions())))
def test_no_rejection_message_contains_an_input_value(index: int) -> None:
    make = _bad_constructions()[index]
    with pytest.raises(pt.AllocationContractError) as info:
        make()  # type: ignore[operator]
    text = str(info.value)
    assert text
    for needle in ("424242", "1.5551", "0.7771", "4242"):
        assert needle not in text


def _tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def test_the_error_is_built_in_exactly_one_place_from_names_only() -> None:
    # 例外の構築は _fail の 1 箇所だけで、メッセージは項目名と問題名(固定の語)だけから作る
    calls = [
        n
        for n in ast.walk(_tree())
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", "")) == "AllocationContractError"
    ]
    assert len(calls) == 1
    (call,) = calls
    assert isinstance(call.args[0], ast.JoinedStr)
    interpolated = [v.value for v in call.args[0].values if isinstance(v, ast.FormattedValue)]
    assert all(isinstance(v, ast.Name) and v.id in {"name", "problem"} for v in interpolated)


# --- 8 純粋性・休眠(AST)---------------------------------------------------------------------

_ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
    "types",
    "typing",
    "jstock_advisor.domain.entities.enums",
    "jstock_advisor.domain.valuation.raer",
}


def test_the_module_imports_only_the_standard_library_and_domain_types() -> None:
    found: set[str] = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    assert found <= _ALLOWED_IMPORTS, found - _ALLOWED_IMPORTS


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


def test_the_module_does_not_import_the_optimizer_or_anything_that_computes() -> None:
    # PR-A は型だけ。M2 の計算関数も import しない(PR-C が使う)
    source = _MODULE_PATH.read_text(encoding="utf-8")
    assert "buy_affordability" not in source
    assert "compute_buy_affordability" not in source


# この module を import してよい src の module の明示リスト(いずれも未配線の dormant)
_ALLOWED_IMPORTERS = {
    "domain/signals/portfolio_allocation.py",  # #603 PR-C(optimize_allocation)
    "domain/signals/capital_rotation.py",  # #603 PR-D(AllocationResult を入力に持つ)
    "domain/signals/allocation_policy_config.py",  # #603 PR-E(方針値を型へ入れて渡す loader)
    "domain/signals/allocation_inputs.py",  # #603 PR-F1(入力の組み立て)
    "domain/signals/allocation_shadow_record.py",  # #603 PR-F2-1(記録の組み立て・replay)
}


def test_the_module_is_imported_from_nowhere_else_in_src() -> None:
    src = _REPO / "src" / "jstock_advisor"
    for path in src.rglob("*.py"):
        if path == _MODULE_PATH:
            continue
        if "portfolio_allocation_types" in path.read_text(encoding="utf-8"):
            assert path.relative_to(src).as_posix() in _ALLOWED_IMPORTERS, str(path)


def test_every_combination_of_decision_and_reason_is_either_consistent_or_rejected() -> None:
    # 全探索: decision × reason × shares で、受け入れた組合せは I-3 を満たす
    accepted = 0
    for decision, reason, shares in itertools.product(
        pt.CandidateDecision, pt.ReasonCode, (0, 100)
    ):
        try:
            item = pt.CandidateAllocation(
                stock_code="0002",
                decision=decision,
                shares=shares,
                amount=D("2340") * shares,
                price_tier_used=None,
                reason_code=reason,
                rank=1,
                cap_limited_shares=shares,
                cash_shortfall=D("0"),
            )
        except pt.AllocationContractError:
            continue
        accepted += 1
        allocated_flag = item.decision is pt.CandidateDecision.ALLOCATED
        assert allocated_flag == (item.shares > 0) == (item.reason_code is pt.ReasonCode.ALLOCATED)
    assert accepted > 0


def test_a_negative_position_is_rejected_for_an_additional_buy_too() -> None:
    # 新規買いは『保有 0』の規則でも弾かれるため、負の評価額そのものの拒否は追加買いで固定する
    with pytest.raises(pt.AllocationContractError):
        candidate(kind=pt.CandidateKind.ADDITIONAL_BUY, current_position_value=D("-1"))


def test_a_repeated_stock_is_rejected_even_when_the_total_is_consistent() -> None:
    twice = (allocated("0002", 200, 1), allocated("0002", 100, 2))
    with pytest.raises(pt.AllocationContractError):
        result(allocations=twice, total_allocated=D("702000"))


@pytest.mark.parametrize(
    "status", [pt.AllocationStatus.ALLOCATED_SOME, pt.AllocationStatus.NO_ACTION]
)
def test_an_available_result_cannot_hold_an_incomplete_policy_reason(
    status: pt.AllocationStatus,
) -> None:
    # 方針が不完全な日は UNAVAILABLE(順位だけの shadow)。完全な日(ALLOCATED_SOME / NO_ACTION)の
    # 候補に POLICY_INCOMPLETE の理由は残らない
    shadow = not_allocated("0003", pt.ReasonCode.POLICY_INCOMPLETE, rank=2)
    if status is pt.AllocationStatus.ALLOCATED_SOME:
        items = (allocated("0002", 200, 1), shadow)
        total, remaining = D("468000"), D("300000")
    else:
        items = (not_allocated("0002", pt.ReasonCode.POLICY_INCOMPLETE, rank=1), shadow)
        total, remaining = D("0"), D("768000")
    with pytest.raises(pt.AllocationContractError):
        result(status=status, allocations=items, total_allocated=total, remaining_cash=remaining)


def test_an_available_result_may_still_exclude_an_unavailable_raer() -> None:
    # RAER_UNAVAILABLE は方針の完全性と無関係に残る(除外して理由を残す)
    r = result(
        allocations=(
            allocated("0002", 200, 1),
            not_allocated("0003", pt.ReasonCode.RAER_UNAVAILABLE),
        )
    )
    assert r.allocations[1].reason_code is pt.ReasonCode.RAER_UNAVAILABLE


def test_an_allocated_candidate_cannot_have_a_zero_amount_in_a_whole_result() -> None:
    # サブちゃんの PR-A SHOULD-1: 株数 > 0 の配分で購入総額 0 円の結果が型を通っていた
    with pytest.raises(pt.AllocationContractError):
        result(
            allocations=(allocated("0002", 100, 1, amount=D("0")),),
            total_allocated=D("0"),
            remaining_cash=D("768000"),
        )


# --- #603 段 4: 比較できない日の診断行(RAER_NOT_COMPARABLE)--------------------------------


def _not_comparable_day(
    *rows: pt.CandidateAllocation, policy_missing: tuple[str, ...] = ()
) -> pt.AllocationResult:
    return pt.AllocationResult(
        owner="owner-a",
        status=pt.AllocationStatus.UNAVAILABLE,
        unavailable_reason=pt.UnavailableReason.RAER_NOT_COMPARABLE,
        allocations=tuple(rows),
        total_allocated=D("0"),
        remaining_cash=None,
        policy_missing=policy_missing,
    )


def test_the_not_comparable_reason_is_unranked_by_both_conditions() -> None:
    """★ 診断行は rank を持てない(rank つきは拒否)。rank なしは UNRANKED の理由だけ(双条件)★"""
    assert pt.ReasonCode.RAER_NOT_COMPARABLE in pt._UNRANKED_REASONS
    row = not_allocated("0002", pt.ReasonCode.RAER_NOT_COMPARABLE)
    assert row.rank is None and row.shares == 0 and row.amount == D("0")
    with pytest.raises(pt.AllocationContractError):
        not_allocated("0002", pt.ReasonCode.RAER_NOT_COMPARABLE, rank=1)


def test_a_not_comparable_day_may_keep_diagnostic_rows_even_when_the_policy_is_complete() -> None:
    """SHOULD-1: 比較できない日は方針が完全でも起こる。policy_missing を要求しない。"""
    result = _not_comparable_day(
        not_allocated("0002", pt.ReasonCode.RAER_NOT_COMPARABLE),
        not_allocated("0003", pt.ReasonCode.RAER_UNAVAILABLE),
    )
    assert result.policy_missing == () and len(result.allocations) == 2
    assert _not_comparable_day().allocations == ()  # 行が 0 件の日(従来の形)も構築できる


def test_a_not_comparable_day_may_also_name_the_missing_policy_items() -> None:
    result = _not_comparable_day(
        not_allocated("0002", pt.ReasonCode.RAER_NOT_COMPARABLE),
        policy_missing=("comparison_key",),
    )
    assert result.policy_missing == ("comparison_key",)


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(lambda: allocated("0002"), id="allocated"),
        pytest.param(
            lambda: not_allocated("0002", pt.ReasonCode.POLICY_INCOMPLETE), id="shadow-reason"
        ),
        pytest.param(lambda: not_allocated("0002", pt.ReasonCode.BELOW_HURDLE), id="below-hurdle"),
        pytest.param(
            lambda: not_allocated("0002", pt.ReasonCode.SECTOR_UNKNOWN), id="other-reason"
        ),
    ],
)
def test_a_not_comparable_day_holds_only_its_own_reasons(row: Any) -> None:
    with pytest.raises(pt.AllocationContractError):
        _not_comparable_day(row())


def test_the_other_unavailable_days_still_keep_no_rows() -> None:
    reasons = (
        pt.UnavailableReason.CASH_NOT_REGISTERED,
        pt.UnavailableReason.PORTFOLIO_VALUE_UNAVAILABLE,
    )
    for reason in reasons:
        with pytest.raises(pt.AllocationContractError):
            pt.AllocationResult(
                owner="owner-a",
                status=pt.AllocationStatus.UNAVAILABLE,
                unavailable_reason=reason,
                allocations=(not_allocated("0002", pt.ReasonCode.RAER_NOT_COMPARABLE),),
                total_allocated=D("0"),
                remaining_cash=None,
                policy_missing=(),
            )
