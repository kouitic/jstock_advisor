"""Issue #603 / PR-C: 配分の最適化 `optimize_allocation` の契約テスト(C603-v1.2)。

純粋関数だけで、時計・I/O・永続化・配線に触れない。どこからも import されない(dormant)。
閾値は決めない(数値の None は未決定 = POLICY_INCOMPLETE → 配分しない)。

## 何を固定するのか

```
・判定の優先順位: 入力検証 → 買付余力が未登録 → 保有の評価が揃わない → 比較可能性 → 方針の完全性 →
  候補ごと(RAER・hurdle)→ 順位 → 貪欲(位置・業種・high-risk・段階・資金)
・順位: value 降順 → confidence 降順(HIGH > MEDIUM > LOW > None)→ stock_code 昇順。入力の並びに依らない
・方針がすべて未決定(既定)なら、全候補が POLICY_INCOMPLETE の『順位だけの shadow』で、配分は 0
・実データでは当面すべて RAER_UNAVAILABLE(B1 の射影は常に UNAVAILABLE)
・独立な参照実装(1 単元ずつ足す)との一致、購入総額 <= 買付余力、各上限を超えない、単調性、最大性
```

## 独立な参照実装

期待値は optimizer の式から導かない。テスト側の参照実装は『順位順に、1 単元ずつ足して、
全ての不等式(資金・金額・比率・業種・high-risk)を満たす間だけ続ける』という定義そのものである。
"""

from __future__ import annotations

import ast
import datetime as dt
import itertools
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import BuyAction, ConfidenceLevel
from jstock_advisor.domain.price_freshness import PriceFreshnessVerdict
from jstock_advisor.domain.signals import portfolio_allocation as pa
from jstock_advisor.domain.signals import portfolio_allocation_types as pt
from jstock_advisor.domain.valuation.expected_return import (
    FairValueInput,
    IncomeInput,
    compute_expected_return,
)
from jstock_advisor.domain.valuation.raer import (
    MODEL_VERSION,
    AxisCode,
    AxisObservation,
    AxisStatus,
    RaerResult,
    RaerState,
    RiskFlag,
    evaluate_raer,
    make_comparison_key,
    to_raer_result,
)
from jstock_advisor.domain.valuation.yield_calc import BenefitProgramState

D = Decimal
_REPO = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO / "src" / "jstock_advisor" / "domain" / "signals" / "portfolio_allocation.py"
_ALL_AXES = frozenset(a.value for a in AxisCode)
_JST = dt.timezone(dt.timedelta(hours=9))
_AS_OF = dt.datetime(2026, 10, 12, 8, 40, tzinfo=_JST)
R = pt.ReasonCode
U = pt.UnavailableReason


# --- 部品(合成データ)-----------------------------------------------------------------------


def raer(
    state: RaerState = RaerState.AVAILABLE,
    value: str = "5",
    confidence: ConfidenceLevel | None = ConfidenceLevel.HIGH,
    risk_flags: frozenset[str] = frozenset(),
    risk_observed: bool = True,
) -> RaerResult:
    """型が許す合成データ。5 軸の分割と comparison_key の規則を満たす。"""
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
        if not risk_observed:
            raise AssertionError("an AVAILABLE raer observes the risk axis")
    else:
        not_applied = frozenset({AxisCode.BUSINESS_RISK.value}) if not risk_observed else frozenset(
            {AxisCode.EARNINGS_TREND.value}
        )
        applied = _ALL_AXES - not_applied
    return RaerResult(
        state=state,
        value=D(value),
        confidence=confidence,
        risk_flags=risk_flags if AxisCode.BUSINESS_RISK.value in applied else frozenset(),
        risk_flags_available=AxisCode.BUSINESS_RISK.value in applied,
        model_version=MODEL_VERSION,
        comparison_key=make_comparison_key(MODEL_VERSION, applied),
        axes_applied=applied,
        axes_not_applied=not_applied,
        reason_codes=() if state is RaerState.AVAILABLE else ("AXIS_NOT_IMPLEMENTED",),
    )


KEY = raer().comparison_key
PRICE = D("1000")
UNIT = 100
UNIT_COST = PRICE * UNIT  # 100,000


def cand(code: str = "0002", **over: object) -> pt.AllocationCandidate:
    base: dict[str, object] = {
        "stock_code": code,
        "kind": pt.CandidateKind.NEW_BUY,
        "buy_action": BuyAction.BUY,
        "current_price": PRICE,
        "price_as_of": _AS_OF,
        "price_tier": None,
        "raer": raer(),
        "current_position_value": D("0"),
        "sector": "電気機器",
    }
    base.update(over)
    return pt.AllocationCandidate(**base)  # type: ignore[arg-type]


def cons(**over: object) -> pt.AllocationConstraints:
    """既定は『すべて決定済みで、効かない上限』(金額は十分大きく、比率は 1)。"""
    base: dict[str, object] = {
        "max_single_stock_amount": D("100000000"),
        "max_single_stock_ratio": D("1"),
        "max_sector_ratio": D("1"),
        "max_high_risk_ratio": D("1"),
        "high_risk_flags": frozenset({RiskFlag.CYCLICAL_INDUSTRY.value}),
        "minimum_trading_unit": UNIT,
    }
    base.update(over)
    return pt.AllocationConstraints(**base)  # type: ignore[arg-type]


def pol(**over: object) -> pt.AllocationPolicy:
    base: dict[str, object] = {
        "min_raer_to_allocate": D("3"),
        "comparison_key": KEY,
        "tier_quantity_caps": {t: D("1") for t in pt.PriceTier},
    }
    base.update(over)
    return pt.AllocationPolicy(**base)  # type: ignore[arg-type]


def empty_policy() -> pt.AllocationPolicy:
    return pt.AllocationPolicy(min_raer_to_allocate=None, comparison_key=None, tier_quantity_caps=None)


def undecided_constraints() -> pt.AllocationConstraints:
    return pt.AllocationConstraints(
        max_single_stock_amount=None,
        max_single_stock_ratio=None,
        max_sector_ratio=None,
        max_high_risk_ratio=None,
        high_risk_flags=frozenset(),
        minimum_trading_unit=UNIT,
    )


def run(**over: object) -> pt.AllocationResult:
    base: dict[str, object] = {
        "owner": "owner-a",
        "available_cash": D("1000000"),
        "portfolio_value": D("10000000"),
        "candidates": [cand()],
        "constraints": cons(),
        "policy": pol(),
    }
    base.update(over)
    return pa.optimize_allocation(**base)  # type: ignore[arg-type]


def by_code(result: pt.AllocationResult) -> dict[str, pt.CandidateAllocation]:
    return {a.stock_code: a for a in result.allocations}


# --- 1 判定の優先順位 --------------------------------------------------------------------------


def test_an_unregistered_cash_is_unavailable_and_is_not_zero_yen() -> None:
    r = run(available_cash=None)
    assert r.status is pt.AllocationStatus.UNAVAILABLE
    assert r.unavailable_reason is U.CASH_NOT_REGISTERED
    assert r.allocations == () and r.remaining_cash is None and r.total_allocated == 0


def test_zero_cash_is_a_registered_state_and_leaves_every_candidate_short_of_cash() -> None:
    r = run(available_cash=D("0"))
    assert r.status is pt.AllocationStatus.NO_ACTION
    assert r.remaining_cash == D("0")
    only = by_code(r)["0002"]
    assert only.reason_code is R.INSUFFICIENT_CASH_REMAINING
    assert only.cap_limited_shares > 0 and only.cash_shortfall == only.cap_limited_shares * PRICE


def test_the_unregistered_cash_wins_over_every_other_condition() -> None:
    r = run(
        available_cash=None,
        portfolio_value=None,
        constraints=cons(max_single_stock_ratio=D("0.1")),
        policy=empty_policy(),
        candidates=[cand("0002"), cand("0003", raer=raer(RaerState.PROVISIONAL))],
    )
    assert r.unavailable_reason is U.CASH_NOT_REGISTERED


def test_a_ratio_cap_in_force_without_a_portfolio_value_is_unavailable() -> None:
    r = run(portfolio_value=None, constraints=cons(max_single_stock_ratio=D("0.1")))
    assert r.unavailable_reason is U.PORTFOLIO_VALUE_UNAVAILABLE
    assert r.allocations == () and r.remaining_cash is None


def test_no_ratio_cap_in_force_needs_no_portfolio_value() -> None:
    r = run(portfolio_value=None)  # 既定の比率はすべて 1(効かない)
    assert r.status is pt.AllocationStatus.ALLOCATED_SOME


def test_an_undecided_ratio_is_not_in_force_so_the_policy_is_what_is_incomplete() -> None:
    r = run(portfolio_value=None, constraints=cons(max_single_stock_ratio=None))
    assert r.unavailable_reason is U.POLICY_INCOMPLETE


def test_a_sector_cap_needs_the_sector_exposure_and_a_high_risk_cap_needs_its_exposure() -> None:
    sector = run(constraints=cons(max_sector_ratio=D("0.5")), sector_exposure=None)
    assert sector.unavailable_reason is U.PORTFOLIO_VALUE_UNAVAILABLE
    hr = run(constraints=cons(max_high_risk_ratio=D("0.5")), high_risk_exposure=None)
    assert hr.unavailable_reason is U.PORTFOLIO_VALUE_UNAVAILABLE
    ok = run(
        constraints=cons(max_sector_ratio=D("0.5"), max_high_risk_ratio=D("0.5")),
        sector_exposure={},
        high_risk_exposure=D("0"),
    )
    assert ok.status is pt.AllocationStatus.ALLOCATED_SOME


def test_candidates_with_two_comparison_keys_are_not_comparable() -> None:
    r = run(candidates=[cand("0002"), cand("0003", raer=raer(RaerState.PROVISIONAL))])
    assert r.unavailable_reason is U.RAER_NOT_COMPARABLE
    assert r.allocations == () and r.remaining_cash is None


def test_a_policy_key_that_differs_from_the_candidates_key_is_not_comparable() -> None:
    r = run(policy=pol(comparison_key="another-model|AXIS"))
    assert r.unavailable_reason is U.RAER_NOT_COMPARABLE and r.allocations == ()


def test_an_unavailable_raer_does_not_break_the_comparability_of_the_others() -> None:
    r = run(candidates=[cand("0002"), cand("0003", raer=raer(RaerState.UNAVAILABLE))])
    assert r.status is pt.AllocationStatus.ALLOCATED_SOME
    assert by_code(r)["0003"].reason_code is R.RAER_UNAVAILABLE


def test_a_provisional_key_matches_a_policy_decided_for_that_key() -> None:
    provisional = raer(RaerState.PROVISIONAL)
    r = run(candidates=[cand(raer=provisional)], policy=pol(comparison_key=provisional.comparison_key))
    assert r.status is pt.AllocationStatus.ALLOCATED_SOME


def test_the_precedence_of_the_unavailable_reasons() -> None:
    mixed = [cand("0002"), cand("0003", raer=raer(RaerState.PROVISIONAL))]
    ratio = cons(max_single_stock_ratio=D("0.1"))
    # 比率の上限が効くのに portfolio_value なし > 比較不能 > 方針不完全
    assert run(portfolio_value=None, constraints=ratio, candidates=mixed, policy=empty_policy()).unavailable_reason is U.PORTFOLIO_VALUE_UNAVAILABLE
    assert run(candidates=mixed, policy=empty_policy()).unavailable_reason is U.RAER_NOT_COMPARABLE
    assert run(policy=empty_policy()).unavailable_reason is U.POLICY_INCOMPLETE


def test_every_unavailable_result_names_what_the_policy_is_missing() -> None:
    p = pol(min_raer_to_allocate=None)
    for r in (run(available_cash=None, policy=p), run(candidates=[cand(), cand("0003", raer=raer(RaerState.PROVISIONAL))], policy=p)):
        assert r.policy_missing == ("min_raer_to_allocate",)


def test_an_empty_candidate_list_is_a_no_action_that_keeps_the_cash() -> None:
    r = run(candidates=[])
    assert r.status is pt.AllocationStatus.NO_ACTION
    assert r.allocations == () and r.remaining_cash == D("1000000")


# --- 2 方針がすべて未決定 = POLICY_INCOMPLETE の shadow(USER の決定)---------------------------


def test_an_all_undecided_policy_yields_a_ranking_only_shadow() -> None:
    cands = [
        cand("0003", raer=raer(value="9")),
        cand("0002", raer=raer(value="5")),
        cand("0004", raer=raer(RaerState.UNAVAILABLE)),
    ]
    r = run(candidates=cands, constraints=undecided_constraints(), policy=empty_policy())
    assert r.status is pt.AllocationStatus.UNAVAILABLE
    assert r.unavailable_reason is U.POLICY_INCOMPLETE
    assert r.policy_missing == empty_policy().missing(undecided_constraints())
    assert r.total_allocated == 0 and r.remaining_cash is None
    shadow = by_code(r)
    assert shadow["0003"].rank == 1 and shadow["0002"].rank == 2 and shadow["0004"].rank is None
    assert shadow["0003"].reason_code is R.POLICY_INCOMPLETE
    assert shadow["0004"].reason_code is R.RAER_UNAVAILABLE
    assert all(a.shares == 0 and a.cap_limited_shares == 0 for a in r.allocations)
    assert all(a.decision is pt.CandidateDecision.NOT_ALLOCATED for a in r.allocations)


def test_the_shadow_ranks_a_candidate_even_below_an_undecided_hurdle() -> None:
    r = run(candidates=[cand(raer=raer(value="-4"))], policy=pol(min_raer_to_allocate=None))
    assert by_code(r)["0002"].reason_code is R.POLICY_INCOMPLETE and by_code(r)["0002"].rank == 1


@pytest.mark.parametrize(
    ("policy_over", "constraint_over"),
    [
        ({"min_raer_to_allocate": None}, {}),
        ({"comparison_key": None}, {}),
        ({"tier_quantity_caps": None}, {}),
        ({}, {"max_single_stock_amount": None}),
        ({}, {"max_single_stock_ratio": None}),
        ({}, {"max_sector_ratio": None}),
        ({}, {"max_high_risk_ratio": None}),
        ({}, {"max_high_risk_ratio": D("0.5"), "high_risk_flags": frozenset()}),
    ],
)
def test_one_undecided_item_alone_stops_every_allocation(
    policy_over: dict[str, object], constraint_over: dict[str, object]
) -> None:
    r = run(policy=pol(**policy_over), constraints=cons(**constraint_over))
    assert r.unavailable_reason is U.POLICY_INCOMPLETE
    assert r.total_allocated == 0
    assert all(a.shares == 0 for a in r.allocations)  # 『とにかく買う』の禁止


def test_deciding_the_items_one_by_one_allocates_only_when_none_is_missing() -> None:
    steps = [
        (empty_policy(), undecided_constraints()),
        (pol(), undecided_constraints()),
    ]
    for policy, constraints in steps:
        assert run(policy=policy, constraints=constraints).unavailable_reason is U.POLICY_INCOMPLETE
    assert run().status is pt.AllocationStatus.ALLOCATED_SOME


# --- 3 候補ごとの判定(RAER・hurdle)と順位 --------------------------------------------------------


def test_an_unavailable_raer_is_excluded_with_its_reason_and_is_not_ranked() -> None:
    r = run(candidates=[cand("0002"), cand("0003", raer=raer(RaerState.UNAVAILABLE))])
    out = by_code(r)["0003"]
    assert out.reason_code is R.RAER_UNAVAILABLE and out.rank is None and out.shares == 0


def test_below_the_hurdle_is_excluded_and_equal_to_the_hurdle_is_kept() -> None:
    r = run(candidates=[cand("0002", raer=raer(value="2.99")), cand("0003", raer=raer(value="3"))])
    assert by_code(r)["0002"].reason_code is R.BELOW_HURDLE and by_code(r)["0002"].rank is None
    assert by_code(r)["0003"].decision is pt.CandidateDecision.ALLOCATED


def test_the_ranking_is_by_value_then_confidence_then_code() -> None:
    cands = [
        cand("0005", raer=raer(value="8", confidence=ConfidenceLevel.LOW)),
        cand("0004", raer=raer(value="8", confidence=ConfidenceLevel.HIGH)),
        cand("0003", raer=raer(value="9", confidence=ConfidenceLevel.LOW)),
        cand("0002", raer=raer(value="8", confidence=ConfidenceLevel.HIGH)),
    ]
    r = run(candidates=cands, available_cash=D("0"))
    assert [a.stock_code for a in r.allocations] == ["0003", "0002", "0004", "0005"]
    assert [a.rank for a in r.allocations] == [1, 2, 3, 4]


def test_the_confidence_table_is_high_medium_low_none() -> None:
    assert pa.confidence_rank(ConfidenceLevel.HIGH) > pa.confidence_rank(ConfidenceLevel.MEDIUM)
    assert pa.confidence_rank(ConfidenceLevel.MEDIUM) > pa.confidence_rank(ConfidenceLevel.LOW)
    assert pa.confidence_rank(ConfidenceLevel.LOW) > pa.confidence_rank(None)
    assert {pa.confidence_rank(c) for c in (*ConfidenceLevel, None)} == {
        pa.confidence_rank(c) for c in ConfidenceLevel
    } | {pa.confidence_rank(None)}
    assert len({pa.confidence_rank(c) for c in (*ConfidenceLevel, None)}) == 4  # 全て別の順位


@pytest.mark.parametrize(
    ("better", "worse"),
    [
        (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM),
        (ConfidenceLevel.HIGH, ConfidenceLevel.LOW),
        (ConfidenceLevel.HIGH, None),
        (ConfidenceLevel.MEDIUM, ConfidenceLevel.LOW),
        (ConfidenceLevel.MEDIUM, None),
        (ConfidenceLevel.LOW, None),
    ],
)
def test_equal_values_are_broken_by_confidence_even_against_the_code_order(
    better: ConfidenceLevel | None, worse: ConfidenceLevel | None
) -> None:
    # 順位が付くのは confidence の高い方(stock_code は逆順にして、コードの順で決まらないことを見る)
    cands = [
        cand("0002", raer=raer(value="7", confidence=worse)),
        cand("0009", raer=raer(value="7", confidence=better)),
    ]
    r = run(candidates=cands, available_cash=D("0"))
    assert [a.stock_code for a in r.allocations] == ["0009", "0002"]


def test_value_wins_over_confidence_and_code() -> None:
    cands = [
        cand("0002", raer=raer(value="7", confidence=ConfidenceLevel.HIGH)),
        cand("0009", raer=raer(value="7.5", confidence=ConfidenceLevel.LOW)),
    ]
    assert [a.stock_code for a in run(candidates=cands, available_cash=D("0")).allocations] == [
        "0009",
        "0002",
    ]


def test_the_result_does_not_depend_on_the_input_order_for_every_permutation() -> None:
    cands = [
        cand("0002", raer=raer(value="8")),
        cand("0003", raer=raer(value="8", confidence=ConfidenceLevel.LOW)),
        cand("0004", raer=raer(value="6")),
        cand("0005", raer=raer(RaerState.UNAVAILABLE)),
        cand("0006", raer=raer(value="2")),
    ]
    results = {
        run(candidates=list(p), available_cash=D("750000"), constraints=cons(max_single_stock_amount=D("400000")))
        for p in itertools.permutations(cands)
    }
    assert len(results) == 1


# --- 4 貪欲: 資金・位置の上限(シナリオ。数値は手計算)------------------------------------------


def test_a_single_candidate_takes_the_whole_floor_of_the_cash_in_units() -> None:
    r = run(available_cash=D("1050000"))
    a = by_code(r)["0002"]
    assert (a.shares, a.amount, a.reason_code) == (1000, D("1000000"), R.ALLOCATED)
    assert r.total_allocated == D("1000000") and r.remaining_cash == D("50000")
    assert r.status is pt.AllocationStatus.ALLOCATED_SOME


def test_the_cash_is_spent_in_rank_order_and_the_second_candidate_gets_the_rest() -> None:
    cands = [cand("0002", raer=raer(value="10")), cand("0003", raer=raer(value="5"))]
    r = run(
        candidates=cands,
        available_cash=D("1050000"),
        constraints=cons(max_single_stock_amount=D("600000")),
    )
    first, second = by_code(r)["0002"], by_code(r)["0003"]
    assert (first.shares, first.rank) == (600, 1)
    assert (second.shares, second.rank) == (400, 2)  # 残り 450,000 のうち 4 単元
    assert r.total_allocated == D("1000000") and r.remaining_cash == D("50000")
    assert second.cap_limited_shares == 600  # 資金を無視した上限は 600 株


def test_a_candidate_that_cannot_buy_one_unit_for_lack_of_cash_reports_the_unmet_demand() -> None:
    r = run(
        available_cash=D("50000"),
        constraints=cons(max_single_stock_amount=D("600000")),
    )
    a = by_code(r)["0002"]
    assert a.reason_code is R.INSUFFICIENT_CASH_REMAINING
    assert a.cap_limited_shares == 600
    assert a.cash_shortfall == D("550000")  # 600 株 × 1,000 − 50,000
    assert r.status is pt.AllocationStatus.NO_ACTION and r.remaining_cash == D("50000")


def test_the_cash_exactly_one_unit_buys_one_unit_and_one_yen_less_buys_none() -> None:
    assert by_code(run(available_cash=UNIT_COST))["0002"].shares == 100
    assert by_code(run(available_cash=UNIT_COST - 1))["0002"].reason_code is R.INSUFFICIENT_CASH_REMAINING


def test_the_amount_cap_includes_the_current_position() -> None:
    c = cand(kind=pt.CandidateKind.ADDITIONAL_BUY, current_position_value=D("300000"))
    r = run(candidates=[c], constraints=cons(max_single_stock_amount=D("600000")))
    assert by_code(r)["0002"].shares == 300  # (600,000 − 300,000) / 1,000


def test_an_amount_cap_with_no_room_left_is_the_amount_cap_reason() -> None:
    c = cand(kind=pt.CandidateKind.ADDITIONAL_BUY, current_position_value=D("580000"))
    r = run(candidates=[c], constraints=cons(max_single_stock_amount=D("600000")))
    a = by_code(r)["0002"]
    assert a.reason_code is R.POSITION_AMOUNT_CAP_APPLIED and a.shares == 0 and a.cap_limited_shares == 0


def test_an_amount_cap_below_one_unit_cost_is_below_the_minimum_trading_unit() -> None:
    r = run(constraints=cons(max_single_stock_amount=D("50000")))
    a = by_code(r)["0002"]
    assert a.reason_code is R.BELOW_MINIMUM_TRADING_UNIT and a.cap_limited_shares == 0


def test_the_single_stock_ratio_uses_the_post_purchase_denominator() -> None:
    # (0 + a) / (10,000,000 + a) <= 0.1  ->  a <= 1,111,111  ->  11 単元
    r = run(
        available_cash=D("5000000"),
        constraints=cons(max_single_stock_ratio=D("0.1")),
    )
    assert by_code(r)["0002"].shares == 1100


def test_a_position_already_over_the_ratio_cap_is_the_ratio_reason() -> None:
    c = cand(kind=pt.CandidateKind.ADDITIONAL_BUY, current_position_value=D("1200000"))
    r = run(candidates=[c], available_cash=D("5000000"), constraints=cons(max_single_stock_ratio=D("0.1")))
    a = by_code(r)["0002"]
    assert a.reason_code is R.POSITION_RATIO_CAP_APPLIED and a.shares == 0


def test_the_tighter_of_the_amount_and_ratio_caps_decides() -> None:
    r = run(
        available_cash=D("5000000"),
        constraints=cons(max_single_stock_amount=D("500000"), max_single_stock_ratio=D("0.1")),
    )
    assert by_code(r)["0002"].shares == 500


# --- 5 業種・high-risk・段階 ---------------------------------------------------------------


def test_a_sector_cap_limits_the_quantity_by_the_exposure_already_held() -> None:
    # 0.2 × 10,000,000 = 2,000,000。業種の保有 1,500,000 → (2,000,000 − 1,500,000) / 0.8 = 625,000 → 6 単元
    r = run(
        available_cash=D("5000000"),
        constraints=cons(max_sector_ratio=D("0.2")),
        sector_exposure={"電気機器": D("1500000")},
    )
    assert by_code(r)["0002"].shares == 600


def test_allocations_in_the_same_sector_accumulate_in_rank_order() -> None:
    cands = [cand("0002", raer=raer(value="9")), cand("0003", raer=raer(value="8"))]
    r = run(
        candidates=cands,
        available_cash=D("5000000"),
        constraints=cons(max_sector_ratio=D("0.2")),
        sector_exposure={"電気機器": D("1500000")},
    )
    first, second = by_code(r)["0002"], by_code(r)["0003"]
    assert first.shares == 600
    # 1 件目の後: 業種 2,100,000・全体 10,600,000 → (2,120,000 − 2,100,000) / 0.8 = 25,000 < 1 単元
    assert second.reason_code is R.SECTOR_RATIO_CAP_APPLIED and second.shares == 0


def test_a_sector_cap_does_not_apply_to_another_sector() -> None:
    cands = [cand("0002", sector="電気機器"), cand("0003", sector="食料品", raer=raer(value="4"))]
    r = run(
        candidates=cands,
        available_cash=D("5000000"),
        constraints=cons(max_sector_ratio=D("0.2")),
        sector_exposure={"電気機器": D("1500000"), "食料品": D("0")},
    )
    assert by_code(r)["0003"].shares > 0


def test_an_unknown_sector_is_a_reason_only_when_a_sector_cap_is_in_force() -> None:
    unknown = cand(sector=None)
    capped = run(candidates=[unknown], constraints=cons(max_sector_ratio=D("0.2")), sector_exposure={})
    assert by_code(capped)["0002"].reason_code is R.SECTOR_UNKNOWN
    assert by_code(capped)["0002"].rank == 1 and by_code(capped)["0002"].shares == 0
    free = run(candidates=[unknown])
    assert by_code(free)["0002"].decision is pt.CandidateDecision.ALLOCATED


def _high_risk(code: str = "0002", value: str = "5") -> pt.AllocationCandidate:
    return cand(code, raer=raer(value=value, risk_flags=frozenset({RiskFlag.CYCLICAL_INDUSTRY.value})))


def test_a_high_risk_cap_limits_only_high_risk_candidates() -> None:
    r = run(
        candidates=[_high_risk("0002", "9"), cand("0003", raer=raer(value="8"))],
        available_cash=D("5000000"),
        constraints=cons(max_high_risk_ratio=D("0.1")),
        high_risk_exposure=D("0"),
    )
    # (0 + a) / (10,000,000 + a) <= 0.1  ->  1,100,000 まで
    assert by_code(r)["0002"].shares == 1100
    assert by_code(r)["0003"].shares > 1100  # high-risk でない候補は上限に掛からない


def test_high_risk_allocations_accumulate() -> None:
    r = run(
        candidates=[_high_risk("0002", "9"), _high_risk("0003", "8")],
        available_cash=D("5000000"),
        constraints=cons(max_high_risk_ratio=D("0.1")),
        high_risk_exposure=D("0"),
    )
    second = by_code(r)["0003"]
    # 1 件目 1,100,000 の後: (0.1 × 11,100,000 − 1,100,000) / 0.9 = 11,111 < 1 単元
    assert second.reason_code is R.HIGH_RISK_ALLOCATION_CAP_APPLIED and second.shares == 0


def test_an_unobserved_risk_is_unknown_not_no_risk() -> None:
    unobserved = cand(raer=raer(RaerState.PROVISIONAL, risk_observed=False))
    assert unobserved.raer.risk_flags_available is False
    policy = pol(comparison_key=unobserved.raer.comparison_key)
    capped = run(
        candidates=[unobserved],
        policy=policy,
        constraints=cons(max_high_risk_ratio=D("0.1")),
        high_risk_exposure=D("0"),
    )
    assert by_code(capped)["0002"].reason_code is R.HIGH_RISK_UNKNOWN
    free = run(candidates=[unobserved], policy=policy)  # 上限 1(= 制約なし)なら影響しない
    assert by_code(free)["0002"].decision is pt.CandidateDecision.ALLOCATED


def test_the_tier_ratio_scales_the_quantity_allowed_by_the_other_caps() -> None:
    tiers = {pt.PriceTier.ENTRY: D("0.5"), pt.PriceTier.STANDARD: D("0.75"), pt.PriceTier.STRONG: D("1")}
    for tier, expected in ((pt.PriceTier.ENTRY, 300), (pt.PriceTier.STANDARD, 400), (pt.PriceTier.STRONG, 600)):
        r = run(
            candidates=[cand(price_tier=tier)],
            available_cash=D("5000000"),
            constraints=cons(max_single_stock_amount=D("600000")),
            policy=pol(tier_quantity_caps=tiers),
        )
        assert by_code(r)["0002"].shares == expected, tier
        assert by_code(r)["0002"].price_tier_used is tier


def test_no_tier_means_no_tier_restriction() -> None:
    tiers = {t: D("0.1") for t in pt.PriceTier}
    r = run(
        candidates=[cand(price_tier=None)],
        available_cash=D("5000000"),
        constraints=cons(max_single_stock_amount=D("600000")),
        policy=pol(tier_quantity_caps=tiers),
    )
    a = by_code(r)["0002"]
    assert a.shares == 600 and a.price_tier_used is None


def test_a_tier_ratio_that_floors_to_zero_units_is_the_tier_reason() -> None:
    tiers = {t: D("0.1") for t in pt.PriceTier}  # 600 株 × 0.1 = 60 株 < 1 単元
    r = run(
        candidates=[cand(price_tier=pt.PriceTier.ENTRY)],
        available_cash=D("5000000"),
        constraints=cons(max_single_stock_amount=D("600000")),
        policy=pol(tier_quantity_caps=tiers),
    )
    a = by_code(r)["0002"]
    assert a.reason_code is R.TIER_CAP_APPLIED and a.shares == 0


def test_when_several_caps_leave_zero_the_first_in_the_fixed_order_gives_the_reason() -> None:
    # 位置(金額)・業種・high-risk・段階のすべてが 0 にする入力 → 位置が先
    c = _high_risk()
    tiers = {t: D("0.1") for t in pt.PriceTier}
    r = run(
        candidates=[cand(raer=c.raer, price_tier=pt.PriceTier.ENTRY, kind=pt.CandidateKind.ADDITIONAL_BUY, current_position_value=D("600000"))],
        available_cash=D("5000000"),
        constraints=cons(
            max_single_stock_amount=D("600000"),
            max_sector_ratio=D("0.01"),
            max_high_risk_ratio=D("0.01"),
        ),
        sector_exposure={"電気機器": D("5000000")},
        high_risk_exposure=D("5000000"),
        policy=pol(tier_quantity_caps=tiers),
    )
    assert by_code(r)["0002"].reason_code is R.POSITION_AMOUNT_CAP_APPLIED


def test_the_sector_reason_comes_before_the_high_risk_reason_and_the_tier_reason() -> None:
    c = _high_risk()
    tiers = {t: D("0.1") for t in pt.PriceTier}
    common: dict[str, object] = {
        "candidates": [cand(raer=c.raer, price_tier=pt.PriceTier.ENTRY)],
        "available_cash": D("5000000"),
        "high_risk_exposure": D("5000000"),
        "policy": pol(tier_quantity_caps=tiers),
    }
    both = run(
        **common,
        constraints=cons(max_sector_ratio=D("0.01"), max_high_risk_ratio=D("0.01")),
        sector_exposure={"電気機器": D("5000000")},
    )
    assert by_code(both)["0002"].reason_code is R.SECTOR_RATIO_CAP_APPLIED
    hr_and_tier = run(
        **common,
        constraints=cons(max_high_risk_ratio=D("0.01")),
        sector_exposure={},
    )
    assert by_code(hr_and_tier)["0002"].reason_code is R.HIGH_RISK_ALLOCATION_CAP_APPLIED


def test_an_unknown_sector_comes_before_an_unobserved_risk() -> None:
    unobserved = cand(sector=None, raer=raer(RaerState.PROVISIONAL, risk_observed=False))
    r = run(
        candidates=[unobserved],
        policy=pol(comparison_key=unobserved.raer.comparison_key),
        constraints=cons(max_sector_ratio=D("0.5"), max_high_risk_ratio=D("0.5")),
        sector_exposure={},
        high_risk_exposure=D("0"),
    )
    assert by_code(r)["0002"].reason_code is R.SECTOR_UNKNOWN


# --- 6 実データ(B1 の射影は常に UNAVAILABLE)---------------------------------------------------


def _real_unavailable_raer() -> RaerResult:
    expected = compute_expected_return(
        evaluation_date=date(2026, 10, 12),
        current_price=D("100"),
        price_freshness=PriceFreshnessVerdict.NORMAL,
        fair_value=FairValueInput(neutral=D("120"), usable_for_trading_judgment=True),
        income=IncomeInput(2.0, None, BenefitProgramState.NO_PROGRAM),
    )
    observations = {a: AxisObservation(AxisStatus.APPLIED) for a in AxisCode}
    evaluation = evaluate_raer(
        evaluation_date=date(2026, 10, 12),
        expected_return=expected,
        axes=observations,
        risk_flags=frozenset(),
    )
    return to_raer_result(evaluation)


def test_the_real_raer_projection_is_always_unavailable() -> None:
    assert _real_unavailable_raer().state is RaerState.UNAVAILABLE


def test_with_the_real_projection_every_candidate_is_excluded_and_the_day_is_no_action() -> None:
    real = _real_unavailable_raer()
    r = run(candidates=[cand("0002", raer=real), cand("0003", raer=real)])
    assert r.status is pt.AllocationStatus.NO_ACTION
    assert all(a.reason_code is R.RAER_UNAVAILABLE and a.rank is None for a in r.allocations)
    assert r.total_allocated == 0 and r.remaining_cash == D("1000000")


def test_with_the_real_projection_and_an_undecided_policy_the_day_is_a_shadow() -> None:
    real = _real_unavailable_raer()
    r = run(candidates=[cand(raer=real)], policy=empty_policy(), constraints=undecided_constraints())
    assert r.unavailable_reason is U.POLICY_INCOMPLETE
    assert by_code(r)["0002"].reason_code is R.RAER_UNAVAILABLE


# --- 7 入力の拒否(例外のメッセージに値を含めない)-----------------------------------------------

_DISTINCT = D("424242.5")


def _bad_inputs() -> list[tuple[str, dict[str, object]]]:
    return [
        ("owner empty", {"owner": ""}),
        ("owner not a str", {"owner": 424242}),
        ("cash float", {"available_cash": 424242.5}),
        ("cash int", {"available_cash": 424242}),
        ("cash bool", {"available_cash": True}),
        ("cash negative", {"available_cash": -_DISTINCT}),
        ("cash NaN", {"available_cash": D("NaN")}),
        ("portfolio float", {"portfolio_value": 424242.5}),
        ("portfolio negative", {"portfolio_value": -_DISTINCT}),
        ("candidates not a sequence", {"candidates": "0002"}),
        ("candidates not candidates", {"candidates": [424242]}),
        ("duplicate stock code", {"candidates": [cand("0002"), cand("0002")]}),
        ("constraints wrong type", {"constraints": "x"}),
        ("policy wrong type", {"policy": "x"}),
        ("sector exposure not a mapping", {"sector_exposure": [1]}),
        ("sector exposure negative", {"sector_exposure": {"電気機器": -_DISTINCT}}),
        ("sector exposure float", {"sector_exposure": {"電気機器": 424242.5}}),
        ("sector exposure key not a str", {"sector_exposure": {424242: D("1")}}),
        ("high risk exposure float", {"high_risk_exposure": 424242.5}),
        ("high risk exposure negative", {"high_risk_exposure": -_DISTINCT}),
    ]


@pytest.mark.parametrize(("label", "over"), _bad_inputs(), ids=[b[0] for b in _bad_inputs()])
def test_a_malformed_input_is_rejected_without_echoing_a_value(
    label: str, over: dict[str, object]
) -> None:
    with pytest.raises(pt.AllocationContractError) as info:
        run(**over)
    text = str(info.value)
    assert text
    for needle in ("424242", "0002"):
        assert needle not in text


def test_a_candidate_list_may_be_a_tuple_and_is_not_mutated() -> None:
    cands = (cand("0003"), cand("0002"))
    before = list(cands)
    run(candidates=cands)
    assert list(cands) == before


def _tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def test_the_error_is_built_in_exactly_one_place_from_names_only() -> None:
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


# --- 8 参照実装(1 単元ずつ)との一致と性質テスト --------------------------------------------------


@dataclass(frozen=True)
class Case:
    cash: Decimal
    portfolio: Decimal | None
    candidates: tuple[pt.AllocationCandidate, ...]
    constraints: pt.AllocationConstraints
    policy: pt.AllocationPolicy
    sector_exposure: Mapping[str, Decimal]
    high_risk_exposure: Decimal


def reference_shares(case: Case) -> dict[str, int]:
    """定義そのもの: 順位順に、1 単元ずつ足して、すべての不等式を満たす間だけ続ける。"""
    k, p = case.constraints, case.policy
    unit = k.minimum_trading_unit
    usable = [
        c
        for c in case.candidates
        if c.raer.state is not RaerState.UNAVAILABLE
        and p.min_raer_to_allocate is not None
        and c.raer.value is not None
        and c.raer.value >= p.min_raer_to_allocate
    ]
    rank = {"HIGH": 2, "MEDIUM": 1, "LOW": 0, None: -1}
    usable.sort(
        key=lambda c: (
            -c.raer.value,  # type: ignore[operator]
            -rank[c.raer.confidence.value if c.raer.confidence else None],
            c.stock_code,
        )
    )
    remaining = Fraction(case.cash)
    allocated_total = Fraction(0)
    sector_alloc: dict[str, Fraction] = {}
    hr_alloc = Fraction(0)
    out: dict[str, int] = {}
    for c in usable:
        price = Fraction(c.current_price)
        total = (Fraction(case.portfolio) if case.portfolio is not None else Fraction(0)) + allocated_total
        position = Fraction(c.current_position_value)
        sector_unknown = k.max_sector_ratio is not None and k.max_sector_ratio < 1 and c.sector is None
        hr_unknown = (
            k.max_high_risk_ratio is not None
            and k.max_high_risk_ratio < 1
            and not c.raer.risk_flags_available
        )
        if sector_unknown or hr_unknown:
            out[c.stock_code] = 0
            continue
        is_hr = bool(k.max_high_risk_ratio is not None and k.max_high_risk_ratio < 1 and (c.raer.risk_flags & k.high_risk_flags))

        def caps_ok(n: int, c: pt.AllocationCandidate = c, price: Fraction = price, total: Fraction = total, position: Fraction = position, is_hr: bool = is_hr) -> bool:
            a = price * n
            assert k.max_single_stock_amount is not None
            if position + a > Fraction(k.max_single_stock_amount):
                return False
            if k.max_single_stock_ratio is not None and k.max_single_stock_ratio < 1:
                if total <= 0 or total < position:
                    return False
                if (position + a) > Fraction(k.max_single_stock_ratio) * (total + a):
                    return False
            if k.max_sector_ratio is not None and k.max_sector_ratio < 1 and c.sector is not None:
                if total <= 0:
                    return False
                held = Fraction(case.sector_exposure.get(c.sector, D(0))) + sector_alloc.get(c.sector, Fraction(0))
                if held + a > Fraction(k.max_sector_ratio) * (total + a):
                    return False
            if is_hr:
                if total <= 0:
                    return False
                if Fraction(case.high_risk_exposure) + hr_alloc + a > Fraction(k.max_high_risk_ratio) * (total + a):  # type: ignore[arg-type]
                    return False
            return True

        base = 0
        while caps_ok(base + unit):
            base += unit
        limit = base
        if c.price_tier is not None:
            assert p.tier_quantity_caps is not None
            ratio = Fraction(p.tier_quantity_caps[pt.PriceTier(c.price_tier)])
            limit = int(Fraction(base) * ratio // unit) * unit
        shares = 0
        while shares + unit <= limit and price * (shares + unit) <= remaining:
            shares += unit
        out[c.stock_code] = shares
        amount = price * shares
        remaining -= amount
        allocated_total += amount
        if c.sector is not None:
            sector_alloc[c.sector] = sector_alloc.get(c.sector, Fraction(0)) + amount
        if is_hr:
            hr_alloc += amount
    return out


def random_case(rng: random.Random) -> Case:
    unit = rng.choice([1, 10, 100])
    n = rng.randint(0, 7)
    sectors = ["電気機器", "食料品", "情報通信", None]
    cands: list[pt.AllocationCandidate] = []
    for i in range(n):
        flags = frozenset(
            rng.sample([f.value for f in RiskFlag], rng.randint(0, 2))
        )
        state = rng.choice([RaerState.AVAILABLE] * 5 + [RaerState.UNAVAILABLE])
        value = str(rng.choice([1, 2, 3, 3, 4, 5, 6, 8, 8, 9]))
        confidence = rng.choice([*ConfidenceLevel, None]) if state is RaerState.AVAILABLE else None
        position = rng.choice([0, 0, 0, 50_000, 300_000, 900_000])
        cands.append(
            cand(
                f"{1000 + i}",
                kind=pt.CandidateKind.ADDITIONAL_BUY if position else pt.CandidateKind.NEW_BUY,
                current_price=D(rng.choice([7, 100, 333, 1000, 2500])),
                price_tier=rng.choice([None, *pt.PriceTier]),
                raer=raer(state, value, confidence, flags if state is RaerState.AVAILABLE else frozenset()),
                current_position_value=D(position),
                sector=rng.choice(sectors),
            )
        )
    ratios = [D("0.05"), D("0.1"), D("0.25"), D("0.5"), D("1")]
    constraints = cons(
        max_single_stock_amount=D(rng.choice([20_000, 150_000, 600_000, 5_000_000])),
        max_single_stock_ratio=rng.choice(ratios),
        max_sector_ratio=rng.choice(ratios),
        max_high_risk_ratio=rng.choice(ratios),
        high_risk_flags=frozenset(rng.sample([f.value for f in RiskFlag], rng.randint(1, 3))),
        minimum_trading_unit=unit,
    )
    policy = pol(
        min_raer_to_allocate=D(rng.choice([2, 3, 5])),
        tier_quantity_caps={t: rng.choice([D("0.25"), D("0.5"), D("0.8"), D("1")]) for t in pt.PriceTier},
    )
    cash = D(rng.choice([0, 3_000, 70_000, 400_000, 1_500_000, 8_000_000]))
    ratio_in_force = any(
        r is not None and r < 1
        for r in (constraints.max_single_stock_ratio, constraints.max_sector_ratio, constraints.max_high_risk_ratio)
    )
    portfolio = D(rng.choice([2_000_000, 10_000_000, 40_000_000])) if ratio_in_force else None
    return Case(
        cash=cash,
        portfolio=portfolio,
        candidates=tuple(cands),
        constraints=constraints,
        policy=policy,
        sector_exposure={s: D(rng.choice([0, 100_000, 800_000])) for s in sectors if s},
        high_risk_exposure=D(rng.choice([0, 200_000, 900_000])),
    )


def run_case(case: Case) -> pt.AllocationResult:
    return pa.optimize_allocation(
        owner="owner-a",
        available_cash=case.cash,
        portfolio_value=case.portfolio,
        candidates=case.candidates,
        constraints=case.constraints,
        policy=case.policy,
        sector_exposure=case.sector_exposure,
        high_risk_exposure=case.high_risk_exposure,
    )


def test_the_optimizer_agrees_with_the_one_unit_at_a_time_reference() -> None:
    rng = random.Random(603)
    allocated_cases = 0
    for _ in range(3000):
        case = random_case(rng)
        got = run_case(case)
        if got.status is pt.AllocationStatus.UNAVAILABLE:
            continue  # 方針は完全に作ってあるため、ここへは来ない(来たら下で落ちる)
        expected = reference_shares(case)
        actual = {a.stock_code: a.shares for a in got.allocations if a.rank is not None}
        assert actual == {code: shares for code, shares in expected.items()}, (case, got)
        allocated_cases += bool(got.status is pt.AllocationStatus.ALLOCATED_SOME)
    assert allocated_cases > 600  # 提案が出た入力が十分な数ある(空振りしていない)


def test_the_random_cases_never_make_the_types_reject_the_result() -> None:
    rng = random.Random(7)
    for _ in range(1500):
        run_case(random_case(rng))  # AllocationContractError(型の拒否)を optimizer が作らない


def check_properties(case: Case, got: pt.AllocationResult) -> None:
    k = case.constraints
    # I-1・I-10
    assert got.total_allocated <= case.cash
    if got.remaining_cash is not None:
        assert got.remaining_cash == case.cash - got.total_allocated
    # I-2・I-3
    for a in got.allocations:
        assert a.shares % k.minimum_trading_unit == 0
    # 並び・順位の連続・重複なしは型が保証するが、入力の候補が全て出力に現れることを見る
    assert sorted(a.stock_code for a in got.allocations) == sorted(c.stock_code for c in case.candidates)
    # I-8 RAER が使えない候補は配分されない
    unusable = {c.stock_code for c in case.candidates if c.raer.state is RaerState.UNAVAILABLE}
    for a in got.allocations:
        if a.stock_code in unusable:
            assert a.shares == 0 and a.reason_code is R.RAER_UNAVAILABLE
    # 単一銘柄の金額・比率の上限(独立な検査)
    by = {c.stock_code: c for c in case.candidates}
    for a in got.allocations:
        c = by[a.stock_code]
        if a.shares == 0:
            continue
        assert c.current_position_value + a.amount <= k.max_single_stock_amount  # type: ignore[operator]
        assert a.amount == c.current_price * a.shares


def test_the_basic_properties_hold_for_random_inputs() -> None:
    rng = random.Random(604)
    for _ in range(2000):
        case = random_case(rng)
        check_properties(case, run_case(case))


def test_more_cash_never_reduces_the_total_allocated() -> None:
    rng = random.Random(605)
    for _ in range(600):
        case = random_case(rng)
        bigger = Case(**{**case.__dict__, "cash": case.cash + D(rng.choice([1_000, 100_000, 3_000_000]))})
        assert run_case(bigger).total_allocated >= run_case(case).total_allocated


def test_the_optimizer_leaves_nothing_that_could_still_be_bought() -> None:
    # 最大性: 配分後に、順位順のどの候補にも、1 単元を足せる余地(資金・上限)が残っていない
    rng = random.Random(606)
    checked = 0
    for _ in range(800):
        case = random_case(rng)
        got = run_case(case)
        for a in got.allocations:
            if a.decision is pt.CandidateDecision.NOT_ALLOCATED and a.rank is not None:
                checked += 1
                assert a.reason_code is not R.ALLOCATED
                if a.reason_code is R.INSUFFICIENT_CASH_REMAINING:
                    unit_cost = by_code_candidate(case, a.stock_code).current_price * case.constraints.minimum_trading_unit
                    # 配分後の残り資金では 1 単元に足りない(そのとき以降に買える額が残っていない)
                    assert (got.remaining_cash or D(0)) < unit_cost or a.cash_shortfall > 0
    assert checked > 100


def by_code_candidate(case: Case, code: str) -> pt.AllocationCandidate:
    return next(c for c in case.candidates if c.stock_code == code)


def test_the_result_is_deterministic_and_does_not_depend_on_the_input_order() -> None:
    rng = random.Random(607)
    for _ in range(200):
        case = random_case(rng)
        shuffled = list(case.candidates)
        rng.shuffle(shuffled)
        again = Case(**{**case.__dict__, "candidates": tuple(shuffled)})
        assert run_case(case) == run_case(again) == run_case(case)


# --- 9 純粋性・休眠(AST)---------------------------------------------------------------------

_ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "decimal",
    "fractions",
    "jstock_advisor.domain.entities.enums",
    "jstock_advisor.domain.signals.buy_affordability",
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
    words = ("send", "push", "post", "publish", "invoke", "deliver", "notify", "save", "write", "persist")
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
    src = _REPO / "src" / "jstock_advisor"
    for path in src.rglob("*.py"):
        if path == _MODULE_PATH:
            continue
        if "portfolio_allocation import" in path.read_text(encoding="utf-8") or "signals.portfolio_allocation\n" in path.read_text(encoding="utf-8"):
            assert path.relative_to(src).as_posix() in _ALLOWED_IMPORTERS, str(path)


def test_the_optimizer_reuses_the_affordability_function_of_m2() -> None:
    assert "compute_buy_affordability" in _MODULE_PATH.read_text(encoding="utf-8")


def test_the_candidates_accepts_any_sequence_type() -> None:
    seq: Sequence[pt.AllocationCandidate] = (cand(),)
    assert run(candidates=seq).status is pt.AllocationStatus.ALLOCATED_SOME
