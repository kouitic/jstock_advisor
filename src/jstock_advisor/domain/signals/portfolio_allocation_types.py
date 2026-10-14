"""Issue #603 / PR-A: 配分(Portfolio Allocation)の型・方針の完全性(C603-v1.3)。

**純粋な型と検証だけ**である。計算(optimizer。PR-C)・閾値の決定・専用 YAML と loader(PR-E)・
入力の組み立てと shadow 記録(PR-F)・資金入替(PR-D)は含まない。ネットワーク・ファイル・AWS・
永続化・時計に触れず、どこからも import されない(dormant)。現行の判定・通知・保存の挙動を変えない。

## この module が型で固定すること

```
・数値の方針(hurdle・各上限・段階の割合)と比較可能性の鍵が未決定(None)のあいだ、方針は『不完全』
  で、配分を持てない(USER の決定: None = POLICY_INCOMPLETE → 配分しない)。完全性は
  `AllocationPolicy.missing()` が判定する。『上限なし』と決めたいときは、比率なら 1(100%)を明示する
  (None は『未決定』であり『制約なし』ではない)
・結果の型は矛盾した組合せを作れない: ALLOCATED <=> shares > 0 <=> reason = ALLOCATED /
  UNAVAILABLE のとき配分なし(方針不完全のときだけ『順位だけの shadow』= すべて NOT_ALLOCATED
  で、理由は POLICY_INCOMPLETE か RAER_UNAVAILABLE)/ 順位は 1..k で連続し、並びは固定
  (rank 昇順 → rank なしは後 → stock_code 昇順)/ 購入総額 = 配分した金額の合計
・入力の検証: stock_code は空でない / 現在価格 > 0 / 買い判定は BUY 系のみ
  (optimizer は適格性を覆さない)/ 評価額 >= 0 / 比率は (0, 1] / 売買単位 > 0 /
  Decimal 以外・bool・NaN・Infinity を拒否
・例外のメッセージには項目名と固定の問題名だけを含め、金額・株数などの値を含めない(資産情報をログへ
  出さない。#909 の教訓)。例外の構築は `_fail` の 1 箇所だけである
```
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from jstock_advisor.domain.entities.enums import BUY_FAMILY_ACTIONS, BuyAction
from jstock_advisor.domain.valuation.raer import RaerResult, RiskFlag

# 契約の版。規約: 『理由コード(語彙)の追加』は版を上げる(記録を読む側が『この語を持ち得る記録』を
# 版で選別できる)/ 『記録の optional key の追加』は schema_version を据え置く。
# v1.3 = ReasonCode に RAER_NOT_COMPARABLE を追加(比較できない日の候補ごとの診断行。#603 段 4)
CONTRACT_VERSION: Final = "C603-v1.3"

#: 方針の項目名の語彙(`AllocationPolicy.missing()` が返してよい名前)。この並びで返す
POLICY_ITEMS: Final = (
    "min_raer_to_allocate",
    "comparison_key",
    "max_single_stock_amount",
    "max_single_stock_ratio",
    "max_sector_ratio",
    "max_high_risk_ratio",
    "high_risk_flags",
    "tier_quantity_caps",
)
_RISK_FLAG_VALUES: Final = frozenset(flag.value for flag in RiskFlag)


class AllocationContractError(ValueError):
    """配分の型の不変条件に反する構築。メッセージに値(金額・株数など)を含めない。"""


def _fail(name: str, problem: str) -> AllocationContractError:
    return AllocationContractError(f"{name}: {problem}")


# --- 列挙 -------------------------------------------------------------------------------------


class CandidateKind(StrEnum):
    NEW_BUY = "NEW_BUY"
    ADDITIONAL_BUY = "ADDITIONAL_BUY"


class PriceTier(StrEnum):
    """現在価格が買い価格帯のどの段階に達したか(entry_price_range の starter 等に対応)."""

    ENTRY = "ENTRY"
    STANDARD = "STANDARD"
    STRONG = "STRONG"


class AllocationStatus(StrEnum):
    ALLOCATED_SOME = "ALLOCATED_SOME"
    NO_ACTION = "NO_ACTION"
    UNAVAILABLE = "UNAVAILABLE"


class UnavailableReason(StrEnum):
    CASH_NOT_REGISTERED = "CASH_NOT_REGISTERED"
    PORTFOLIO_VALUE_UNAVAILABLE = "PORTFOLIO_VALUE_UNAVAILABLE"
    RAER_NOT_COMPARABLE = "RAER_NOT_COMPARABLE"
    POLICY_INCOMPLETE = "POLICY_INCOMPLETE"


class CandidateDecision(StrEnum):
    ALLOCATED = "ALLOCATED"
    NOT_ALLOCATED = "NOT_ALLOCATED"


class ReasonCode(StrEnum):
    ALLOCATED = "ALLOCATED"
    RAER_UNAVAILABLE = "RAER_UNAVAILABLE"
    POLICY_INCOMPLETE = "POLICY_INCOMPLETE"
    BELOW_HURDLE = "BELOW_HURDLE"
    INSUFFICIENT_CASH_REMAINING = "INSUFFICIENT_CASH_REMAINING"
    BELOW_MINIMUM_TRADING_UNIT = "BELOW_MINIMUM_TRADING_UNIT"
    POSITION_AMOUNT_CAP_APPLIED = "POSITION_AMOUNT_CAP_APPLIED"
    POSITION_RATIO_CAP_APPLIED = "POSITION_RATIO_CAP_APPLIED"
    SECTOR_RATIO_CAP_APPLIED = "SECTOR_RATIO_CAP_APPLIED"
    SECTOR_UNKNOWN = "SECTOR_UNKNOWN"
    HIGH_RISK_ALLOCATION_CAP_APPLIED = "HIGH_RISK_ALLOCATION_CAP_APPLIED"
    HIGH_RISK_UNKNOWN = "HIGH_RISK_UNKNOWN"
    TIER_CAP_APPLIED = "TIER_CAP_APPLIED"
    #: ★ 診断用 ★(段 4)。『この日 RAER が比べられなかったので、この候補は評価していない』。
    #: 順位ではない(rank を持てない)・配分でもない(株数・金額は 0)
    RAER_NOT_COMPARABLE = "RAER_NOT_COMPARABLE"


#: 順位付けに進まない(= rank を持たない)理由
_UNRANKED_REASONS: Final = frozenset(
    {ReasonCode.RAER_UNAVAILABLE, ReasonCode.BELOW_HURDLE, ReasonCode.RAER_NOT_COMPARABLE}
)
#: 方針が不完全のときの『順位だけの shadow』が持てる理由
_SHADOW_REASONS: Final = frozenset({ReasonCode.POLICY_INCOMPLETE, ReasonCode.RAER_UNAVAILABLE})
#: 比較できない日(段 4)の診断行が持てる理由。★ 順位を持たない ★(_UNRANKED_REASONS の部分集合)
_NOT_COMPARABLE_REASONS: Final = frozenset(
    {ReasonCode.RAER_NOT_COMPARABLE, ReasonCode.RAER_UNAVAILABLE}
)


# --- 検証の補助 -------------------------------------------------------------------------------


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(name, "must be a non-empty str")
    return value


def _decimal(name: str, value: object) -> Decimal:
    # bool は int のサブクラス、float・int は Decimal ではない。金額・比率に float を混ぜない
    if not isinstance(value, Decimal):
        raise _fail(name, "must be a Decimal")
    if not value.is_finite():
        raise _fail(name, "must be finite")
    return value


def _non_negative(name: str, value: object) -> Decimal:
    number = _decimal(name, value)
    if number < 0:
        raise _fail(name, "must not be negative")
    return number


def _positive(name: str, value: object) -> Decimal:
    number = _decimal(name, value)
    if number <= 0:
        raise _fail(name, "must be positive")
    return number


def _ratio(name: str, value: object) -> Decimal:
    number = _decimal(name, value)
    if not (Decimal(0) < number <= Decimal(1)):
        raise _fail(name, "must be in (0, 1]")
    return number


def _int(name: str, value: object, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(name, "must be an int")
    if value < minimum:
        raise _fail(name, "is below its minimum")
    return value


def _enum[E: StrEnum](name: str, value: object, enum_type: type[E]) -> E:
    if not isinstance(value, enum_type):
        raise _fail(name, "has the wrong type")
    return value


# --- 入力: 候補・制約・方針 -------------------------------------------------------------------


@dataclass(frozen=True)
class AllocationCandidate:
    """配分の候補 1 件。買い判定(BUY 系)を済ませたものだけを受ける。"""

    stock_code: str
    kind: CandidateKind
    buy_action: BuyAction
    current_price: Decimal
    price_as_of: dt.datetime | None
    price_tier: PriceTier | None
    raer: RaerResult
    current_position_value: Decimal
    sector: str | None

    def __post_init__(self) -> None:
        _text("candidate.stock_code", self.stock_code)
        _enum("candidate.kind", self.kind, CandidateKind)
        _enum("candidate.buy_action", self.buy_action, BuyAction)
        if self.buy_action not in BUY_FAMILY_ACTIONS:
            # optimizer は買い判定の適格性を覆さない
            raise _fail("candidate.buy_action", "must be a buy-family action")
        _positive("candidate.current_price", self.current_price)
        if self.price_as_of is not None and (
            not isinstance(self.price_as_of, dt.datetime) or self.price_as_of.tzinfo is None
        ):
            raise _fail("candidate.price_as_of", "must be a timezone-aware datetime")
        if self.price_tier is not None:
            _enum("candidate.price_tier", self.price_tier, PriceTier)
        if not isinstance(self.raer, RaerResult):
            raise _fail("candidate.raer", "has the wrong type")
        position = _non_negative("candidate.current_position_value", self.current_position_value)
        if self.kind is CandidateKind.NEW_BUY and position != 0:
            raise _fail("candidate.current_position_value", "a new buy has no position")
        if self.sector is not None:
            _text("candidate.sector", self.sector)


@dataclass(frozen=True)
class AllocationConstraints:
    """制約。数値の None は『未決定』である(『制約なし』は比率なら 1 で明示する)。"""

    max_single_stock_amount: Decimal | None
    max_single_stock_ratio: Decimal | None
    max_sector_ratio: Decimal | None
    max_high_risk_ratio: Decimal | None
    high_risk_flags: frozenset[str]
    minimum_trading_unit: int

    def __post_init__(self) -> None:
        if self.max_single_stock_amount is not None:
            _non_negative("constraints.max_single_stock_amount", self.max_single_stock_amount)
        if self.max_single_stock_ratio is not None:
            _ratio("constraints.max_single_stock_ratio", self.max_single_stock_ratio)
        if self.max_sector_ratio is not None:
            _ratio("constraints.max_sector_ratio", self.max_sector_ratio)
        if self.max_high_risk_ratio is not None:
            _ratio("constraints.max_high_risk_ratio", self.max_high_risk_ratio)
        if not isinstance(self.high_risk_flags, frozenset):
            raise _fail("constraints.high_risk_flags", "must be a frozenset")
        if any(not isinstance(flag, str) for flag in self.high_risk_flags):
            raise _fail("constraints.high_risk_flags", "must hold str values")
        if self.high_risk_flags - _RISK_FLAG_VALUES:
            raise _fail("constraints.high_risk_flags", "has a value outside the vocabulary")
        _int("constraints.minimum_trading_unit", self.minimum_trading_unit, minimum=1)


@dataclass(frozen=True)
class AllocationPolicy:
    """人が決める値(Shadow → backtest → human review の後)。None は未決定。"""

    min_raer_to_allocate: Decimal | None
    comparison_key: str | None
    tier_quantity_caps: Mapping[PriceTier, Decimal] | None

    def __post_init__(self) -> None:
        if self.min_raer_to_allocate is not None:
            _decimal("policy.min_raer_to_allocate", self.min_raer_to_allocate)
        if self.comparison_key is not None:
            _text("policy.comparison_key", self.comparison_key)
        caps = self.tier_quantity_caps
        if caps is None:
            return
        if not isinstance(caps, Mapping):
            raise _fail("policy.tier_quantity_caps", "must be a mapping")
        if set(caps) != set(PriceTier) or any(not isinstance(k, PriceTier) for k in caps):
            raise _fail("policy.tier_quantity_caps", "must cover every tier")
        for tier in PriceTier:
            _ratio("policy.tier_quantity_caps", caps[tier])
        # 渡された mapping を後から変えても方針が変わらないよう、コピーして読み取り専用にする
        object.__setattr__(self, "tier_quantity_caps", MappingProxyType(dict(caps)))

    def missing(self, constraints: AllocationConstraints) -> tuple[str, ...]:
        """未決定の項目名(POLICY_ITEMS の並び)。空でなければ方針は不完全(= 配分しない)。"""
        if not isinstance(constraints, AllocationConstraints):
            raise _fail("policy.missing", "needs the constraints")
        missing: list[str] = []
        if self.min_raer_to_allocate is None:
            missing.append("min_raer_to_allocate")
        if self.comparison_key is None:
            missing.append("comparison_key")
        if constraints.max_single_stock_amount is None:
            missing.append("max_single_stock_amount")
        if constraints.max_single_stock_ratio is None:
            missing.append("max_single_stock_ratio")
        if constraints.max_sector_ratio is None:
            missing.append("max_sector_ratio")
        if constraints.max_high_risk_ratio is None:
            missing.append("max_high_risk_ratio")
        elif constraints.max_high_risk_ratio < 1 and not constraints.high_risk_flags:
            # 上限を決めたのに、何を high-risk と数えるかが空 = 未決定
            missing.append("high_risk_flags")
        if self.tier_quantity_caps is None:
            missing.append("tier_quantity_caps")
        return tuple(missing)

    def is_complete(self, constraints: AllocationConstraints) -> bool:
        return not self.missing(constraints)


# --- 出力: 候補ごとの配分・結果 ---------------------------------------------------------------


@dataclass(frozen=True)
class CandidateAllocation:
    stock_code: str
    decision: CandidateDecision
    shares: int
    amount: Decimal
    price_tier_used: PriceTier | None
    reason_code: ReasonCode
    rank: int | None
    cap_limited_shares: int
    cash_shortfall: Decimal

    def __post_init__(self) -> None:
        _text("allocation.stock_code", self.stock_code)
        _enum("allocation.decision", self.decision, CandidateDecision)
        _enum("allocation.reason_code", self.reason_code, ReasonCode)
        _int("allocation.shares", self.shares, minimum=0)
        _non_negative("allocation.amount", self.amount)
        if self.price_tier_used is not None:
            _enum("allocation.price_tier_used", self.price_tier_used, PriceTier)
        if self.rank is not None:
            _int("allocation.rank", self.rank, minimum=1)
        _int("allocation.cap_limited_shares", self.cap_limited_shares, minimum=0)
        _non_negative("allocation.cash_shortfall", self.cash_shortfall)
        if self.decision is CandidateDecision.ALLOCATED:
            self._validate_allocated()
        else:
            self._validate_not_allocated()

    def _validate_allocated(self) -> None:
        if self.shares <= 0 or self.reason_code is not ReasonCode.ALLOCATED:
            raise _fail("allocation", "an allocated candidate has shares and its own reason")
        if self.amount <= 0:
            # 株数を配分したのに金額が 0 の結果(購入総額 0 円)を作れない
            raise _fail("allocation.amount", "an allocated candidate has a positive amount")
        if self.rank is None:
            raise _fail("allocation.rank", "an allocated candidate is ranked")
        if self.shares > self.cap_limited_shares:
            raise _fail("allocation.shares", "exceeds the shares allowed by the caps")
        if self.cash_shortfall != 0:
            raise _fail("allocation.cash_shortfall", "an allocated candidate has no shortfall")

    def _validate_not_allocated(self) -> None:
        if self.shares != 0 or self.amount != 0 or self.price_tier_used is not None:
            raise _fail("allocation", "a not-allocated candidate holds no quantity")
        if self.reason_code is ReasonCode.ALLOCATED:
            raise _fail("allocation.reason_code", "a not-allocated candidate has another reason")
        if (self.rank is None) != (self.reason_code in _UNRANKED_REASONS):
            raise _fail("allocation.rank", "is held exactly by the candidates that were ranked")
        short_for_cash = self.reason_code is ReasonCode.INSUFFICIENT_CASH_REMAINING
        if short_for_cash:
            if self.cash_shortfall <= 0 or self.cap_limited_shares <= 0:
                raise _fail("allocation.cash_shortfall", "the cash reason needs an unmet demand")
        elif self.cash_shortfall != 0:
            raise _fail("allocation.cash_shortfall", "only the cash reason has a shortfall")


def _order_key(item: CandidateAllocation) -> tuple[int, int, str]:
    return (0 if item.rank is not None else 1, item.rank or 0, item.stock_code)


@dataclass(frozen=True)
class AllocationResult:
    owner: str
    status: AllocationStatus
    unavailable_reason: UnavailableReason | None
    allocations: tuple[CandidateAllocation, ...]
    total_allocated: Decimal
    remaining_cash: Decimal | None
    policy_missing: tuple[str, ...]
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self) -> None:
        _text("result.owner", self.owner)
        _enum("result.status", self.status, AllocationStatus)
        if self.unavailable_reason is not None:
            _enum("result.unavailable_reason", self.unavailable_reason, UnavailableReason)
        if self.contract_version != CONTRACT_VERSION:
            raise _fail("result.contract_version", "is fixed")
        self._validate_collections()
        _non_negative("result.total_allocated", self.total_allocated)
        if self.remaining_cash is not None:
            _non_negative("result.remaining_cash", self.remaining_cash)
        allocated = [a for a in self.allocations if a.decision is CandidateDecision.ALLOCATED]
        if self.total_allocated != sum((a.amount for a in allocated), Decimal(0)):
            raise _fail("result.total_allocated", "must equal the allocated amounts")
        if self.status is AllocationStatus.UNAVAILABLE:
            self._validate_unavailable(allocated)
            return
        if self.unavailable_reason is not None or self.policy_missing:
            raise _fail("result", "an available result has no unavailable reason or missing item")
        if self.remaining_cash is None:
            raise _fail("result.remaining_cash", "an available result knows its remainder")
        if any(a.reason_code is ReasonCode.POLICY_INCOMPLETE for a in self.allocations):
            # 方針が不完全な日は UNAVAILABLE(順位だけの shadow)。完全な日の候補は別の理由を持つ
            raise _fail("result.allocations", "an available result has no incomplete-policy reason")
        if (self.status is AllocationStatus.ALLOCATED_SOME) != bool(allocated):
            raise _fail("result.status", "must agree with the allocated candidates")

    def _validate_collections(self) -> None:
        if not isinstance(self.allocations, tuple):
            raise _fail("result.allocations", "must be a tuple")
        if any(not isinstance(a, CandidateAllocation) for a in self.allocations):
            raise _fail("result.allocations", "has an item of the wrong type")
        if not isinstance(self.policy_missing, tuple):
            raise _fail("result.policy_missing", "must be a tuple")
        if any(item not in POLICY_ITEMS for item in self.policy_missing):
            raise _fail("result.policy_missing", "has a name outside the vocabulary")
        codes = [a.stock_code for a in self.allocations]
        if len(set(codes)) != len(codes):
            raise _fail("result.allocations", "must not repeat a stock")
        if list(self.allocations) != sorted(self.allocations, key=_order_key):
            raise _fail("result.allocations", "must be in the fixed order")
        ranks = [a.rank for a in self.allocations if a.rank is not None]
        if ranks != list(range(1, len(ranks) + 1)):
            raise _fail("result.allocations", "ranks run from one without gaps")

    def _validate_unavailable(self, allocated: list[CandidateAllocation]) -> None:
        reason = self.unavailable_reason
        if reason is None:
            raise _fail("result.unavailable_reason", "an unavailable result needs a reason")
        if allocated or self.total_allocated != 0 or self.remaining_cash is not None:
            raise _fail("result", "an unavailable result holds no allocation and no remainder")
        if reason is UnavailableReason.RAER_NOT_COMPARABLE:
            # 診断行(順位なし・配分なし)だけを持てる。★ policy_missing は要求しない ★
            # (比較できない日は、方針が完全でも起こる)
            if any(a.reason_code not in _NOT_COMPARABLE_REASONS for a in self.allocations):
                raise _fail("result.allocations", "a not-comparable day holds only its own reasons")
            return
        if reason is not UnavailableReason.POLICY_INCOMPLETE:
            if self.allocations:
                raise _fail("result.allocations", "only an incomplete policy keeps a shadow")
            return
        if not self.policy_missing:
            raise _fail("result.policy_missing", "an incomplete policy names what is missing")
        if any(a.reason_code not in _SHADOW_REASONS for a in self.allocations):
            raise _fail("result.allocations", "a shadow holds only its own reasons")
