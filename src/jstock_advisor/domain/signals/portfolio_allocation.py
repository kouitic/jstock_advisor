"""Issue #603 / PR-C: 配分の最適化 `optimize_allocation`(C603-v1.2。#128。dormant)。

買い判定を済ませた候補(BUY 系)に、買付余力を RAER の順に貪欲に割り当てる**純粋関数**である。
どこからも import されない(現行の判定・通知・保存・config・AWS は変わらない)。時計・I/O・永続化・
ログに触れない。閾値は決めない(数値の None は『未決定』= POLICY_INCOMPLETE → 配分しない)。
資金入替(PR-D)・専用 YAML(PR-E)・入力の組み立てと shadow 記録(PR-F)は含まない。

## 判定の順序(上が優先)

```
 1 入力の検証(型・符号・重複)                         -> AllocationContractError(値を含めない)
 2 買付余力が未登録(None。0 円とは別)                 -> UNAVAILABLE(CASH_NOT_REGISTERED)
 3 効いている比率上限に必要な総額・業種・high-risk の保有が揃わない
                                                       -> UNAVAILABLE(PORTFOLIO_VALUE_UNAVAILABLE)
 4 RAER が使える候補の comparison_key が揃わない / 方針の鍵と違う
                                                       -> UNAVAILABLE(RAER_NOT_COMPARABLE)
 5 方針が不完全(`AllocationPolicy.missing()` が空でない)
                                                       -> UNAVAILABLE(POLICY_INCOMPLETE)。配分 0 の
                                                          『順位だけの shadow』にする
 6 候補ごと: RAER が使えない -> RAER_UNAVAILABLE / hurdle 未満 -> BELOW_HURDLE(順位なし)
 7 順位順の貪欲: 位置(金額 -> 比率)-> 業種 -> high-risk -> 段階 -> 資金 の順で上限を当てる
```

## 順位と貪欲

    ・順位   RAER の value 降順 -> confidence 降順(HIGH > MEDIUM > LOW > None)-> stock_code 昇順。
             入力の並びに依らず決まる
    ・上限   各上限は『購入額 a』の不等式で、厳密な有理数(Fraction)で床にする。比率は購入後の構成比
             (保有 + a)/(総額 + a) <= r(分母に購入額を含む。M2・add_on_risk と同じ)。総額・業種・
             high-risk の保有には、先に配分した候補の購入額を足す(順位の高い候補が先に枠を使う)
    ・段階   候補が価格帯の段階を持つとき、他の上限で決まる単元数に段階の割合を掛けて床にする
             (段階を持たない候補は段階による制限なし)
    ・資金   他の上限で決まった単元数のうち、残りの資金で買える数だけを買う。0 になったときは
             INSUFFICIENT_CASH_REMAINING と、資金だけが足りなかった需要(cash_shortfall)を残す
    ・正確さ 株数は常に『床』。購入総額 <= 買付余力・各上限は、計算方法そのもので保証する

## 到達性

M2(buy_affordability)の計算は、資金を除いた上限だけの株数(cap_limited_shares)を返せず、業種・
high-risk・段階の上限も持たないため、本 module は同じ式を自前の Fraction で持つ。結果が M2 と一致
することは契約テストで固定している。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Final, cast

from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.signals.portfolio_allocation_types import (
    AllocationCandidate,
    AllocationConstraints,
    AllocationContractError,
    AllocationPolicy,
    AllocationResult,
    AllocationStatus,
    CandidateAllocation,
    CandidateDecision,
    PriceTier,
    ReasonCode,
    UnavailableReason,
)
from jstock_advisor.domain.valuation.raer import RaerState

#: 金額の厳密な計算に使う精度(通常の context の桁数で丸めない)
_EXACT_PRECISION: Final = 400

_CONFIDENCE_RANK: Final[Mapping[ConfidenceLevel | None, int]] = {
    ConfidenceLevel.HIGH: 2,
    ConfidenceLevel.MEDIUM: 1,
    ConfidenceLevel.LOW: 0,
    None: -1,
}


def _fail(name: str, problem: str) -> AllocationContractError:
    return AllocationContractError(f"{name}: {problem}")


def confidence_rank(confidence: ConfidenceLevel | None) -> int:
    """同じ RAER の value のときの優先度(大きいほど先)。HIGH > MEDIUM > LOW > None。"""
    return _CONFIDENCE_RANK[confidence]


# --- 入力の検証 -------------------------------------------------------------------------------


def _non_negative(name: str, value: object) -> Decimal:
    # bool は int のサブクラス、float・int は Decimal ではない。金額に float を混ぜない
    if not isinstance(value, Decimal):
        raise _fail(name, "must be a Decimal")
    if not value.is_finite():
        raise _fail(name, "must be finite")
    if value < 0:
        raise _fail(name, "must not be negative")
    return value


def _optional_non_negative(name: str, value: object) -> Decimal | None:
    return None if value is None else _non_negative(name, value)


def _validated_candidates(candidates: object) -> tuple[AllocationCandidate, ...]:
    if isinstance(candidates, str) or not isinstance(candidates, Sequence):
        raise _fail("candidates", "must be a sequence")
    if any(not isinstance(c, AllocationCandidate) for c in candidates):
        raise _fail("candidates", "has an item of the wrong type")
    items = tuple(candidates)
    if len({c.stock_code for c in items}) != len(items):
        raise _fail("candidates", "must not repeat a stock")
    return items


def _validated_sector_exposure(exposure: object) -> Mapping[str, Decimal] | None:
    if exposure is None:
        return None
    if not isinstance(exposure, Mapping):
        raise _fail("sector_exposure", "must be a mapping")
    checked: dict[str, Decimal] = {}
    for sector, amount in exposure.items():
        if not isinstance(sector, str) or not sector.strip():
            raise _fail("sector_exposure", "has a key that is not a non-empty str")
        checked[sector] = _non_negative("sector_exposure", amount)
    return checked


# --- 厳密な数量の計算 -------------------------------------------------------------------------


def _floor_units(numerator: Fraction, denominator: Fraction) -> int:
    """numerator / denominator の床(負なら 0)。"""
    if numerator <= 0:
        return 0
    return int(numerator // denominator)


def _ratio_headroom_units(
    ratio: Decimal, total: Fraction, held: Fraction, unit_cost: Fraction
) -> int:
    """(held + a) / (total + a) <= r  <=>  a <= (r * total - held) / (1 - r)  を満たす最大の単元数。

    r < 1 のときだけ呼ぶ(r = 1 は上限なしと同じで、呼び出し側が除く)。
    """
    r = Fraction(ratio)
    return _floor_units((r * total - held) / (1 - r), unit_cost)


@dataclass(frozen=True)
class _Rules:
    """方針が完全(`missing()` が空)のときの、None を除いた決定値."""

    hurdle: Decimal
    amount_cap: Decimal
    single_ratio: Decimal
    sector_ratio: Decimal
    high_risk_ratio: Decimal
    high_risk_flags: frozenset[str]
    tier_caps: Mapping[PriceTier, Decimal]
    unit: int

    @property
    def single_in_force(self) -> bool:
        return self.single_ratio < 1

    @property
    def sector_in_force(self) -> bool:
        return self.sector_ratio < 1

    @property
    def high_risk_in_force(self) -> bool:
        return self.high_risk_ratio < 1


def _rules(constraints: AllocationConstraints, policy: AllocationPolicy) -> _Rules:
    """`policy.missing(constraints)` が空だと確かめた後に呼ぶ(全項目が非 None)."""
    return _Rules(
        hurdle=cast(Decimal, policy.min_raer_to_allocate),
        amount_cap=cast(Decimal, constraints.max_single_stock_amount),
        single_ratio=cast(Decimal, constraints.max_single_stock_ratio),
        sector_ratio=cast(Decimal, constraints.max_sector_ratio),
        high_risk_ratio=cast(Decimal, constraints.max_high_risk_ratio),
        high_risk_flags=constraints.high_risk_flags,
        tier_caps=cast(Mapping[PriceTier, Decimal], policy.tier_quantity_caps),
        unit=constraints.minimum_trading_unit,
    )


@dataclass
class _Book:
    """貪欲の途中経過(先に配分した候補の購入額)。"""

    total: Fraction
    cash: Fraction
    sector: dict[str, Fraction]
    high_risk: Fraction


def _cap_units(
    candidate: AllocationCandidate,
    rules: _Rules,
    book: _Book,
    sector_exposure: Mapping[str, Decimal],
    high_risk_exposure: Decimal,
) -> tuple[int, ReasonCode | None]:
    """資金を除いた上限だけで決まる単元数と、0 のときの理由(位置 -> 業種 -> high-risk -> 段階)."""
    unit_cost = Fraction(candidate.current_price) * rules.unit
    position = Fraction(candidate.current_position_value)

    # 位置: 金額 -> 比率
    amount_cap = Fraction(rules.amount_cap)
    units = _floor_units(amount_cap - position, unit_cost)
    if units == 0:
        # 上限そのものが 1 単元の購入額に満たないときは、売買単位の問題として区別する
        if amount_cap < unit_cost:
            return 0, ReasonCode.BELOW_MINIMUM_TRADING_UNIT
        return 0, ReasonCode.POSITION_AMOUNT_CAP_APPLIED
    if rules.single_in_force:
        limit = _ratio_headroom_units(rules.single_ratio, book.total, position, unit_cost)
        if limit == 0:
            return 0, ReasonCode.POSITION_RATIO_CAP_APPLIED
        units = min(units, limit)

    # 業種
    if rules.sector_in_force:
        if candidate.sector is None:
            return 0, ReasonCode.SECTOR_UNKNOWN
        held = Fraction(sector_exposure.get(candidate.sector, Decimal(0))) + book.sector.get(
            candidate.sector, Fraction(0)
        )
        limit = _ratio_headroom_units(rules.sector_ratio, book.total, held, unit_cost)
        if limit == 0:
            return 0, ReasonCode.SECTOR_RATIO_CAP_APPLIED
        units = min(units, limit)

    # high-risk
    if rules.high_risk_in_force:
        if not candidate.raer.risk_flags_available:
            return 0, ReasonCode.HIGH_RISK_UNKNOWN
        if candidate.raer.risk_flags & rules.high_risk_flags:
            held = Fraction(high_risk_exposure) + book.high_risk
            limit = _ratio_headroom_units(rules.high_risk_ratio, book.total, held, unit_cost)
            if limit == 0:
                return 0, ReasonCode.HIGH_RISK_ALLOCATION_CAP_APPLIED
            units = min(units, limit)

    # 段階(段階を持たない候補は制限なし)
    if candidate.price_tier is not None:
        scaled = int(Fraction(units) * Fraction(rules.tier_caps[candidate.price_tier]))
        if scaled == 0:
            return 0, ReasonCode.TIER_CAP_APPLIED
        units = scaled
    return units, None


# --- 順位 -------------------------------------------------------------------------------------


def _usable(candidate: AllocationCandidate) -> bool:
    raer = candidate.raer
    return raer.state is not RaerState.UNAVAILABLE and raer.value is not None


def _rank_order(candidates: Sequence[AllocationCandidate]) -> list[AllocationCandidate]:
    """value 降順 -> confidence 降順 -> stock_code 昇順(安定ソートを後ろの鍵から順に掛ける)."""
    ordered = sorted(candidates, key=lambda c: c.stock_code)
    ordered.sort(key=lambda c: confidence_rank(c.raer.confidence), reverse=True)
    # value を符号反転すると context の精度で丸まりうるため、反転せず reverse で降順にする
    ordered.sort(key=lambda c: cast(Decimal, c.raer.value), reverse=True)
    return ordered


def _not_allocated(
    code: str,
    reason: ReasonCode,
    rank: int | None,
    cap_limited: int = 0,
    shortfall: Decimal = Decimal(0),
) -> CandidateAllocation:
    return CandidateAllocation(
        stock_code=code,
        decision=CandidateDecision.NOT_ALLOCATED,
        shares=0,
        amount=Decimal(0),
        price_tier_used=None,
        reason_code=reason,
        rank=rank,
        cap_limited_shares=cap_limited,
        cash_shortfall=shortfall,
    )


def _arranged(items: list[CandidateAllocation]) -> tuple[CandidateAllocation, ...]:
    """型が要求する並び: rank 昇順 -> rank なしは後 -> stock_code 昇順."""
    return tuple(sorted(items, key=lambda a: (a.rank is None, a.rank or 0, a.stock_code)))


def _unavailable(
    owner: str,
    reason: UnavailableReason,
    missing: tuple[str, ...],
    allocations: tuple[CandidateAllocation, ...] = (),
) -> AllocationResult:
    return AllocationResult(
        owner=owner,
        status=AllocationStatus.UNAVAILABLE,
        unavailable_reason=reason,
        allocations=allocations,
        total_allocated=Decimal(0),
        remaining_cash=None,
        policy_missing=missing,
    )


def _shadow(
    owner: str, candidates: Sequence[AllocationCandidate], missing: tuple[str, ...]
) -> AllocationResult:
    """方針が不完全な日: 配分は 0 で、RAER が使える候補に順位だけを付ける."""
    ranked = _rank_order([c for c in candidates if _usable(c)])
    items = [
        _not_allocated(c.stock_code, ReasonCode.RAER_UNAVAILABLE, None)
        for c in candidates
        if not _usable(c)
    ]
    items += [
        _not_allocated(c.stock_code, ReasonCode.POLICY_INCOMPLETE, position)
        for position, c in enumerate(ranked, start=1)
    ]
    return _unavailable(owner, UnavailableReason.POLICY_INCOMPLETE, missing, _arranged(items))


# --- 公開関数 ---------------------------------------------------------------------------------


def optimize_allocation(
    *,
    owner: str,
    available_cash: Decimal | None,
    portfolio_value: Decimal | None,
    candidates: Sequence[AllocationCandidate],
    constraints: AllocationConstraints,
    policy: AllocationPolicy,
    sector_exposure: Mapping[str, Decimal] | None = None,
    high_risk_exposure: Decimal | None = None,
) -> AllocationResult:
    """買付余力を、RAER の順に貪欲に候補へ割り当てる(純粋関数)。

    owner               結果に残す識別子(非空の str)
    available_cash      買付余力。None = 未登録(0 円ではない)
    portfolio_value     owner 全体の保有評価額(候補の保有を含み、買付余力を含まない)。
                        比率の上限(< 1)が効いているときだけ使う。None / 0 は使えない扱い
    candidates          買い判定(BUY 系)を済ませた候補。stock_code は重複しない
    sector_exposure     業種ごとの現在の保有評価額(業種の上限が効くときに必要)
    high_risk_exposure  high-risk と数える銘柄の現在の保有評価額(high-risk の上限が効くときに必要)

    入力の矛盾は AllocationContractError(値を含めない)。それ以外は結果の status で表す。
    """
    if not isinstance(owner, str) or not owner.strip():
        raise _fail("owner", "must be a non-empty str")
    cash = _optional_non_negative("available_cash", available_cash)
    total_value = _optional_non_negative("portfolio_value", portfolio_value)
    items = _validated_candidates(candidates)
    if not isinstance(constraints, AllocationConstraints):
        raise _fail("constraints", "has the wrong type")
    if not isinstance(policy, AllocationPolicy):
        raise _fail("policy", "has the wrong type")
    sectors = _validated_sector_exposure(sector_exposure)
    risk_exposure = _optional_non_negative("high_risk_exposure", high_risk_exposure)

    missing = policy.missing(constraints)

    # 2 買付余力が未登録(0 円とは別)
    if cash is None:
        return _unavailable(owner, UnavailableReason.CASH_NOT_REGISTERED, missing)

    # 3 効いている比率上限に必要な保有の情報(fail-close)
    single_in_force = _in_force(constraints.max_single_stock_ratio)
    sector_in_force = _in_force(constraints.max_sector_ratio)
    risk_in_force = _in_force(constraints.max_high_risk_ratio)
    if (single_in_force or sector_in_force or risk_in_force) and (
        total_value is None or total_value <= 0
    ):
        return _unavailable(owner, UnavailableReason.PORTFOLIO_VALUE_UNAVAILABLE, missing)
    if (sector_in_force and sectors is None) or (risk_in_force and risk_exposure is None):
        return _unavailable(owner, UnavailableReason.PORTFOLIO_VALUE_UNAVAILABLE, missing)

    # 4 比較可能性: RAER が使える候補どうし・方針の鍵と同じ comparison_key でなければ比べない
    keys = {c.raer.comparison_key for c in items if _usable(c)}
    if len(keys) > 1 or (policy.comparison_key is not None and keys - {policy.comparison_key}):
        return _unavailable(owner, UnavailableReason.RAER_NOT_COMPARABLE, missing)

    # 5 方針が不完全 -> 配分しない(順位だけの shadow)
    if missing:
        return _shadow(owner, items, missing)

    return _allocate(
        owner=owner,
        cash=cash,
        portfolio_value=total_value,
        candidates=items,
        rules=_rules(constraints, policy),
        sector_exposure=sectors or {},
        high_risk_exposure=risk_exposure or Decimal(0),
    )


def _in_force(ratio: Decimal | None) -> bool:
    """比率の上限が効いている(決定済みで 1 未満)。None は未決定で、効いているとは数えない."""
    return ratio is not None and ratio < 1


def _allocate(
    *,
    owner: str,
    cash: Decimal,
    portfolio_value: Decimal | None,
    candidates: Sequence[AllocationCandidate],
    rules: _Rules,
    sector_exposure: Mapping[str, Decimal],
    high_risk_exposure: Decimal,
) -> AllocationResult:
    items: list[CandidateAllocation] = []
    eligible: list[AllocationCandidate] = []
    for candidate in candidates:
        value = candidate.raer.value
        if not _usable(candidate):
            items.append(_not_allocated(candidate.stock_code, ReasonCode.RAER_UNAVAILABLE, None))
        elif cast(Decimal, value) < rules.hurdle:
            items.append(_not_allocated(candidate.stock_code, ReasonCode.BELOW_HURDLE, None))
        else:
            eligible.append(candidate)

    book = _Book(
        total=Fraction(portfolio_value) if portfolio_value is not None else Fraction(0),
        cash=Fraction(cash),
        sector={},
        high_risk=Fraction(0),
    )
    with localcontext() as context:
        context.prec = _EXACT_PRECISION
        for position, candidate in enumerate(_rank_order(eligible), start=1):
            items.append(
                _decide(candidate, position, rules, book, sector_exposure, high_risk_exposure)
            )

    allocated = [a for a in items if a.decision is CandidateDecision.ALLOCATED]
    total = sum((a.amount for a in allocated), Decimal(0))
    return AllocationResult(
        owner=owner,
        status=AllocationStatus.ALLOCATED_SOME if allocated else AllocationStatus.NO_ACTION,
        unavailable_reason=None,
        allocations=_arranged(items),
        total_allocated=total,
        remaining_cash=cash - total,
        policy_missing=(),
    )


def _decide(
    candidate: AllocationCandidate,
    rank: int,
    rules: _Rules,
    book: _Book,
    sector_exposure: Mapping[str, Decimal],
    high_risk_exposure: Decimal,
) -> CandidateAllocation:
    code = candidate.stock_code
    cap_units, cap_reason = _cap_units(candidate, rules, book, sector_exposure, high_risk_exposure)
    if cap_reason is not None:
        return _not_allocated(code, cap_reason, rank)

    unit_cost = Fraction(candidate.current_price) * rules.unit
    units = min(cap_units, _floor_units(book.cash, unit_cost))
    cap_shares = cap_units * rules.unit
    if units == 0:
        # 資金だけが足りなかった需要(#604 へ渡す根拠)
        shortfall = candidate.current_price * cap_shares - _to_decimal(book.cash)
        return _not_allocated(
            code, ReasonCode.INSUFFICIENT_CASH_REMAINING, rank, cap_shares, shortfall
        )

    shares = units * rules.unit
    amount = candidate.current_price * shares
    spent = Fraction(amount)
    book.cash -= spent
    book.total += spent
    if candidate.sector is not None:
        book.sector[candidate.sector] = book.sector.get(candidate.sector, Fraction(0)) + spent
    if rules.high_risk_in_force and candidate.raer.risk_flags & rules.high_risk_flags:
        book.high_risk += spent
    return CandidateAllocation(
        stock_code=code,
        decision=CandidateDecision.ALLOCATED,
        shares=shares,
        amount=amount,
        price_tier_used=candidate.price_tier,
        reason_code=ReasonCode.ALLOCATED,
        rank=rank,
        cap_limited_shares=cap_shares,
        cash_shortfall=Decimal(0),
    )


def _to_decimal(value: Fraction) -> Decimal:
    """Fraction(残り資金)を Decimal へ。入力は Decimal 由来で、10 の冪を分母に持つため厳密."""
    return Decimal(value.numerator) / Decimal(value.denominator)
