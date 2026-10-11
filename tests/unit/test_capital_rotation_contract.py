"""Issue #603 / PR-D: Capital Rotation の純粋関数 `evaluate_capital_rotation` の契約テスト。

純粋関数だけで、時計・I/O・永続化・配線に触れない。どこからも import されない(dormant)。
最低改善幅・算入割合・信頼度の下限は決めない(None = 未決定 = SHADOW_ONLY で、提案を出さない)。

## 何を固定するのか

```
・提案は SELL と BUY の対だけ(SELL なしの BUY は出ない)。入替元は『企業に重大な問題がない』保有
  (eligibility = ELIGIBLE)だけで、UNKNOWN / BLOCKED_BY_EXIT は入替元にしない(fail-closed)
・買付余力の不足だけでは SELL を強めない: 入替元の選定は RAER の差と適格性が先で、
  資金は『その組で購入が 1 単元以上成立するか』にだけ使う。改善幅が足りない組は、資金がどれだけ
  あっても(0 円でも)提案しない
・購入資金へ算入するのは売却見込額の算入割合(0 < r < 1)の分だけ。購入総額 <= 残資金 + 算入額。
  税・手数料は推定しない(cost_model = NONE)。未約定の売却代金を 100% 確定資金として扱わない
・方針が 1 つでも None なら SHADOW_ONLY(評価の統計だけを残し、提案しない)
・購入株数は PR-C の optimize_allocation を『売却後の状態・購入候補 1 件』で呼んで決める(再判定)。
  売却で総額が減ると比率上限は厳しくなる(売却前の cap_limited_shares をそのまま使わない)
```

## 独立な参照実装

期待値は実装の式から導かない。参照実装は『売却株数 k を 1 単元ずつ、購入株数 n を 1 単元ずつ試し、
すべての不等式(資金・金額・比率・業種・high-risk・段階)を直接評価する』という定義そのものである。
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import BuyAction, ConfidenceLevel
from jstock_advisor.domain.exit_architecture.vocabulary import ExitClass
from jstock_advisor.domain.signals import capital_rotation as cr
from jstock_advisor.domain.signals import portfolio_allocation as pa
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
_MODULE_PATH = _REPO / "src" / "jstock_advisor" / "domain" / "signals" / "capital_rotation.py"
_ALL_AXES = frozenset(a.value for a in AxisCode)
_JST = dt.timezone(dt.timedelta(hours=9))
_AS_OF = dt.datetime(2026, 10, 12, 8, 40, tzinfo=_JST)
R = pt.ReasonCode
T = cr.TargetReason


# --- 部品(合成データ)-----------------------------------------------------------------------


def raer(
    state: RaerState = RaerState.AVAILABLE,
    value: str = "5",
    confidence: ConfidenceLevel | None = ConfidenceLevel.HIGH,
    risk_flags: frozenset[str] = frozenset(),
) -> RaerResult:
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
    if state is RaerState.PROVISIONAL:
        not_applied = frozenset({AxisCode.EARNINGS_TREND.value})
        applied = _ALL_AXES - not_applied
        return RaerResult(
            state=state,
            value=D(value),
            confidence=confidence,
            risk_flags=risk_flags,
            risk_flags_available=True,
            model_version=MODEL_VERSION,
            comparison_key=make_comparison_key(MODEL_VERSION, applied),
            axes_applied=applied,
            axes_not_applied=not_applied,
            reason_codes=("AXIS_NOT_IMPLEMENTED",),
        )
    return RaerResult(
        state=RaerState.AVAILABLE,
        value=D(value),
        confidence=confidence,
        risk_flags=risk_flags,
        risk_flags_available=True,
        model_version=MODEL_VERSION,
        comparison_key=make_comparison_key(MODEL_VERSION, _ALL_AXES),
        axes_applied=_ALL_AXES,
        axes_not_applied=frozenset(),
        reason_codes=(),
    )


KEY = raer().comparison_key
PRICE = D("1000")
UNIT = 100
HIGH_RISK = frozenset({RiskFlag.CYCLICAL_INDUSTRY.value})


def cand(code: str = "0002", **over: object) -> pt.AllocationCandidate:
    base: dict[str, object] = {
        "stock_code": code,
        "kind": pt.CandidateKind.NEW_BUY,
        "buy_action": BuyAction.BUY,
        "current_price": PRICE,
        "price_as_of": _AS_OF,
        "price_tier": None,
        "raer": raer(value="9"),
        "current_position_value": D("0"),
        "sector": "電気機器",
    }
    base.update(over)
    return pt.AllocationCandidate(**base)  # type: ignore[arg-type]


def cons(**over: object) -> pt.AllocationConstraints:
    base: dict[str, object] = {
        "max_single_stock_amount": D("600000"),
        "max_single_stock_ratio": D("1"),
        "max_sector_ratio": D("1"),
        "max_high_risk_ratio": D("1"),
        "high_risk_flags": HIGH_RISK,
        "minimum_trading_unit": UNIT,
    }
    base.update(over)
    return pt.AllocationConstraints(**base)  # type: ignore[arg-type]


def apol(**over: object) -> pt.AllocationPolicy:
    base: dict[str, object] = {
        "min_raer_to_allocate": D("3"),
        "comparison_key": KEY,
        "tier_quantity_caps": {t: D("1") for t in pt.PriceTier},
    }
    base.update(over)
    return pt.AllocationPolicy(**base)  # type: ignore[arg-type]


def src(code: str = "5001", **over: object) -> cr.RotationSource:
    base: dict[str, object] = {
        "stock_code": code,
        "shares_held": 1000,
        "current_price": D("500"),
        "minimum_trading_unit": 100,
        "raer": raer(value="3.5"),
        "eligibility": cr.SourceEligibility.ELIGIBLE,
        "sector": "食料品",
    }
    base.update(over)
    return cr.RotationSource(**base)  # type: ignore[arg-type]


def rpol(**over: object) -> cr.RotationPolicy:
    base: dict[str, object] = {
        "min_raer_improvement": D("2"),
        "proceeds_inclusion_ratio": D("0.8"),
        "min_confidence": ConfidenceLevel.LOW,
    }
    base.update(over)
    return cr.RotationPolicy(**base)  # type: ignore[arg-type]


def empty_rpol() -> cr.RotationPolicy:
    return cr.RotationPolicy(
        min_raer_improvement=None, proceeds_inclusion_ratio=None, min_confidence=None
    )


def make_allocation(**over: object) -> pt.AllocationResult:
    base: dict[str, object] = {
        "owner": "owner-a",
        "available_cash": D("50000"),
        "portfolio_value": D("10000000"),
        "candidates": [cand()],
        "constraints": cons(),
        "policy": apol(),
    }
    base.update(over)
    return pa.optimize_allocation(**base)  # type: ignore[arg-type]


def run(**over: object) -> cr.RotationResult:
    """既定: 買付余力 50,000 円で 100,000 円の 1 単元も買えない候補 0002 と、入替元 5001。"""
    alloc_args: dict[str, object] = {}
    for key in (
        "available_cash",
        "portfolio_value",
        "candidates",
        "constraints",
        "policy",
        "sector_exposure",
        "high_risk_exposure",
    ):
        alias = {"policy": "allocation_policy"}.get(key, key)
        if alias in over:
            alloc_args[key] = over.pop(alias)
    candidates = alloc_args.get("candidates", [cand()])
    constraints = alloc_args.get("constraints", cons())
    allocation = over.pop("allocation", None) or make_allocation(**alloc_args)
    base: dict[str, object] = {
        "owner": "owner-a",
        "allocation": allocation,
        "candidates": candidates,
        "sources": [src()],
        "constraints": constraints,
        "allocation_policy": alloc_args.get("policy", apol()),
        "policy": rpol(),
        "portfolio_value": alloc_args.get("portfolio_value", D("10000000")),
        "sector_exposure": alloc_args.get("sector_exposure"),
        "high_risk_exposure": alloc_args.get("high_risk_exposure"),
    }
    base.update(over)
    return cr.evaluate_capital_rotation(**base)  # type: ignore[arg-type]


def target_reasons(result: cr.RotationResult) -> dict[str, cr.TargetReason]:
    return {t.stock_code: t.reason_code for t in result.targets}


# --- 1 基本の提案(手計算)-------------------------------------------------------------------


def test_the_basic_rotation_sells_the_least_that_funds_the_largest_purchase() -> None:
    # 目標 0002(価格 1,000・100,000 円 / 単元)。買付余力 50,000。入替元 5001(500 円 x 1,000 株)。
    # k 単元(100 株 = 50,000 円)の売却で算入額 = 40,000 x k。k = 9 で 410,000 -> 4 単元
    r = run()
    assert r.status is cr.RotationStatus.ROTATION_PROPOSED
    (p,) = r.proposals
    assert p.proposal_id == "rotation:owner-a:5001:0002"
    assert (p.sell_leg.stock_code, p.sell_leg.sell_shares) == ("5001", 900)
    assert p.sell_leg.estimated_proceeds == D("450000")
    assert p.sell_leg.counted_proceeds == D("360000")
    assert p.sell_leg.exit_class is ExitClass.CAPITAL_ROTATION
    assert p.sell_leg.quantity_kind is cr.SellQuantityKind.PARTIAL
    assert (p.buy_leg.stock_code, p.buy_leg.shares, p.buy_leg.amount) == ("0002", 400, D("400000"))
    assert p.buy_leg.price_tier_used is None
    assert p.improvement == D("5.5")
    assert p.projected_remaining_cash == D("10000")
    assert (r.starting_cash, r.ending_cash) == (D("50000"), D("10000"))
    assert target_reasons(r) == {"0002": T.PROPOSED}


def test_a_sale_of_the_whole_position_is_marked_full() -> None:
    r = run(sources=[src(shares_held=900)])
    (p,) = r.proposals
    assert p.sell_leg.sell_shares == 900 and p.sell_leg.quantity_kind is cr.SellQuantityKind.FULL


def test_the_purchase_never_exceeds_the_unmet_demand_of_the_allocation() -> None:
    # 需要 = 資金を無視した上限(金額の上限 300,000 -> 300 株)。売却額が十分に大きくても 300 株まで
    r = run(constraints=cons(max_single_stock_amount=D("300000")), sources=[src(shares_held=5000)])
    (p,) = r.proposals
    assert p.buy_leg.shares == 300
    # 300,000 を買う: 50,000 + 40,000 x k >= 300,000 -> k = 7 -> 700 株
    assert p.sell_leg.sell_shares == 700


def test_cost_and_contract_version_are_fixed() -> None:
    r = run()
    assert r.cost_model == "NONE" and r.contract_version == "C604-v1.1"
    assert cr.COST_MODEL == "NONE" and cr.CONTRACT_VERSION == "C604-v1.1"


def test_the_estimated_proceeds_use_the_current_price_without_any_cost() -> None:
    r = run(sources=[src(current_price=D("512.5"))])
    (p,) = r.proposals
    assert p.sell_leg.estimated_proceeds == D("512.5") * p.sell_leg.sell_shares
    assert p.sell_leg.counted_proceeds == p.sell_leg.estimated_proceeds * D("0.8")


def test_the_purchase_amount_is_funded_by_cash_plus_the_counted_proceeds_only() -> None:
    r = run()
    (p,) = r.proposals
    assert p.buy_leg.amount <= D("50000") + p.sell_leg.counted_proceeds
    assert p.buy_leg.amount > D("50000") + p.sell_leg.estimated_proceeds * D("0.8") - PRICE * UNIT


# --- 2 同じ回の複数の提案: 資金の持ち越し・1 回だけ使う ------------------------------------------


def _two_targets(**over: object) -> cr.RotationResult:
    base: dict[str, object] = {
        "candidates": [cand("0002", raer=raer(value="9")), cand("0003", raer=raer(value="8"))],
        "constraints": cons(max_single_stock_amount=D("300000")),
        "sources": [
            src("5001", raer=raer(value="3")),
            src("5002", raer=raer(value="4")),
        ],
    }
    base.update(over)
    return run(**base)


def test_two_targets_each_take_their_own_source_and_the_cash_carries_over() -> None:
    r = _two_targets()
    first, second = r.proposals
    # 0002(RAER 高)が先: 改善幅の大きい 5001(9 - 3 = 6)。k = 7 で 330,000 -> 300,000 を買う
    assert (first.sell_leg.stock_code, first.sell_leg.sell_shares) == ("5001", 700)
    assert first.buy_leg.shares == 300 and first.projected_remaining_cash == D("30000")
    # 0003: 5001 は使用済みで、5002(8 - 4 = 4)。残資金 30,000 + 40,000 x 7 = 310,000 -> 300,000
    assert (second.sell_leg.stock_code, second.sell_leg.sell_shares) == ("5002", 700)
    assert second.buy_leg.shares == 300 and second.projected_remaining_cash == D("10000")
    assert r.ending_cash == D("10000")
    assert [p.buy_leg.stock_code for p in r.proposals] == ["0002", "0003"]


def test_a_source_is_used_by_one_proposal_only() -> None:
    r = _two_targets(sources=[src("5001", raer=raer(value="3"))])
    assert [p.buy_leg.stock_code for p in r.proposals] == ["0002"]
    assert target_reasons(r) == {"0002": T.PROPOSED, "0003": T.SOURCE_ALREADY_USED}


def test_the_total_purchase_stays_within_the_cash_plus_all_counted_proceeds() -> None:
    r = _two_targets()
    assert r.starting_cash is not None
    bought = sum((p.buy_leg.amount for p in r.proposals), D(0))
    counted = sum((p.sell_leg.counted_proceeds for p in r.proposals), D(0))
    assert bought <= r.starting_cash + counted
    assert r.ending_cash == r.starting_cash + counted - bought


# --- 3 入替元の選定: RAER の差と適格性が先 -------------------------------------------


def test_the_source_with_the_largest_improvement_is_tried_first() -> None:
    r = run(sources=[src("5001", raer=raer(value="4")), src("5002", raer=raer(value="3.2"))])
    (p,) = r.proposals
    assert p.sell_leg.stock_code == "5002"


def test_equal_improvements_are_broken_by_the_stock_code() -> None:
    r = run(sources=[src("5009", raer=raer(value="4")), src("5002", raer=raer(value="4"))])
    assert r.proposals[0].sell_leg.stock_code == "5002"


def test_a_source_that_cannot_fund_a_purchase_is_skipped_for_the_next_one() -> None:
    # 5001(改善幅が最大)は 100 株しかなく、全部売っても 50,000 + 40,000 = 90,000 < 100,000
    small = src("5001", shares_held=100, raer=raer(value="2"))
    ok = src("5002", raer=raer(value="4"))
    r = run(sources=[small, ok])
    (p,) = r.proposals
    assert p.sell_leg.stock_code == "5002"


def test_when_no_source_can_fund_a_unit_the_target_is_reported_so() -> None:
    r = run(sources=[src(shares_held=100)])
    assert r.status is cr.RotationStatus.NO_ROTATION and r.proposals == ()
    assert target_reasons(r) == {"0002": T.CANNOT_FUND_PURCHASE}


@pytest.mark.parametrize(
    "eligibility", [cr.SourceEligibility.UNKNOWN, cr.SourceEligibility.BLOCKED_BY_EXIT]
)
def test_only_an_eligible_holding_can_be_a_source(eligibility: cr.SourceEligibility) -> None:
    r = run(sources=[src(eligibility=eligibility)])
    assert r.proposals == () and target_reasons(r) == {"0002": T.NO_ELIGIBLE_SOURCE}
    assert r.evaluated_pairs == 0 and r.best_improvement is None


def test_there_is_no_source_at_all() -> None:
    r = run(sources=[])
    assert r.status is cr.RotationStatus.NO_ROTATION
    assert target_reasons(r) == {"0002": T.NO_ELIGIBLE_SOURCE}


def test_a_holding_that_is_also_a_candidate_today_is_not_a_source() -> None:
    both = cand("0003", raer=raer(value="8"))
    r = run(candidates=[cand("0002"), both], sources=[src("0003", raer=raer(value="2"))])
    assert r.proposals == () and set(target_reasons(r).values()) == {T.NO_ELIGIBLE_SOURCE}


def test_a_source_without_a_usable_raer_is_excluded_with_its_reason() -> None:
    r = run(sources=[src(raer=raer(RaerState.UNAVAILABLE))])
    assert r.proposals == () and target_reasons(r) == {"0002": T.SOURCE_RAER_UNAVAILABLE}
    assert r.evaluated_pairs == 0


def test_a_source_with_another_comparison_key_is_not_comparable() -> None:
    r = run(sources=[src(raer=raer(RaerState.PROVISIONAL, value="3.5"))])
    assert r.proposals == () and target_reasons(r) == {"0002": T.RAER_NOT_COMPARABLE}
    assert r.evaluated_pairs == 0


def test_an_improvement_below_the_minimum_is_not_a_rotation_however_much_cash_is_short() -> None:
    # 改善幅 9 - 7.5 = 1.5 < 2。買付余力が 0 円でも、足りなくても、SELL を強めない
    for cash in (D("0"), D("50000"), D("99999")):
        r = run(available_cash=cash, sources=[src(raer=raer(value="7.5"))])
        assert r.proposals == (), cash
        assert target_reasons(r) == {"0002": T.INSUFFICIENT_IMPROVEMENT}


def test_an_improvement_exactly_at_the_minimum_is_enough() -> None:
    r = run(sources=[src(raer=raer(value="7"))])  # 9 - 7 = 2
    assert len(r.proposals) == 1
    r = run(sources=[src(raer=raer(value="7.01"))])  # 1.99
    assert r.proposals == ()


def test_a_negative_improvement_is_never_a_rotation() -> None:
    r = run(sources=[src(raer=raer(value="12"))])
    assert r.proposals == () and target_reasons(r) == {"0002": T.INSUFFICIENT_IMPROVEMENT}


def test_a_confidence_below_the_floor_on_either_side_blocks_the_pair() -> None:
    p = rpol(min_confidence=ConfidenceLevel.MEDIUM)
    low_source = run(
        policy=p, sources=[src(raer=raer(value="3.5", confidence=ConfidenceLevel.LOW))]
    )
    assert low_source.proposals == ()
    assert target_reasons(low_source) == {"0002": T.CONFIDENCE_BELOW_MINIMUM}
    low_target = run(
        policy=p, candidates=[cand(raer=raer(value="9", confidence=ConfidenceLevel.LOW))]
    )
    assert low_target.proposals == ()
    assert target_reasons(low_target) == {"0002": T.CONFIDENCE_BELOW_MINIMUM}
    ok = run(policy=p, sources=[src(raer=raer(value="3.5", confidence=ConfidenceLevel.MEDIUM))])
    assert len(ok.proposals) == 1


def test_an_unknown_confidence_is_below_every_floor() -> None:
    r = run(sources=[src(raer=raer(value="3.5", confidence=None))])
    assert r.proposals == () and target_reasons(r) == {"0002": T.CONFIDENCE_BELOW_MINIMUM}


def test_the_reasons_follow_the_order_of_the_filters() -> None:
    # 適格 -> RAER -> 比較可能性 -> 改善幅 -> 信頼度 -> 使用済み -> 資金。最後に残った段階の理由
    mixed = [
        src("5001", eligibility=cr.SourceEligibility.UNKNOWN),
        src("5002", raer=raer(RaerState.UNAVAILABLE)),
    ]
    assert target_reasons(run(sources=mixed)) == {"0002": T.SOURCE_RAER_UNAVAILABLE}
    mixed.append(src("5003", raer=raer(value="8.5")))
    assert target_reasons(run(sources=mixed)) == {"0002": T.INSUFFICIENT_IMPROVEMENT}
    mixed.append(src("5004", raer=raer(value="3", confidence=None)))
    assert target_reasons(run(sources=mixed)) == {"0002": T.CONFIDENCE_BELOW_MINIMUM}


# --- 4 評価の統計(SHADOW_ONLY でも残る)-----------------------------------------------------------


def test_the_statistics_count_the_comparable_pairs_and_keep_the_best_improvement() -> None:
    r = run(sources=[src("5001", raer=raer(value="3.5")), src("5002", raer=raer(value="6"))])
    assert r.evaluated_pairs == 2
    assert r.best_improvement == D("5.5")
    (t,) = r.targets
    assert t.best_improvement == D("5.5")


def test_a_pair_below_the_minimum_improvement_still_counts_as_evaluated() -> None:
    r = run(sources=[src(raer=raer(value="8"))])
    assert r.evaluated_pairs == 1 and r.best_improvement == D("1")


def test_the_best_improvement_can_be_negative() -> None:
    r = run(sources=[src(raer=raer(value="12"))])
    assert r.best_improvement == D("-3")


# --- 5 方針が未決定 = SHADOW_ONLY(提案を出さない)------------------------------------------------


def test_an_undecided_policy_is_shadow_only_and_keeps_the_statistics() -> None:
    r = run(policy=empty_rpol())
    assert r.status is cr.RotationStatus.SHADOW_ONLY
    assert r.proposals == ()
    assert r.policy_missing == cr.ROTATION_POLICY_ITEMS
    assert target_reasons(r) == {"0002": T.POLICY_INCOMPLETE}
    assert r.evaluated_pairs == 1 and r.best_improvement == D("5.5")
    assert (r.starting_cash, r.ending_cash) == (D("50000"), D("50000"))


@pytest.mark.parametrize(
    "item",
    ["min_raer_improvement", "proceeds_inclusion_ratio", "min_confidence"],
)
def test_one_undecided_item_alone_stops_every_proposal(item: str) -> None:
    r = run(policy=rpol(**{item: None}))
    assert r.status is cr.RotationStatus.SHADOW_ONLY and r.proposals == ()
    assert r.policy_missing == (item,)


def test_a_shadow_has_no_executable_trade_even_with_a_huge_improvement() -> None:
    r = run(policy=rpol(min_raer_improvement=None), sources=[src(raer=raer(value="-50"))])
    assert r.proposals == () and r.status is cr.RotationStatus.SHADOW_ONLY


def test_a_shadow_with_nothing_to_evaluate_is_still_shadow_only() -> None:
    r = run(policy=empty_rpol(), candidates=[cand()], available_cash=D("1000000"))
    assert r.status is cr.RotationStatus.SHADOW_ONLY and r.targets == ()


# --- 6 UNAVAILABLE / NO_ROTATION --------------------------------------------------------------


def test_an_unavailable_allocation_makes_the_rotation_unavailable() -> None:
    r = run(available_cash=None)
    assert r.status is cr.RotationStatus.UNAVAILABLE
    assert r.unavailable_reason is cr.RotationUnavailableReason.ALLOCATION_UNAVAILABLE
    assert r.proposals == () and r.targets == ()
    assert (r.starting_cash, r.ending_cash) == (None, None)
    assert r.evaluated_pairs == 0 and r.best_improvement is None


def test_an_allocation_with_an_undecided_policy_is_unavailable_for_the_rotation_too() -> None:
    r = run(allocation_policy=pt.AllocationPolicy(None, None, None), constraints=cons())
    assert r.status is cr.RotationStatus.UNAVAILABLE


def test_the_unavailable_allocation_wins_over_an_undecided_rotation_policy() -> None:
    r = run(available_cash=None, policy=empty_rpol())
    assert r.status is cr.RotationStatus.UNAVAILABLE
    assert r.policy_missing == cr.ROTATION_POLICY_ITEMS


def test_when_no_candidate_was_short_of_cash_there_is_nothing_to_rotate() -> None:
    r = run(available_cash=D("1000000"))
    assert r.status is cr.RotationStatus.NO_ROTATION
    assert r.targets == () and r.proposals == ()
    assert r.starting_cash == r.ending_cash


def test_only_a_candidate_that_was_short_of_cash_alone_is_a_target() -> None:
    cands = [
        cand("0002", raer=raer(value="9")),  # 資金不足
        cand("0003", raer=raer(value="1")),  # hurdle 未満
        cand("0004", raer=raer(RaerState.UNAVAILABLE)),  # RAER なし
        cand("0005", raer=raer(value="8"), sector=None),
    ]
    r = run(candidates=cands, constraints=cons(max_sector_ratio=D("0.5")), sector_exposure={})
    assert set(target_reasons(r)) == {"0002"}


def test_a_candidate_allocated_in_full_is_not_a_target() -> None:
    cands = [cand("0002", raer=raer(value="9")), cand("0003", raer=raer(value="8"))]
    r = run(candidates=cands, available_cash=D("100000"))
    assert set(target_reasons(r)) == {"0003"}


# --- 7 売却後の再判定(比率の上限は売却で厳しくなる)-------------------------------------------


def test_a_sale_tightens_the_ratio_cap_of_the_purchase() -> None:
    # 総額 1,000,000(5001 の 400,000 を含む)。比率の上限 0.1: b / (T' + b) <= 0.1 -> b <= T' / 9。
    # 売却前: b <= 111,111 -> 1 単元(需要 100 株)。k 単元の売却で T' = 1,000,000 - 100,000 k。
    # k = 1: 資金 80,000 < 100,000。k = 2: T' = 800,000 -> 88,888 で 0 単元。再判定すると提案なし
    r = run(
        available_cash=D("0"),
        portfolio_value=D("1000000"),
        constraints=cons(max_single_stock_ratio=D("0.1")),
        sources=[src(shares_held=400, current_price=D("1000"), raer=raer(value="3"))],
    )
    assert r.proposals == ()
    assert target_reasons(r) == {"0002": T.CANNOT_FUND_PURCHASE}


def test_the_purchase_is_limited_by_the_ratio_cap_after_the_sale() -> None:
    # 総額 2,000,000。売却前の上限は b <= 222,222(2 単元 = 需要 200 株)。
    # k = 2: T' = 1,800,000 -> 上限 2 単元・資金 160,000 -> 1 単元。k = 3: T' = 1,700,000 -> 1 単元
    # 再判定をしないと k = 3 で資金 240,000 -> 2 単元と誤る。正しくは 1 単元・k = 2
    r = run(
        available_cash=D("0"),
        portfolio_value=D("2000000"),
        constraints=cons(max_single_stock_ratio=D("0.1")),
        sources=[src(shares_held=600, current_price=D("1000"), raer=raer(value="3"))],
    )
    (p,) = r.proposals
    assert (p.sell_leg.sell_shares, p.buy_leg.shares) == (200, 100)


def test_a_ratio_cap_in_force_needs_the_portfolio_value_for_the_rotation_too() -> None:
    allocation = make_allocation(
        portfolio_value=D("2000000"), constraints=cons(max_single_stock_ratio=D("0.1"))
    )
    assert allocation.status is not pt.AllocationStatus.UNAVAILABLE
    r = run(
        allocation=allocation,
        constraints=cons(max_single_stock_ratio=D("0.1")),
        portfolio_value=None,
    )
    assert r.status is cr.RotationStatus.UNAVAILABLE
    assert r.unavailable_reason is cr.RotationUnavailableReason.PORTFOLIO_VALUE_UNAVAILABLE


def test_no_ratio_cap_in_force_needs_no_portfolio_value() -> None:
    r = run(portfolio_value=None)
    assert r.status is cr.RotationStatus.ROTATION_PROPOSED


def test_the_missing_exposures_are_not_needed_when_nothing_is_a_target() -> None:
    allocation = make_allocation(available_cash=D("1000000"))
    r = run(
        allocation=allocation,
        available_cash=None,
        constraints=cons(max_sector_ratio=D("0.5")),
        sector_exposure=None,
    )
    assert r.status is cr.RotationStatus.NO_ROTATION


def test_the_sector_exposure_of_the_source_is_released_by_the_sale() -> None:
    # 業種の上限 0.2・総額 4,000,000・電気機器の保有 600,000(うち入替元が 500,000)・目標も電気機器。
    # 売却前: b <= (800,000 - 600,000) / 0.8 = 250,000(2 単元 = 需要 200 株)。
    # k = 3(300 株)の売却で保有 300,000・総額 3,700,000 -> 上限 550,000、算入 240,000 -> 2 単元。
    # 売却による業種の保有の減少を認めないと、上限は 175,000(1 単元)に落ちて 200 株は買えない
    r = run(
        available_cash=D("0"),
        portfolio_value=D("4000000"),
        constraints=cons(max_sector_ratio=D("0.2")),
        sector_exposure={"電気機器": D("600000")},
        sources=[
            src(shares_held=500, current_price=D("1000"), sector="電気機器", raer=raer(value="3"))
        ],
    )
    (p,) = r.proposals
    assert (p.sell_leg.sell_shares, p.buy_leg.shares) == (300, 200)


# --- 8 段階(再判定は optimize_allocation と同じ規則)-----------------------------------


def test_the_tier_ratio_applies_to_the_purchase_after_the_sale() -> None:
    tiers = {t: D("0.5") for t in pt.PriceTier}
    r = run(
        candidates=[cand(price_tier=pt.PriceTier.ENTRY)],
        allocation_policy=apol(tier_quantity_caps=tiers),
        sources=[src(shares_held=5000)],
    )
    (p,) = r.proposals
    assert p.buy_leg.shares == 300  # 他の上限で 600 株 x 0.5
    assert p.buy_leg.price_tier_used is pt.PriceTier.ENTRY


def test_the_high_risk_exposure_of_the_source_is_released_by_the_sale() -> None:
    # high-risk の上限 0.2・総額 4,000,000・high-risk の保有 600,000(うち入替元が 500,000)。
    # 売却前: b <= (800,000 - 600,000) / 0.8 = 250,000(2 単元 = 需要 200 株)。
    # k = 3 の売却で保有 300,000・総額 3,700,000 -> 上限 550,000、算入 240,000 -> 2 単元
    risky = raer(value="3", risk_flags=HIGH_RISK)
    r = run(
        available_cash=D("0"),
        portfolio_value=D("4000000"),
        constraints=cons(max_high_risk_ratio=D("0.2")),
        candidates=[cand(raer=raer(value="9", risk_flags=HIGH_RISK))],
        high_risk_exposure=D("600000"),
        sector_exposure={},
        sources=[src(shares_held=500, current_price=D("1000"), raer=risky)],
    )
    (p,) = r.proposals
    assert (p.sell_leg.sell_shares, p.buy_leg.shares) == (300, 200)


def test_a_source_that_is_not_high_risk_does_not_release_the_high_risk_exposure() -> None:
    # 入替元の risk_flags が high_risk_flags に含まれない語なら、売却でも保有は減らない。
    # k = 2: 総額 3,800,000 -> 上限 2 単元・算入 160,000 -> 1 単元。k = 3 以降は上限が 1 単元
    other = frozenset({RiskFlag.MAJOR_CUSTOMER_DEPENDENCY.value})
    r = run(
        available_cash=D("0"),
        portfolio_value=D("4000000"),
        constraints=cons(max_high_risk_ratio=D("0.2")),
        candidates=[cand(raer=raer(value="9", risk_flags=HIGH_RISK))],
        high_risk_exposure=D("600000"),
        sector_exposure={},
        sources=[
            src(shares_held=500, current_price=D("1000"), raer=raer(value="3", risk_flags=other))
        ],
    )
    (p,) = r.proposals
    assert (p.sell_leg.sell_shares, p.buy_leg.shares) == (200, 100)


def test_the_purchase_never_exceeds_the_demand_even_when_the_sale_loosens_the_caps() -> None:
    # 業種の上限 0.2・総額 4,500,000・電気機器の保有 700,000(うち入替元が 600,000)。需要は 2 単元。
    # 入替元は 2,000 円 x 300 株: k = 2 で算入 320,000 -> 3 単元だが、需要の 2 単元までにする。
    # 需要を超えて k = 3 まで進むと、4 単元を買ってしまう
    r = run(
        available_cash=D("0"),
        portfolio_value=D("4500000"),
        constraints=cons(max_sector_ratio=D("0.2")),
        sector_exposure={"電気機器": D("700000")},
        sources=[
            src(shares_held=300, current_price=D("2000"), sector="電気機器", raer=raer(value="3"))
        ],
    )
    (p,) = r.proposals
    assert (p.sell_leg.sell_shares, p.buy_leg.shares) == (200, 200)


@pytest.mark.parametrize(
    "which", ["max_single_stock_ratio", "max_sector_ratio", "max_high_risk_ratio"]
)
@pytest.mark.parametrize("portfolio", [None, D("0")])
def test_each_ratio_cap_alone_needs_a_usable_portfolio_value_for_the_rotation(
    which: str, portfolio: Decimal | None
) -> None:
    caps = cons(**{which: D("0.5")})
    allocation = make_allocation(
        portfolio_value=D("2000000"),
        constraints=caps,
        sector_exposure={},
        high_risk_exposure=D("0"),
    )
    assert target_reasons(
        run(
            allocation=allocation,
            constraints=caps,
            portfolio_value=D("2000000"),
            sector_exposure={},
            high_risk_exposure=D("0"),
        )
    ) == {"0002": T.PROPOSED}
    r = run(
        allocation=allocation,
        constraints=caps,
        portfolio_value=portfolio,
        sector_exposure={},
        high_risk_exposure=D("0"),
    )
    assert r.unavailable_reason is cr.RotationUnavailableReason.PORTFOLIO_VALUE_UNAVAILABLE


@pytest.mark.parametrize(
    ("which", "missing"),
    [("max_sector_ratio", "sector_exposure"), ("max_high_risk_ratio", "high_risk_exposure")],
)
def test_the_sector_and_high_risk_caps_need_their_own_exposure_for_the_rotation(
    which: str, missing: str
) -> None:
    caps = cons(**{which: D("0.5")})
    allocation = make_allocation(
        portfolio_value=D("2000000"),
        constraints=caps,
        sector_exposure={},
        high_risk_exposure=D("0"),
    )
    given: dict[str, object] = {"sector_exposure": {}, "high_risk_exposure": D("0")}
    given[missing] = None
    r = run(allocation=allocation, constraints=caps, portfolio_value=D("2000000"), **given)
    assert r.unavailable_reason is cr.RotationUnavailableReason.PORTFOLIO_VALUE_UNAVAILABLE


def test_the_amounts_are_exact_beyond_the_default_decimal_precision() -> None:
    price = D("500.00000000000000000000000000001")  # 35 桁。既定の 28 桁で丸めると合わなくなる
    r = run(sources=[src(current_price=price)])
    (p,) = r.proposals
    assert Fraction(p.sell_leg.estimated_proceeds) == Fraction(price) * p.sell_leg.sell_shares
    assert Fraction(p.sell_leg.counted_proceeds) == Fraction(
        p.sell_leg.estimated_proceeds
    ) * Fraction(D("0.8"))


def test_a_string_is_not_a_sequence_of_candidates_or_sources() -> None:
    with pytest.raises(cr.RotationContractError, match="must be a sequence"):
        run(**{"allocation": make_allocation(), "candidates": "0002"})
    with pytest.raises(cr.RotationContractError, match="must be a sequence"):
        run(**{"allocation": make_allocation(), "sources": "5001"})


# --- 9 型の検証(入力)----------------------------------------------------------------------------


def _bad_sources() -> list[tuple[str, dict[str, object]]]:
    return [
        ("empty code", {"stock_code": ""}),
        ("blank code", {"stock_code": " "}),
        ("zero shares", {"shares_held": 0}),
        ("negative shares", {"shares_held": -100}),
        ("bool shares", {"shares_held": True}),
        ("float shares", {"shares_held": 100.0}),
        ("zero price", {"current_price": D("0")}),
        ("negative price", {"current_price": D("-1")}),
        ("float price", {"current_price": 500.0}),
        ("int price", {"current_price": 500}),
        ("nan price", {"current_price": D("NaN")}),
        ("zero unit", {"minimum_trading_unit": 0}),
        ("bool unit", {"minimum_trading_unit": True}),
        ("raer wrong type", {"raer": "x"}),
        ("eligibility wrong type", {"eligibility": "ELIGIBLE"}),
        ("blank sector", {"sector": " "}),
        ("sector not str", {"sector": 5}),
    ]


@pytest.mark.parametrize(("label", "over"), _bad_sources(), ids=[b[0] for b in _bad_sources()])
def test_a_malformed_source_is_rejected(label: str, over: dict[str, object]) -> None:
    with pytest.raises(cr.RotationContractError):
        src(**over)


def test_a_source_may_have_no_sector_and_an_unusable_raer() -> None:
    assert src(sector=None).sector is None
    assert src(raer=raer(RaerState.UNAVAILABLE)).raer.value is None


def test_the_source_boundaries_are_continuous() -> None:
    assert src(shares_held=1).shares_held == 1
    assert src(minimum_trading_unit=1).minimum_trading_unit == 1
    assert src(current_price=D("0.0001")).current_price == D("0.0001")


def _bad_policies() -> list[tuple[str, dict[str, object]]]:
    return [
        ("zero improvement", {"min_raer_improvement": D("0")}),
        ("negative improvement", {"min_raer_improvement": D("-1")}),
        ("float improvement", {"min_raer_improvement": 2.0}),
        ("int improvement", {"min_raer_improvement": 2}),
        ("nan improvement", {"min_raer_improvement": D("NaN")}),
        ("zero ratio", {"proceeds_inclusion_ratio": D("0")}),
        ("ratio one", {"proceeds_inclusion_ratio": D("1")}),
        ("ratio above one", {"proceeds_inclusion_ratio": D("1.5")}),
        ("negative ratio", {"proceeds_inclusion_ratio": D("-0.1")}),
        ("float ratio", {"proceeds_inclusion_ratio": 0.8}),
        ("confidence wrong type", {"min_confidence": "LOW"}),
    ]


@pytest.mark.parametrize(("label", "over"), _bad_policies(), ids=[b[0] for b in _bad_policies()])
def test_a_malformed_policy_is_rejected(label: str, over: dict[str, object]) -> None:
    with pytest.raises(cr.RotationContractError):
        rpol(**over)


def test_the_policy_boundaries_are_continuous() -> None:
    assert rpol(min_raer_improvement=D("0.0001")).min_raer_improvement == D("0.0001")
    assert rpol(proceeds_inclusion_ratio=D("0.9999")).proceeds_inclusion_ratio == D("0.9999")
    assert rpol(proceeds_inclusion_ratio=D("0.0001")).proceeds_inclusion_ratio == D("0.0001")


def test_the_policy_lists_what_is_undecided_in_a_fixed_order() -> None:
    assert cr.ROTATION_POLICY_ITEMS == (
        "min_raer_improvement",
        "proceeds_inclusion_ratio",
        "min_confidence",
    )
    assert empty_rpol().missing() == cr.ROTATION_POLICY_ITEMS
    assert rpol().missing() == () and rpol().is_complete()
    assert not empty_rpol().is_complete()


# --- 10 型の検証(出力)---------------------------------------------------------------------------


def _sell(**over: object) -> cr.SellLeg:
    base: dict[str, object] = {
        "stock_code": "5001",
        "sell_shares": 900,
        "estimated_proceeds": D("450000"),
        "counted_proceeds": D("360000"),
        "exit_class": ExitClass.CAPITAL_ROTATION,
        "quantity_kind": cr.SellQuantityKind.PARTIAL,
    }
    base.update(over)
    return cr.SellLeg(**base)  # type: ignore[arg-type]


def _buy(**over: object) -> cr.BuyLeg:
    base: dict[str, object] = {
        "stock_code": "0002",
        "shares": 400,
        "amount": D("400000"),
        "price_tier_used": None,
    }
    base.update(over)
    return cr.BuyLeg(**base)  # type: ignore[arg-type]


def _proposal(**over: object) -> cr.RotationProposal:
    base: dict[str, object] = {
        "proposal_id": "rotation:owner-a:5001:0002",
        "sell_leg": _sell(),
        "buy_leg": _buy(),
        "improvement": D("5.5"),
        "projected_remaining_cash": D("10000"),
    }
    base.update(over)
    return cr.RotationProposal(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "over",
    [
        {"stock_code": ""},
        {"sell_shares": 0},
        {"sell_shares": True},
        {"estimated_proceeds": D("0")},
        {"estimated_proceeds": 450000.0},
        {"counted_proceeds": D("0")},
        {"counted_proceeds": D("450000")},  # 算入額 == 見込額(割合 1)は不可
        {"counted_proceeds": D("460000")},
        {"exit_class": ExitClass.RISK_EXIT},
        {"exit_class": ExitClass.NONE},
        {"quantity_kind": "PARTIAL"},
    ],
)
def test_a_malformed_sell_leg_is_rejected(over: dict[str, object]) -> None:
    with pytest.raises(cr.RotationContractError):
        _sell(**over)


@pytest.mark.parametrize(
    "over",
    [
        {"stock_code": " "},
        {"shares": 0},
        {"shares": True},
        {"amount": D("0")},
        {"amount": D("-1")},
        {"amount": 400000},
        {"price_tier_used": "ENTRY"},
    ],
)
def test_a_malformed_buy_leg_is_rejected(over: dict[str, object]) -> None:
    with pytest.raises(cr.RotationContractError):
        _buy(**over)


@pytest.mark.parametrize(
    "over",
    [
        {"proposal_id": ""},
        {"sell_leg": "x"},
        {"buy_leg": "x"},
        {"sell_leg": _sell(stock_code="0002")},  # 同じ銘柄の売りと買い
        {"improvement": D("0")},
        {"improvement": D("-1")},
        {"improvement": 5.5},
        {"projected_remaining_cash": D("-1")},
        {"projected_remaining_cash": 10000},
    ],
)
def test_a_malformed_proposal_is_rejected(over: dict[str, object]) -> None:
    with pytest.raises(cr.RotationContractError):
        _proposal(**over)


def test_the_full_and_partial_kinds_are_not_checked_against_a_position_the_leg_does_not_know() -> (
    None
):
    assert _sell(quantity_kind=cr.SellQuantityKind.FULL).quantity_kind is cr.SellQuantityKind.FULL


def _good() -> cr.RotationResult:
    return run()


def _with(result: cr.RotationResult, **over: object) -> cr.RotationResult:
    return dataclasses.replace(result, **over)  # type: ignore[arg-type]


def _inconsistent_results() -> list[
    tuple[str, Callable[[cr.RotationResult], cr.RotationResult], str]
]:
    """(名前, 壊し方, 拒否のメッセージに含まれる部分)。別の規則に隠れた拒否を区別する。"""
    shadow = run(policy=empty_rpol())
    unavailable = run(available_cash=None)
    has_proposal = "result.status: must agree"
    return [
        ("version", lambda r: _with(r, contract_version="C604-v1"), "result.contract_version"),
        ("cost model", lambda r: _with(r, cost_model="ESTIMATED"), "result.cost_model"),
        ("owner", lambda r: _with(r, owner=""), "result.owner"),
        ("status wrong type", lambda r: _with(r, status="ROTATION_PROPOSED"), "result.status"),
        ("proposed without proposals", lambda r: _with(r, proposals=()), has_proposal),
        (
            "no rotation with proposals",
            lambda r: _with(r, status=cr.RotationStatus.NO_ROTATION),
            has_proposal,
        ),
        (
            "reason on an available result",
            lambda r: _with(
                r, unavailable_reason=cr.RotationUnavailableReason.ALLOCATION_UNAVAILABLE
            ),
            "result.unavailable_reason: an available",
        ),
        (
            "available without starting cash",
            lambda r: _with(r, starting_cash=None),
            "result: an available result knows its cash",
        ),
        (
            "available without ending cash",
            lambda r: _with(r, ending_cash=None),
            "result: an available result knows its cash",
        ),
        (
            "cash chain broken",
            lambda r: _with(r, ending_cash=D("0")),
            "result.ending_cash: must follow",
        ),
        (
            "negative starting cash",
            lambda r: _with(r, starting_cash=D("-1")),
            "result.starting_cash",
        ),
        (
            "negative cash on a shadow",
            lambda r: _with(shadow, starting_cash=D("-1"), ending_cash=D("-1")),
            "result.starting_cash",
        ),
        (
            "target marked proposed without a proposal",
            lambda r: _with(r, targets=(*r.targets, cr.TargetEvaluation("0009", T.PROPOSED, None))),
            "result.targets: marks exactly",
        ),
        (
            "proposal without a proposed target",
            lambda r: _with(r, targets=(cr.TargetEvaluation("0002", T.NO_ELIGIBLE_SOURCE, None),)),
            "result.targets: marks exactly",
        ),
        (
            "duplicate target",
            lambda r: _with(r, targets=(*r.targets, *r.targets)),
            "result.targets: must not repeat",
        ),
        (
            "targets not a tuple",
            lambda r: _with(r, targets=list(r.targets)),
            "result.targets: must be a tuple",
        ),
        (
            "proposals not a tuple",
            lambda r: _with(r, proposals=list(r.proposals)),
            "result.proposals: must be a tuple",
        ),
        (
            "negative pairs",
            lambda r: _with(r, evaluated_pairs=-1),
            "result.evaluated_pairs: is below its minimum",
        ),
        (
            "pairs fewer than proposals",
            lambda r: _with(r, evaluated_pairs=0, best_improvement=None),
            "result.evaluated_pairs: is below the number",
        ),
        (
            "best improvement without pairs",
            lambda r: _with(r, evaluated_pairs=0),
            "result.best_improvement: is held exactly",
        ),
        (
            "pairs without best improvement",
            lambda r: _with(r, best_improvement=None),
            "result.best_improvement: is held exactly",
        ),
        (
            "proposal above the best improvement",
            lambda r: _with(r, best_improvement=D("1")),
            "result.best_improvement: is below a proposal",
        ),
        (
            "policy missing on an available result",
            lambda r: _with(r, policy_missing=("min_confidence",)),
            "result.policy_missing: is held exactly",
        ),
        (
            "policy name outside the vocabulary",
            lambda r: _with(shadow, policy_missing=("x",)),
            "result.policy_missing: has a name outside",
        ),
        (
            "shadow with a proposal",
            lambda r: _with(shadow, proposals=r.proposals),
            has_proposal,
        ),
        (
            "shadow without the missing items",
            lambda r: _with(shadow, policy_missing=()),
            "result.policy_missing: is held exactly",
        ),
        (
            "shadow with a decided target reason",
            lambda r: _with(
                shadow, targets=(cr.TargetEvaluation("0002", T.NO_ELIGIBLE_SOURCE, None),)
            ),
            "result.targets: an incomplete policy holds only",
        ),
        (
            "shadow changes the cash",
            lambda r: _with(shadow, ending_cash=D("1")),
            "result.ending_cash: must follow",
        ),
        (
            "incomplete-policy reason on a complete policy",
            lambda r: _with(
                r,
                status=cr.RotationStatus.NO_ROTATION,
                proposals=(),
                ending_cash=r.starting_cash,
                targets=(cr.TargetEvaluation("0002", T.POLICY_INCOMPLETE, None),),
            ),
            "result.targets: a complete policy has no",
        ),
        (
            "projected cash differs",
            lambda r: _with(
                r,
                proposals=(
                    dataclasses.replace(r.proposals[0], projected_remaining_cash=D("11000")),
                ),
            ),
            "result.proposals: has a remaining cash",
        ),
        (
            "id differs from the legs",
            lambda r: _with(
                r,
                proposals=(
                    dataclasses.replace(r.proposals[0], proposal_id="rotation:owner-a:5001:9999"),
                ),
            ),
            "result.proposals: has an id that differs",
        ),
        (
            "unavailable without a reason",
            lambda r: _with(unavailable, unavailable_reason=None),
            "result.unavailable_reason: an unavailable result needs",
        ),
        (
            "unavailable with a proposal",
            lambda r: _with(unavailable, proposals=r.proposals),
            "result: an unavailable result holds no proposal",
        ),
        (
            "unavailable with targets",
            lambda r: _with(unavailable, targets=r.targets),
            "result: an unavailable result holds no proposal",
        ),
        (
            "unavailable with starting cash",
            lambda r: _with(unavailable, starting_cash=D("1")),
            "result: an unavailable result knows no cash",
        ),
        (
            "unavailable with ending cash",
            lambda r: _with(unavailable, ending_cash=D("1")),
            "result: an unavailable result knows no cash",
        ),
        (
            "unavailable with pairs",
            lambda r: _with(unavailable, evaluated_pairs=1, best_improvement=D("1")),
            "result: an unavailable result evaluated nothing",
        ),
        (
            "unavailable with a best improvement",
            lambda r: _with(unavailable, best_improvement=D("1")),
            "result: an unavailable result evaluated nothing",
        ),
    ]


@pytest.mark.parametrize(
    ("build", "fragment"),
    [(b[1], b[2]) for b in _inconsistent_results()],
    ids=[b[0] for b in _inconsistent_results()],
)
def test_an_inconsistent_result_is_rejected(
    build: Callable[[cr.RotationResult], cr.RotationResult], fragment: str
) -> None:
    with pytest.raises(cr.RotationContractError) as info:
        build(_good())
    assert fragment in str(info.value)


def test_a_proposal_whose_cash_chain_overspends_is_rejected() -> None:
    r = run()
    (p,) = r.proposals
    bigger = dataclasses.replace(p, buy_leg=dataclasses.replace(p.buy_leg, amount=D("500000")))
    with pytest.raises(cr.RotationContractError, match="remaining cash"):
        dataclasses.replace(r, proposals=(bigger,))


def test_two_proposals_cannot_sell_or_buy_the_same_stock() -> None:
    r = _two_targets()
    first, second = r.proposals
    same_sell = dataclasses.replace(
        second,
        proposal_id="rotation:owner-a:5001:0003",
        sell_leg=dataclasses.replace(second.sell_leg, stock_code="5001"),
    )
    with pytest.raises(cr.RotationContractError, match="once on each side"):
        dataclasses.replace(r, proposals=(first, same_sell))
    same_buy = dataclasses.replace(
        second,
        proposal_id="rotation:owner-a:5002:0002",
        buy_leg=dataclasses.replace(second.buy_leg, stock_code="0002"),
    )
    with pytest.raises(cr.RotationContractError, match="once on each side"):
        dataclasses.replace(r, proposals=(first, same_buy))


def test_a_stock_cannot_be_sold_and_bought_in_the_same_result() -> None:
    r = _two_targets()
    first, second = r.proposals
    crossing = dataclasses.replace(
        second,
        proposal_id="rotation:owner-a:0002:0003",
        sell_leg=dataclasses.replace(second.sell_leg, stock_code="0002"),
    )
    with pytest.raises(cr.RotationContractError, match="sell and buy the same stock"):
        dataclasses.replace(r, proposals=(first, crossing))


# --- 11 入力の拒否(例外のメッセージに値を含めない)---------------------------------------

_DISTINCT = D("424242.5")


def _bad_inputs() -> list[tuple[str, dict[str, object]]]:
    return [
        ("owner empty", {"owner": ""}),
        ("owner not a str", {"owner": 424242}),
        ("owner differs from the allocation", {"owner": "owner-b"}),
        ("allocation wrong type", {"allocation": "x"}),
        ("candidates not a sequence", {"candidates": "0002"}),
        ("candidates not candidates", {"candidates": [424242]}),
        ("duplicate candidate", {"candidates": [cand(), cand()]}),
        ("candidates differ from the allocation", {"candidates": [cand("0002"), cand("0003")]}),
        ("sources not a sequence", {"sources": "5001"}),
        ("sources not sources", {"sources": [424242]}),
        ("duplicate source", {"sources": [src(), src()]}),
        ("constraints wrong type", {"constraints": "x"}),
        ("allocation policy wrong type", {"allocation_policy": "x"}),
        (
            "allocation policy incomplete",
            {"allocation_policy": pt.AllocationPolicy(None, None, None)},
        ),
        ("policy wrong type", {"policy": "x"}),
        ("portfolio float", {"portfolio_value": 424242.5}),
        ("portfolio negative", {"portfolio_value": -_DISTINCT}),
        ("sector exposure not a mapping", {"sector_exposure": [1]}),
        ("sector exposure negative", {"sector_exposure": {"電気機器": -_DISTINCT}}),
        ("sector exposure float", {"sector_exposure": {"電気機器": 424242.5}}),
        ("sector exposure key blank", {"sector_exposure": {" ": D("1")}}),
        ("high risk exposure float", {"high_risk_exposure": 424242.5}),
        ("high risk exposure negative", {"high_risk_exposure": -_DISTINCT}),
    ]


@pytest.mark.parametrize(("label", "over"), _bad_inputs(), ids=[b[0] for b in _bad_inputs()])
def test_a_malformed_input_is_rejected_without_echoing_a_value(
    label: str, over: dict[str, object]
) -> None:
    with pytest.raises(cr.RotationContractError) as info:
        run(**{"allocation": make_allocation(), **over})
    text = str(info.value)
    assert text
    for needle in ("424242", "0002", "0003", "5001", "owner-a", "owner-b"):
        assert needle not in text


@pytest.mark.parametrize(
    ("over", "field"),
    [
        ({"owner": " "}, "owner"),
        ({"allocation": "x"}, "allocation"),
        ({"candidates": "0002"}, "candidates"),
        ({"candidates": [cand("0002"), cand("0003")]}, "candidates"),
        ({"sources": "5001"}, "sources"),
        ({"sources": [src(), src()]}, "sources"),
        ({"constraints": "x"}, "constraints"),
        ({"allocation_policy": "x"}, "allocation_policy"),
        ({"allocation_policy": pt.AllocationPolicy(None, None, None)}, "allocation_policy"),
        ({"policy": "x"}, "policy"),
        ({"portfolio_value": D("-0.01")}, "portfolio_value"),
        ({"sector_exposure": {" ": D("1")}}, "sector_exposure"),
        ({"high_risk_exposure": D("-0.01")}, "high_risk_exposure"),
    ],
)
def test_the_evaluation_names_the_rejected_input_itself(
    over: dict[str, object], field: str
) -> None:
    with pytest.raises(cr.RotationContractError) as info:
        run(**{"allocation": make_allocation(), **over})
    assert str(info.value).startswith(f"{field}:")


def test_the_inputs_may_be_tuples_and_are_not_mutated() -> None:
    sources = (src("5002"), src("5001"))
    candidates = (cand(),)
    before = (list(sources), list(candidates))
    run(sources=sources, candidates=candidates)
    assert (list(sources), list(candidates)) == before


def test_the_result_does_not_depend_on_the_input_order() -> None:
    sources = [src("5001", raer=raer(value="3")), src("5002", raer=raer(value="4")), src("5003")]
    cands = [cand("0002", raer=raer(value="9")), cand("0003", raer=raer(value="8"))]
    a = _two_targets(sources=sources, candidates=cands)
    b = _two_targets(sources=list(reversed(sources)), candidates=list(reversed(cands)))
    assert a == b


def _tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def test_the_error_is_built_in_exactly_one_place_from_names_only() -> None:
    calls = [
        n
        for n in ast.walk(_tree())
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", "")) == "RotationContractError"
    ]
    assert len(calls) == 1
    (call,) = calls
    assert isinstance(call.args[0], ast.JoinedStr)
    interpolated = [v.value for v in call.args[0].values if isinstance(v, ast.FormattedValue)]
    assert all(isinstance(v, ast.Name) and v.id in {"name", "problem"} for v in interpolated)


# --- 12 参照実装(1 単元ずつ・直接の不等式)との一致 ---------------------------------


@dataclass(frozen=True)
class Scenario:
    allocation_cash: Decimal
    portfolio: Decimal | None
    candidates: tuple[pt.AllocationCandidate, ...]
    sources: tuple[cr.RotationSource, ...]
    constraints: pt.AllocationConstraints
    allocation_policy: pt.AllocationPolicy
    policy: cr.RotationPolicy
    sector_exposure: dict[str, Decimal]
    high_risk_exposure: Decimal


def build_allocation(s: Scenario) -> pt.AllocationResult:
    return pa.optimize_allocation(
        owner="owner-a",
        available_cash=s.allocation_cash,
        portfolio_value=s.portfolio,
        candidates=s.candidates,
        constraints=s.constraints,
        policy=s.allocation_policy,
        sector_exposure=s.sector_exposure,
        high_risk_exposure=s.high_risk_exposure,
    )


def evaluate(s: Scenario) -> cr.RotationResult:
    return cr.evaluate_capital_rotation(
        owner="owner-a",
        allocation=build_allocation(s),
        candidates=s.candidates,
        sources=s.sources,
        constraints=s.constraints,
        allocation_policy=s.allocation_policy,
        policy=s.policy,
        portfolio_value=s.portfolio,
        sector_exposure=s.sector_exposure,
        high_risk_exposure=s.high_risk_exposure,
    )


def _conf(level: ConfidenceLevel | None) -> int:
    return {ConfidenceLevel.HIGH: 2, ConfidenceLevel.MEDIUM: 1, ConfidenceLevel.LOW: 0, None: -1}[
        level
    ]


def _largest(ok: Callable[[int], bool], upper: int) -> int:
    """ok(n) が成り立つ最大の n(0 <= n <= upper)。ok は単調(成り立てば小さい n でも成り立つ)."""
    low, high = 0, upper
    while low < high:
        mid = (low + high + 1) // 2
        if ok(mid):
            low = mid
        else:
            high = mid - 1
    return low


def reference(s: Scenario) -> list[tuple[str, int, str, int]]:
    """(売却銘柄, 売却株数, 購入銘柄, 購入株数) の並び。定義そのもの(直接の不等式・全探索)。"""
    allocation = build_allocation(s)
    assert s.policy.is_complete() and allocation.status is not pt.AllocationStatus.UNAVAILABLE
    k, ap, rp = s.constraints, s.allocation_policy, s.policy
    assert rp.min_raer_improvement is not None and rp.proceeds_inclusion_ratio is not None
    assert rp.min_confidence is not None and k.max_single_stock_amount is not None
    by_code = {c.stock_code: c for c in s.candidates}
    allocated = [a for a in allocation.allocations if a.decision is pt.CandidateDecision.ALLOCATED]
    targets = [
        a
        for a in allocation.allocations
        if a.decision is pt.CandidateDecision.NOT_ALLOCATED
        and a.reason_code is pt.ReasonCode.INSUFFICIENT_CASH_REMAINING
    ]
    cash = Fraction(allocation.remaining_cash or 0)
    total = (Fraction(s.portfolio) if s.portfolio is not None else Fraction(0)) + Fraction(
        allocation.total_allocated
    )
    sector = {name: Fraction(v) for name, v in s.sector_exposure.items()}
    hr = Fraction(s.high_risk_exposure)

    def risky(raer_: RaerResult) -> bool:
        return bool(raer_.risk_flags_available and (raer_.risk_flags & k.high_risk_flags))

    hr_in_force = k.max_high_risk_ratio is not None and k.max_high_risk_ratio < 1
    for a in allocated:
        c = by_code[a.stock_code]
        if c.sector is not None:
            sector[c.sector] = sector.get(c.sector, Fraction(0)) + Fraction(a.amount)
        if hr_in_force and risky(c.raer):
            hr += Fraction(a.amount)

    held_codes = {c.stock_code for c in s.candidates}
    used_sources: set[str] = set()
    out: list[tuple[str, int, str, int]] = []
    for t in targets:
        c = by_code[t.stock_code]
        assert c.raer.value is not None
        pool = [
            x
            for x in s.sources
            if x.eligibility is cr.SourceEligibility.ELIGIBLE
            and x.stock_code not in held_codes
            and x.raer.state is not RaerState.UNAVAILABLE
            and x.raer.value is not None
            and x.raer.comparison_key == ap.comparison_key
        ]
        pool = [
            x
            for x in pool
            if c.raer.value - x.raer.value >= rp.min_raer_improvement  # type: ignore[operator]
            and _conf(c.raer.confidence) >= _conf(rp.min_confidence)
            and _conf(x.raer.confidence) >= _conf(rp.min_confidence)
            and x.stock_code not in used_sources
        ]
        pool.sort(key=lambda x: (-(c.raer.value - x.raer.value), x.stock_code))  # type: ignore[operator]
        for x in pool:
            best = (0, 0)  # (購入株数, 売却株数)
            sale_units = x.shares_held // x.minimum_trading_unit
            for sale_k in range(1, sale_units + 1):
                sold = sale_k * x.minimum_trading_unit
                est = Fraction(x.current_price) * sold
                counted = est * Fraction(rp.proceeds_inclusion_ratio)
                buy = _buy_shares(
                    c,
                    t,
                    k,
                    ap,
                    cash + counted,
                    total - est,
                    _sector_after(sector, x, est),
                    _hr_after(hr, x, est, k),
                    s.portfolio is not None,
                )
                if buy > best[0]:
                    best = (buy, sold)
            if best[0] > 0:
                buy, sold = best
                est = Fraction(x.current_price) * sold
                counted = est * Fraction(rp.proceeds_inclusion_ratio)
                spent = Fraction(c.current_price) * buy
                cash += counted - spent
                total += spent - est
                if c.sector is not None:
                    sector[c.sector] = sector.get(c.sector, Fraction(0)) + spent
                sector = _sector_after(sector, x, est)
                if hr_in_force and risky(c.raer):
                    hr += spent
                hr = _hr_after(hr, x, est, k)
                used_sources.add(x.stock_code)
                out.append((x.stock_code, sold, c.stock_code, buy))
                break
    return out


def _sector_after(
    sector: dict[str, Fraction], x: cr.RotationSource, est: Fraction
) -> dict[str, Fraction]:
    after = dict(sector)
    if x.sector is not None:
        after[x.sector] = max(Fraction(0), after.get(x.sector, Fraction(0)) - est)
    return after


def _hr_after(
    hr: Fraction, x: cr.RotationSource, est: Fraction, k: pt.AllocationConstraints
) -> Fraction:
    in_force = k.max_high_risk_ratio is not None and k.max_high_risk_ratio < 1
    if in_force and x.raer.risk_flags_available and (x.raer.risk_flags & k.high_risk_flags):
        return max(Fraction(0), hr - est)
    return hr


def _buy_shares(
    c: pt.AllocationCandidate,
    t: pt.CandidateAllocation,
    k: pt.AllocationConstraints,
    ap: pt.AllocationPolicy,
    funds: Fraction,
    total: Fraction,
    sector: dict[str, Fraction],
    hr: Fraction,
    has_portfolio: bool,
) -> int:
    """売却後の状態で、直接の不等式を満たす最大の購入株数(需要の範囲)。"""
    assert k.max_single_stock_amount is not None and ap.tier_quantity_caps is not None
    unit = k.minimum_trading_unit
    price = Fraction(c.current_price)
    position = Fraction(c.current_position_value)
    ratio_in_force = [
        r is not None and r < 1
        for r in (k.max_single_stock_ratio, k.max_sector_ratio, k.max_high_risk_ratio)
    ]
    if any(ratio_in_force) and (not has_portfolio or total <= 0):
        return 0
    sector_in_force, hr_in_force = ratio_in_force[1], ratio_in_force[2]
    if sector_in_force and c.sector is None:
        return 0
    if hr_in_force and not c.raer.risk_flags_available:
        return 0
    is_hr = bool(hr_in_force and (c.raer.risk_flags & k.high_risk_flags))

    def caps_ok(units: int) -> bool:
        a = price * units * unit
        if position + a > Fraction(k.max_single_stock_amount):  # type: ignore[arg-type]
            return False
        if ratio_in_force[0] and position + a > Fraction(k.max_single_stock_ratio) * (total + a):  # type: ignore[arg-type]
            return False
        if sector_in_force and c.sector is not None:
            held = sector.get(c.sector, Fraction(0))
            if held + a > Fraction(k.max_sector_ratio) * (total + a):  # type: ignore[arg-type]
                return False
        return not (
            is_hr and hr + a > Fraction(k.max_high_risk_ratio) * (total + a)  # type: ignore[arg-type]
        )

    base = _largest(caps_ok, 10**6)
    limit = base
    if c.price_tier is not None:
        limit = int(Fraction(base) * Fraction(ap.tier_quantity_caps[pt.PriceTier(c.price_tier)]))
    demand = t.cap_limited_shares // unit
    by_funds = int(funds // (price * unit)) if funds > 0 else 0
    return min(limit, by_funds, demand) * unit


def random_scenario(rng: random.Random) -> Scenario:
    n_cand = rng.randint(1, 3)
    n_src = rng.randint(0, 4)
    sectors = ["電気機器", "食料品", "情報通信", None]
    flag_sets = [frozenset(), frozenset(), HIGH_RISK]
    candidates = []
    for i in range(n_cand):
        position = rng.choice([0, 0, 100_000, 300_000])
        candidates.append(
            cand(
                f"{1000 + i}",
                kind=pt.CandidateKind.ADDITIONAL_BUY if position else pt.CandidateKind.NEW_BUY,
                current_price=D(rng.choice([700, 1000, 2500])),
                price_tier=rng.choice([None, *pt.PriceTier]),
                raer=raer(
                    value=str(rng.choice([5, 6, 8, 9, 10])),
                    confidence=rng.choice([*ConfidenceLevel, None]),
                    risk_flags=rng.choice(flag_sets),
                ),
                current_position_value=D(position),
                sector=rng.choice(sectors),
            )
        )
    sources = []
    for j in range(n_src):
        sources.append(
            src(
                f"{5000 + j}",
                shares_held=rng.choice([100, 300, 1000, 2500]),
                current_price=D(rng.choice([200, 500, 1200])),
                minimum_trading_unit=rng.choice([10, 100]),
                raer=raer(
                    rng.choice([RaerState.AVAILABLE] * 6 + [RaerState.UNAVAILABLE]),
                    value=str(rng.choice([1, 2, 3, 3.5, 4, 6, 7])),
                    confidence=rng.choice([*ConfidenceLevel, None]),
                    risk_flags=rng.choice(flag_sets),
                ),
                eligibility=rng.choice(
                    [cr.SourceEligibility.ELIGIBLE] * 4 + [cr.SourceEligibility.UNKNOWN]
                ),
                sector=rng.choice(sectors),
            )
        )
    ratios = [D("0.1"), D("0.25"), D("0.5"), D("1")]
    constraints = cons(
        max_single_stock_amount=D(rng.choice([150_000, 300_000, 600_000, 3_000_000])),
        max_single_stock_ratio=rng.choice(ratios),
        max_sector_ratio=rng.choice(ratios),
        max_high_risk_ratio=rng.choice(ratios),
    )
    ratio_in_force = any(
        r is not None and r < 1
        for r in (
            constraints.max_single_stock_ratio,
            constraints.max_sector_ratio,
            constraints.max_high_risk_ratio,
        )
    )
    held = sum((s.current_price * s.shares_held for s in sources), D(0))
    portfolio = held + D(rng.choice([300_000, 2_000_000, 10_000_000])) if ratio_in_force else None
    sector_exposure = {
        name: sum((s.current_price * s.shares_held for s in sources if s.sector == name), D(0))
        + D(rng.choice([0, 100_000]))
        for name in sectors
        if name
    }
    hr_exposure = sum(
        (
            s.current_price * s.shares_held
            for s in sources
            if s.raer.risk_flags_available and s.raer.risk_flags & HIGH_RISK
        ),
        D(0),
    ) + D(rng.choice([0, 100_000]))
    return Scenario(
        allocation_cash=D(rng.choice([0, 30_000, 90_000, 250_000])),
        portfolio=portfolio,
        candidates=tuple(candidates),
        sources=tuple(sources),
        constraints=constraints,
        allocation_policy=apol(
            tier_quantity_caps={t: rng.choice([D("0.5"), D("0.8"), D("1")]) for t in pt.PriceTier}
        ),
        policy=rpol(
            min_raer_improvement=D(rng.choice(["0.5", "2", "3"])),
            proceeds_inclusion_ratio=D(rng.choice(["0.5", "0.8", "0.95"])),
            min_confidence=rng.choice([ConfidenceLevel.LOW, ConfidenceLevel.MEDIUM]),
        ),
        sector_exposure=sector_exposure,
        high_risk_exposure=hr_exposure,
    )


def random_binding_scenario(rng: random.Random) -> Scenario:
    """比率の上限が必ず効き、候補と入替元が業種・high-risk を共有する(売却後の再判定が効く入力)."""
    sectors = ["電気機器", "食料品"]
    flag_sets = [frozenset(), HIGH_RISK]
    candidates = []
    for i in range(rng.randint(2, 4)):
        position = rng.choice([0, 0, 100_000])
        candidates.append(
            cand(
                f"{1000 + i}",
                kind=pt.CandidateKind.ADDITIONAL_BUY if position else pt.CandidateKind.NEW_BUY,
                current_price=D(rng.choice([700, 1000, 2500])),
                raer=raer(value=str(rng.choice([8, 9, 10])), risk_flags=rng.choice(flag_sets)),
                current_position_value=D(position),
                sector=rng.choice(sectors),
            )
        )
    sources = [
        src(
            f"{5000 + j}",
            shares_held=rng.choice([200, 500, 1000]),
            current_price=D(rng.choice([500, 1000, 2000])),
            raer=raer(value=str(rng.choice([2, 3, 4, 5])), risk_flags=rng.choice(flag_sets)),
            sector=rng.choice(sectors),
        )
        for j in range(rng.randint(2, 4))
    ]

    def value(item: object) -> Decimal:
        if isinstance(item, cr.RotationSource):
            return item.current_price * item.shares_held
        assert isinstance(item, pt.AllocationCandidate)
        return item.current_position_value

    holdings = [*sources, *candidates]
    constraints = cons(
        max_single_stock_amount=D(rng.choice([300_000, 600_000, 3_000_000])),
        max_single_stock_ratio=rng.choice([D("0.1"), D("0.3"), D("1")]),
        max_sector_ratio=rng.choice([D("0.2"), D("0.3"), D("0.5")]),
        max_high_risk_ratio=rng.choice([D("0.2"), D("0.3"), D("0.5")]),
    )
    return Scenario(
        allocation_cash=D(rng.choice([0, 40_000, 90_000])),
        portfolio=sum((value(h) for h in holdings), D(0))
        + D(rng.choice([300_000, 1_000_000, 3_000_000])),
        candidates=tuple(candidates),
        sources=tuple(sources),
        constraints=constraints,
        allocation_policy=apol(),
        policy=rpol(
            min_raer_improvement=D(rng.choice(["2", "3"])),
            proceeds_inclusion_ratio=D(rng.choice(["0.5", "0.8"])),
        ),
        sector_exposure={
            name: sum((value(h) for h in holdings if h.sector == name), D(0))
            + D(rng.choice([0, 100_000]))
            for name in sectors
        },
        high_risk_exposure=sum((value(h) for h in holdings if h.raer.risk_flags & HIGH_RISK), D(0))
        + D(rng.choice([0, 100_000])),
    )


def _legs(result: cr.RotationResult) -> list[tuple[str, int, str, int]]:
    return [
        (p.sell_leg.stock_code, p.sell_leg.sell_shares, p.buy_leg.stock_code, p.buy_leg.shares)
        for p in result.proposals
    ]


def test_the_evaluation_agrees_with_the_one_unit_at_a_time_reference() -> None:
    rng = random.Random(604)
    proposed = 0
    for _ in range(1500):
        s = random_scenario(rng)
        allocation = build_allocation(s)
        if allocation.status is pt.AllocationStatus.UNAVAILABLE:
            continue
        got = evaluate(s)
        assert _legs(got) == reference(s), (s, got)
        proposed += bool(got.proposals)
    assert proposed > 150  # 提案が出た入力が十分な数ある(空振りしていない)


def test_the_evaluation_agrees_with_the_reference_when_the_caps_bind() -> None:
    rng = random.Random(609)
    proposed = multiple = 0
    for _ in range(500):
        s = random_binding_scenario(rng)
        got = evaluate(s)
        assert _legs(got) == reference(s), (s, got)
        check_properties(s, got)
        proposed += bool(got.proposals)
        multiple += len(got.proposals) >= 2
    assert proposed > 100 and multiple > 20  # 上限が効く提案・複数の提案が十分にある


def test_the_random_scenarios_never_make_the_types_reject_the_result() -> None:
    rng = random.Random(7)
    for _ in range(800):
        s = random_scenario(rng)
        if build_allocation(s).status is pt.AllocationStatus.UNAVAILABLE:
            continue
        evaluate(s)  # RotationContractError(型の拒否)を評価が作らない


def check_properties(s: Scenario, got: cr.RotationResult) -> None:
    allocation = build_allocation(s)
    assert got.starting_cash == allocation.remaining_cash
    bought = sum((p.buy_leg.amount for p in got.proposals), D(0))
    counted = sum((p.sell_leg.counted_proceeds for p in got.proposals), D(0))
    estimated = sum((p.sell_leg.estimated_proceeds for p in got.proposals), D(0))
    assert got.starting_cash is not None
    # R-3 購入総額 <= 残資金 + 算入額の合計 / R-4 算入額 <= 見込額 / 残りの計算
    assert bought <= got.starting_cash + counted
    assert counted <= estimated
    assert got.ending_cash == got.starting_cash + counted - bought
    by_src = {x.stock_code: x for x in s.sources}
    by_cand = {c.stock_code: c for c in s.candidates}
    for p in got.proposals:
        x, c = by_src[p.sell_leg.stock_code], by_cand[p.buy_leg.stock_code]
        # R-1 SELL と BUY の対 / R-5 改善幅 >= 最低改善幅 / R-6 どちらかの RAER が使えない組は出ない
        assert x.raer.value is not None and c.raer.value is not None
        assert p.improvement == c.raer.value - x.raer.value
        assert s.policy.min_raer_improvement is not None
        assert p.improvement >= s.policy.min_raer_improvement
        assert x.eligibility is cr.SourceEligibility.ELIGIBLE
        # 売却は入替元の単元の倍数で、保有を超えない / 購入は目標の単元の倍数
        assert p.sell_leg.sell_shares % x.minimum_trading_unit == 0
        assert p.sell_leg.sell_shares <= x.shares_held
        assert p.buy_leg.shares % s.constraints.minimum_trading_unit == 0
        assert p.buy_leg.amount == c.current_price * p.buy_leg.shares
        full = p.sell_leg.sell_shares == x.shares_held
        assert (p.sell_leg.quantity_kind is cr.SellQuantityKind.FULL) == full
        assert p.sell_leg.estimated_proceeds == x.current_price * p.sell_leg.sell_shares
    # 入替元は 1 提案に 1 回
    sells = [p.sell_leg.stock_code for p in got.proposals]
    assert len(set(sells)) == len(sells)


def test_the_basic_properties_hold_for_random_inputs() -> None:
    rng = random.Random(605)
    for _ in range(1200):
        s = random_scenario(rng)
        if build_allocation(s).status is pt.AllocationStatus.UNAVAILABLE:
            continue
        check_properties(s, evaluate(s))


def test_an_undecided_policy_never_proposes_for_random_inputs() -> None:
    rng = random.Random(606)
    for _ in range(500):
        s = random_scenario(rng)
        if build_allocation(s).status is pt.AllocationStatus.UNAVAILABLE:
            continue
        for item in cr.ROTATION_POLICY_ITEMS:
            undecided = dataclasses.replace(s.policy, **{item: None})
            got = evaluate(dataclasses.replace(s, policy=undecided))
            assert got.status is cr.RotationStatus.SHADOW_ONLY and got.proposals == ()


def test_the_cash_never_makes_a_below_threshold_pair_a_rotation_for_random_inputs() -> None:
    # R-2: 買付余力を変えても、改善幅が最低改善幅に満たない組は提案されない
    rng = random.Random(607)
    for _ in range(500):
        s = random_scenario(rng)
        for cash in (D("0"), D("30000"), D("250000"), D("5000000")):
            t = dataclasses.replace(s, allocation_cash=cash)
            if build_allocation(t).status is pt.AllocationStatus.UNAVAILABLE:
                continue
            got = evaluate(t)
            by_src = {x.stock_code: x for x in t.sources}
            by_cand = {c.stock_code: c for c in t.candidates}
            for p in got.proposals:
                x, c = by_src[p.sell_leg.stock_code], by_cand[p.buy_leg.stock_code]
                assert c.raer.value - x.raer.value >= t.policy.min_raer_improvement  # type: ignore[operator]


def test_the_result_is_deterministic_and_does_not_depend_on_the_input_order_for_random_inputs() -> (
    None
):
    rng = random.Random(608)
    for _ in range(200):
        s = random_scenario(rng)
        if build_allocation(s).status is pt.AllocationStatus.UNAVAILABLE:
            continue
        shuffled_sources = list(s.sources)
        shuffled_candidates = list(s.candidates)
        rng.shuffle(shuffled_sources)
        rng.shuffle(shuffled_candidates)
        again = dataclasses.replace(
            s, sources=tuple(shuffled_sources), candidates=tuple(shuffled_candidates)
        )
        assert evaluate(s) == evaluate(again) == evaluate(s)


# --- 13 純粋性・休眠・既存の Exit 判定への影響なし(AST)-------------------------------

_ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "decimal",
    "enum",
    "fractions",
    "jstock_advisor.domain.entities.enums",
    "jstock_advisor.domain.exit_architecture.vocabulary",
    "jstock_advisor.domain.signals.portfolio_allocation",
    "jstock_advisor.domain.signals.portfolio_allocation_types",
    "jstock_advisor.domain.valuation.raer",
    "typing",
}


def test_the_module_imports_only_domain_pieces_and_the_standard_library() -> None:
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


def test_the_module_defines_and_calls_nothing_that_looks_like_sending_or_saving() -> None:
    words = ("send", "push", "publish", "invoke", "deliver", "notify", "save", "write", "persist")
    for node in ast.walk(_tree()):
        names: list[str] = []
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.append(node.name)
        elif isinstance(node, ast.Call):
            func = node.func
            names.append(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
        for name in names:
            assert not any(w in name.lower() for w in words), name


_ALLOWED_IMPORTERS: set[str] = set()  # この module を import してよい src の module(現時点は無し)


def test_the_module_is_imported_from_nowhere_in_src() -> None:
    src_root = _REPO / "src" / "jstock_advisor"
    for path in src_root.rglob("*.py"):
        if path == _MODULE_PATH:
            continue
        text = path.read_text(encoding="utf-8")
        if "signals.capital_rotation" in text or "signals import capital_rotation" in text:
            assert path.relative_to(src_root).as_posix() in _ALLOWED_IMPORTERS, str(path)


def test_the_exit_decision_modules_do_not_know_the_rotation_module() -> None:
    # 既存の SELL / 利確 / 下落保護 / Arbiter の判定は、本 module を参照しない(影響がない)
    src_root = _REPO / "src" / "jstock_advisor"
    names = [
        "domain/signals/sell_signal.py",
        "domain/signals/profit_taking.py",
        "domain/signals/profit_protection.py",
    ]
    paths = [src_root / n for n in names] + sorted(
        (src_root / "domain/exit_architecture").glob("*.py")
    )
    assert len(paths) > 5
    for path in paths:
        assert "capital_rotation" not in path.read_text(encoding="utf-8"), str(path)


def test_the_exit_class_vocabulary_is_unchanged_and_the_rotation_class_exists() -> None:
    assert {c.value for c in ExitClass} == {
        "RISK_EXIT",
        "VALUE_EXIT",
        "PROFIT_PROTECTION",
        "CAPITAL_ROTATION",
        "NONE",
    }
    assert cr.SellLeg.__dataclass_fields__["exit_class"].type in ("ExitClass", ExitClass)


def test_the_module_does_not_change_the_allocation_it_reads() -> None:
    allocation = make_allocation()
    before = dataclasses.replace(allocation)
    run(allocation=allocation)
    assert allocation == before


def test_the_sequence_alias_is_accepted() -> None:
    seq: Sequence[cr.RotationSource] = (src(),)
    assert run(sources=seq).status is cr.RotationStatus.ROTATION_PROPOSED
