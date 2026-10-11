"""Issue #603 / PR-D: Capital Rotation(資金入替)の純粋関数 `evaluate_capital_rotation`(C604-v1.1)。

資金が足りず買えなかった最良の買い候補(PR-C の AllocationResult で『資金だけが理由で買えなかった
候補』)と、企業に重大な問題がない保有(入替元)について、『保有の RAER より、買えなかった候補の
RAER が十分に高い』ときだけ、SELL と BUY を**一体の 1 提案**として評価する。どこからも import
されない(dormant)。時計・I/O・永続化・ログに触れない。現行の SELL / 利確 / 下落保護 / Arbiter の
判定は変更しない。

最低改善幅・算入割合・信頼度の下限は決めない(None = 未決定 = SHADOW_ONLY で、提案を出さない)。
独立定期実行(#604 の P)・専用 YAML(PR-E)・入力の組み立てと shadow 記録(PR-F)・配線は含まない。

## 契約

```
・提案は SELL と BUY の対だけ。SELL なしの BUY は出ない。入替元は eligibility = ELIGIBLE の保有
  だけで、UNKNOWN / BLOCKED_BY_EXIT は入替元にしない(fail-closed)。今日の買い候補は入替元にしない
・★ 買付余力の不足だけでは SELL を強めない ★ 入替元の選定は RAER の差(改善幅 >= 最低改善幅)と
  適格性が先で、資金は『その組で購入が 1 単元以上成立するか』にだけ使う。改善幅が足りない組は、
  資金がどれだけ少なくても提案しない
・★ 未約定の売却代金を確定資金として扱わない ★ 購入資金へ算入するのは売却見込額の算入割合
  (0 < r < 1)の分だけ。購入総額 <= 残資金 + 算入額。見込額 = 現在価格 x 売却株数。税・手数料は
  推定しない(cost_model = NONE)
・方針(最低改善幅・算入割合・信頼度の下限)が 1 つでも None なら SHADOW_ONLY: 評価の統計(評価した
  組の数・最良の改善幅)と対象ごとの理由は残すが、提案は出さない
・購入株数は PR-C の optimize_allocation を『売却後の状態・購入候補 1 件』で呼んで決める(再判定)。
  売却で総額・業種・high-risk の保有が変わるため、売却前の cap_limited_shares をそのまま使わない。
  購入は、売却前の需要(cap_limited_shares)を超えない
```

## 提案の組み方(決定的)

```
・対象 = allocation で NOT_ALLOCATED かつ INSUFFICIENT_CASH_REMAINING の候補(rank 順)
・対象ごとに、入替元を 適格 -> RAER が使える -> 比較可能(鍵が配分の方針と同じ)-> 改善幅 >=
  最低改善幅 -> 信頼度(両側)>= 下限 -> 未使用 の順に絞り、『改善幅の降順 -> stock_code 昇順』に
  見て、購入が 1 単元以上成立する最初の入替元を選ぶ。売却株数は、購入株数(最大)を満たす最小
  (入替元の売買単位の倍数)
・同じ回の提案は、残資金 + 算入額 - 購入額 を次の提案の残資金にする。入替元・対象とも 1 提案に
  1 回だけ
```
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Final, cast

from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.exit_architecture.vocabulary import ExitClass
from jstock_advisor.domain.signals.portfolio_allocation import (
    confidence_rank,
    optimize_allocation,
)
from jstock_advisor.domain.signals.portfolio_allocation_types import (
    AllocationCandidate,
    AllocationConstraints,
    AllocationPolicy,
    AllocationResult,
    AllocationStatus,
    CandidateDecision,
    PriceTier,
    ReasonCode,
)
from jstock_advisor.domain.valuation.raer import RaerResult, RaerState

CONTRACT_VERSION: Final = "C604-v1.1"
#: MVP は税・手数料を推定しない
COST_MODEL: Final = "NONE"
#: 回転の方針の項目名の語彙(`RotationPolicy.missing()` が返してよい名前)。この並びで返す
ROTATION_POLICY_ITEMS: Final = (
    "min_raer_improvement",
    "proceeds_inclusion_ratio",
    "min_confidence",
)
#: 金額の厳密な計算に使う精度(通常の context の桁数で丸めない)
_EXACT_PRECISION: Final = 400


class RotationContractError(ValueError):
    """資金入替の型の不変条件・入力の契約に反する構築。メッセージに値を含めない。"""


def _fail(name: str, problem: str) -> RotationContractError:
    return RotationContractError(f"{name}: {problem}")


# --- 列挙 -------------------------------------------------------------------------------------


class SourceEligibility(StrEnum):
    """入替元としての適格(『企業に重大な問題がない』保有か。導出は入力の組み立て役)."""

    ELIGIBLE = "ELIGIBLE"
    BLOCKED_BY_EXIT = "BLOCKED_BY_EXIT"
    UNKNOWN = "UNKNOWN"


class RotationStatus(StrEnum):
    ROTATION_PROPOSED = "ROTATION_PROPOSED"
    NO_ROTATION = "NO_ROTATION"
    SHADOW_ONLY = "SHADOW_ONLY"
    UNAVAILABLE = "UNAVAILABLE"


class RotationUnavailableReason(StrEnum):
    ALLOCATION_UNAVAILABLE = "ALLOCATION_UNAVAILABLE"
    PORTFOLIO_VALUE_UNAVAILABLE = "PORTFOLIO_VALUE_UNAVAILABLE"


class SellQuantityKind(StrEnum):
    PARTIAL = "PARTIAL"
    FULL = "FULL"


class TargetReason(StrEnum):
    """対象(資金だけが理由で買えなかった候補)ごとの結果。提案しなかった理由を必ず残す."""

    PROPOSED = "PROPOSED"
    POLICY_INCOMPLETE = "POLICY_INCOMPLETE"
    NO_ELIGIBLE_SOURCE = "NO_ELIGIBLE_SOURCE"
    SOURCE_RAER_UNAVAILABLE = "SOURCE_RAER_UNAVAILABLE"
    RAER_NOT_COMPARABLE = "RAER_NOT_COMPARABLE"
    INSUFFICIENT_IMPROVEMENT = "INSUFFICIENT_IMPROVEMENT"
    CONFIDENCE_BELOW_MINIMUM = "CONFIDENCE_BELOW_MINIMUM"
    SOURCE_ALREADY_USED = "SOURCE_ALREADY_USED"
    CANNOT_FUND_PURCHASE = "CANNOT_FUND_PURCHASE"


# --- 検証の補助 -------------------------------------------------------------------------------


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(name, "must be a non-empty str")
    return value


def _decimal(name: str, value: object) -> Decimal:
    # bool は int のサブクラス、float・int は Decimal ではない。金額に float を混ぜない
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


def _usable(raer: RaerResult) -> bool:
    # AVAILABLE / PROVISIONAL は必ず有限の value を持つ(RaerResult の不変条件)
    return raer.state is not RaerState.UNAVAILABLE


# --- 入力: 入替元・方針 -----------------------------------------------------------------------


@dataclass(frozen=True)
class RotationSource:
    """入替元の候補(既存の保有 1 銘柄)。sector は保有の業種(不明は None)."""

    stock_code: str
    shares_held: int
    current_price: Decimal
    minimum_trading_unit: int
    raer: RaerResult
    eligibility: SourceEligibility
    sector: str | None

    def __post_init__(self) -> None:
        _text("source.stock_code", self.stock_code)
        _int("source.shares_held", self.shares_held, minimum=1)
        _positive("source.current_price", self.current_price)
        _int("source.minimum_trading_unit", self.minimum_trading_unit, minimum=1)
        if not isinstance(self.raer, RaerResult):
            raise _fail("source.raer", "has the wrong type")
        _enum("source.eligibility", self.eligibility, SourceEligibility)
        if self.sector is not None:
            _text("source.sector", self.sector)


@dataclass(frozen=True)
class RotationPolicy:
    """人が決める値(Shadow -> backtest -> human review の後)。None は未決定."""

    min_raer_improvement: Decimal | None
    proceeds_inclusion_ratio: Decimal | None
    min_confidence: ConfidenceLevel | None

    def __post_init__(self) -> None:
        if self.min_raer_improvement is not None:
            # 0 以下だと、改善しない入替(または売買の往復)を提案できてしまう
            _positive("policy.min_raer_improvement", self.min_raer_improvement)
        if self.proceeds_inclusion_ratio is not None:
            ratio = _decimal("policy.proceeds_inclusion_ratio", self.proceeds_inclusion_ratio)
            # 1 以上 = 未約定の売却見込額を 100% 確定資金として扱うこと(禁止)
            if not (Decimal(0) < ratio < Decimal(1)):
                raise _fail("policy.proceeds_inclusion_ratio", "must be in (0, 1)")
        if self.min_confidence is not None:
            _enum("policy.min_confidence", self.min_confidence, ConfidenceLevel)

    def missing(self) -> tuple[str, ...]:
        """未決定の項目名(ROTATION_POLICY_ITEMS の並び)。空でなければ SHADOW_ONLY."""
        missing: list[str] = []
        if self.min_raer_improvement is None:
            missing.append("min_raer_improvement")
        if self.proceeds_inclusion_ratio is None:
            missing.append("proceeds_inclusion_ratio")
        if self.min_confidence is None:
            missing.append("min_confidence")
        return tuple(missing)

    def is_complete(self) -> bool:
        return not self.missing()


# --- 出力: 提案・結果 -------------------------------------------------------------------------


@dataclass(frozen=True)
class SellLeg:
    stock_code: str
    sell_shares: int
    estimated_proceeds: Decimal
    counted_proceeds: Decimal
    exit_class: ExitClass
    quantity_kind: SellQuantityKind

    def __post_init__(self) -> None:
        _text("sell_leg.stock_code", self.stock_code)
        _int("sell_leg.sell_shares", self.sell_shares, minimum=1)
        estimated = _decimal("sell_leg.estimated_proceeds", self.estimated_proceeds)
        counted = _positive("sell_leg.counted_proceeds", self.counted_proceeds)
        if counted >= estimated:
            # 売却見込額の全額を確定資金として扱わない
            raise _fail("sell_leg.counted_proceeds", "is below the estimated proceeds")
        if self.exit_class is not ExitClass.CAPITAL_ROTATION:
            raise _fail("sell_leg.exit_class", "is the capital rotation class")
        _enum("sell_leg.quantity_kind", self.quantity_kind, SellQuantityKind)


@dataclass(frozen=True)
class BuyLeg:
    stock_code: str
    shares: int
    amount: Decimal
    price_tier_used: PriceTier | None

    def __post_init__(self) -> None:
        _text("buy_leg.stock_code", self.stock_code)
        _int("buy_leg.shares", self.shares, minimum=1)
        _positive("buy_leg.amount", self.amount)
        if self.price_tier_used is not None:
            _enum("buy_leg.price_tier_used", self.price_tier_used, PriceTier)


@dataclass(frozen=True)
class RotationProposal:
    """SELL と BUY の一体の提案(分解した独立の SELL / BUY にはしない)."""

    proposal_id: str
    sell_leg: SellLeg
    buy_leg: BuyLeg
    improvement: Decimal
    projected_remaining_cash: Decimal

    def __post_init__(self) -> None:
        _text("proposal.proposal_id", self.proposal_id)
        if not isinstance(self.sell_leg, SellLeg):
            raise _fail("proposal.sell_leg", "has the wrong type")
        if not isinstance(self.buy_leg, BuyLeg):
            raise _fail("proposal.buy_leg", "has the wrong type")
        if self.sell_leg.stock_code == self.buy_leg.stock_code:
            raise _fail("proposal", "does not sell and buy the same stock")
        _positive("proposal.improvement", self.improvement)
        _non_negative("proposal.projected_remaining_cash", self.projected_remaining_cash)


@dataclass(frozen=True)
class TargetEvaluation:
    stock_code: str
    reason_code: TargetReason
    best_improvement: Decimal | None

    def __post_init__(self) -> None:
        _text("target.stock_code", self.stock_code)
        _enum("target.reason_code", self.reason_code, TargetReason)
        if self.best_improvement is not None:
            _decimal("target.best_improvement", self.best_improvement)


@dataclass(frozen=True)
class RotationResult:
    owner: str
    status: RotationStatus
    unavailable_reason: RotationUnavailableReason | None
    proposals: tuple[RotationProposal, ...]
    targets: tuple[TargetEvaluation, ...]
    evaluated_pairs: int
    best_improvement: Decimal | None
    starting_cash: Decimal | None
    ending_cash: Decimal | None
    policy_missing: tuple[str, ...]
    cost_model: str = COST_MODEL
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self) -> None:
        _text("result.owner", self.owner)
        _enum("result.status", self.status, RotationStatus)
        if self.unavailable_reason is not None:
            _enum("result.unavailable_reason", self.unavailable_reason, RotationUnavailableReason)
        if self.contract_version != CONTRACT_VERSION:
            raise _fail("result.contract_version", "is fixed")
        if self.cost_model != COST_MODEL:
            raise _fail("result.cost_model", "is fixed")
        self._validate_collections()
        _int("result.evaluated_pairs", self.evaluated_pairs, minimum=0)
        if self.best_improvement is not None:
            _decimal("result.best_improvement", self.best_improvement)
        for name, cash in (
            ("starting_cash", self.starting_cash),
            ("ending_cash", self.ending_cash),
        ):
            if cash is not None:
                _non_negative(f"result.{name}", cash)
        if self.status is RotationStatus.UNAVAILABLE:
            self._validate_unavailable()
            return
        self._validate_available()

    def _validate_collections(self) -> None:
        if not isinstance(self.proposals, tuple) or any(
            not isinstance(p, RotationProposal) for p in self.proposals
        ):
            raise _fail("result.proposals", "must be a tuple of proposals")
        if not isinstance(self.targets, tuple) or any(
            not isinstance(t, TargetEvaluation) for t in self.targets
        ):
            raise _fail("result.targets", "must be a tuple of target evaluations")
        if not isinstance(self.policy_missing, tuple) or any(
            item not in ROTATION_POLICY_ITEMS for item in self.policy_missing
        ):
            raise _fail("result.policy_missing", "has a name outside the vocabulary")
        codes = [t.stock_code for t in self.targets]
        if len(set(codes)) != len(codes):
            raise _fail("result.targets", "must not repeat a stock")

    def _validate_unavailable(self) -> None:
        if self.unavailable_reason is None:
            raise _fail("result.unavailable_reason", "an unavailable result needs a reason")
        if self.proposals or self.targets:
            raise _fail("result", "an unavailable result holds no proposal and no target")
        if self.starting_cash is not None or self.ending_cash is not None:
            raise _fail("result", "an unavailable result knows no cash")
        if self.evaluated_pairs != 0 or self.best_improvement is not None:
            raise _fail("result", "an unavailable result evaluated nothing")

    def _validate_available(self) -> None:
        if self.unavailable_reason is not None:
            raise _fail("result.unavailable_reason", "an available result has no reason")
        if self.starting_cash is None or self.ending_cash is None:
            raise _fail("result", "an available result knows its cash")
        shadow = self.status is RotationStatus.SHADOW_ONLY
        if shadow != bool(self.policy_missing):
            raise _fail("result.policy_missing", "is held exactly by a shadow result")
        if (self.status is RotationStatus.ROTATION_PROPOSED) != bool(self.proposals):
            raise _fail("result.status", "must agree with the proposals")
        if (self.evaluated_pairs == 0) != (self.best_improvement is None):
            raise _fail("result.best_improvement", "is held exactly when pairs were evaluated")
        if self.evaluated_pairs < len(self.proposals):
            raise _fail("result.evaluated_pairs", "is below the number of proposals")
        incomplete = [t.reason_code is TargetReason.POLICY_INCOMPLETE for t in self.targets]
        if shadow and not all(incomplete):
            raise _fail("result.targets", "an incomplete policy holds only its own reason")
        if not shadow and any(incomplete):
            raise _fail("result.targets", "a complete policy has no incomplete-policy reason")
        self._validate_proposals(self.starting_cash, self.ending_cash)

    def _validate_proposals(self, starting_cash: Decimal, ending_cash: Decimal) -> None:
        sells = [p.sell_leg.stock_code for p in self.proposals]
        buys = [p.buy_leg.stock_code for p in self.proposals]
        if len(set(sells)) != len(sells) or len(set(buys)) != len(buys):
            raise _fail("result.proposals", "uses a stock once on each side")
        if set(sells) & set(buys):
            raise _fail("result.proposals", "does not sell and buy the same stock")
        proposed = {t.stock_code for t in self.targets if t.reason_code is TargetReason.PROPOSED}
        if proposed != set(buys):
            raise _fail("result.targets", "marks exactly the bought stocks as proposed")
        with localcontext() as context:
            context.prec = _EXACT_PRECISION
            running = starting_cash
            for proposal in self.proposals:
                expected = f"rotation:{self.owner}:{proposal.sell_leg.stock_code}:"
                if proposal.proposal_id != expected + proposal.buy_leg.stock_code:
                    raise _fail("result.proposals", "has an id that differs from its legs")
                running = running + proposal.sell_leg.counted_proceeds - proposal.buy_leg.amount
                if running != proposal.projected_remaining_cash:
                    # 購入総額 <= 残資金 + 算入額(提案ごとに持ち越す残資金が合う)
                    raise _fail("result.proposals", "has a remaining cash that does not follow")
        if ending_cash != running:
            raise _fail("result.ending_cash", "must follow the proposals")
        best = self.best_improvement
        if best is not None and any(p.improvement > best for p in self.proposals):
            raise _fail("result.best_improvement", "is below a proposal's improvement")


# --- 入力の検証 -------------------------------------------------------------------------------


def _optional_non_negative(name: str, value: object) -> Decimal | None:
    return None if value is None else _non_negative(name, value)


def _sequence_of[T](name: str, items: object, item_type: type[T]) -> tuple[T, ...]:
    if isinstance(items, str) or not isinstance(items, Sequence):
        raise _fail(name, "must be a sequence")
    if any(not isinstance(item, item_type) for item in items):
        raise _fail(name, "has an item of the wrong type")
    return tuple(items)


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


def _unique_codes(name: str, codes: Sequence[str]) -> None:
    if len(set(codes)) != len(codes):
        raise _fail(name, "must not repeat a stock")


def _in_force(ratio: Decimal | None) -> bool:
    """比率の上限が効いている(1 未満)。配分の方針が完全〔比率は決定済み〕と確かめた後に呼ぶ."""
    return cast(Decimal, ratio) < 1


def _is_high_risk(raer: RaerResult, constraints: AllocationConstraints) -> bool:
    """high-risk と数える銘柄か。観測がない銘柄は risk_flags が空(RaerResult の不変条件)で False."""
    return bool(raer.risk_flags & constraints.high_risk_flags)


def _unavailable(
    owner: str, reason: RotationUnavailableReason, missing: tuple[str, ...]
) -> RotationResult:
    return RotationResult(
        owner=owner,
        status=RotationStatus.UNAVAILABLE,
        unavailable_reason=reason,
        proposals=(),
        targets=(),
        evaluated_pairs=0,
        best_improvement=None,
        starting_cash=None,
        ending_cash=None,
        policy_missing=missing,
    )


# --- 公開関数 ---------------------------------------------------------------------------------


def evaluate_capital_rotation(
    *,
    owner: str,
    allocation: AllocationResult,
    candidates: Sequence[AllocationCandidate],
    sources: Sequence[RotationSource],
    constraints: AllocationConstraints,
    allocation_policy: AllocationPolicy,
    policy: RotationPolicy,
    portfolio_value: Decimal | None = None,
    sector_exposure: Mapping[str, Decimal] | None = None,
    high_risk_exposure: Decimal | None = None,
) -> RotationResult:
    """資金だけが理由で買えなかった候補について、保有からの資金入替を評価する(純粋関数)。

    allocation          optimize_allocation の結果。残資金は allocation.remaining_cash を使う
    candidates          allocation に渡した候補と同じ銘柄の集合(業種・段階・RAER の参照に使う)
    sources             入替元の候補(保有)。eligibility = ELIGIBLE だけが入替元になる
    constraints         allocation と同じ制約
    allocation_policy   allocation と同じ配分の方針(完全であること)。鍵と段階の割合を使う
    policy              回転の方針。未決定(None)が 1 つでもあれば SHADOW_ONLY
    portfolio_value     owner 全体の保有評価額(入替元の保有を含み、買付余力を含まない)。比率の上限
    sector_exposure     (< 1)が効くときに必要。業種・high-risk の現在の保有評価額は、売却による
    high_risk_exposure  減少を認めて購入株数の再判定に使う(allocation に渡したものと同じ)

    入力の矛盾は RotationContractError(値を含めない)。それ以外は結果の status で表す。
    """
    _text("owner", owner)
    if not isinstance(allocation, AllocationResult):
        raise _fail("allocation", "has the wrong type")
    cands = _sequence_of("candidates", candidates, AllocationCandidate)
    _unique_codes("candidates", [c.stock_code for c in cands])
    held = _sequence_of("sources", sources, RotationSource)
    _unique_codes("sources", [s.stock_code for s in held])
    if not isinstance(constraints, AllocationConstraints):
        raise _fail("constraints", "has the wrong type")
    if not isinstance(allocation_policy, AllocationPolicy):
        raise _fail("allocation_policy", "has the wrong type")
    if not isinstance(policy, RotationPolicy):
        raise _fail("policy", "has the wrong type")
    total_value = _optional_non_negative("portfolio_value", portfolio_value)
    sectors = _validated_sector_exposure(sector_exposure)
    risk_exposure = _optional_non_negative("high_risk_exposure", high_risk_exposure)
    if allocation.owner != owner:
        raise _fail("owner", "must match the allocation")

    missing = policy.missing()
    if allocation.status is AllocationStatus.UNAVAILABLE:
        return _unavailable(owner, RotationUnavailableReason.ALLOCATION_UNAVAILABLE, missing)
    if {c.stock_code for c in cands} != {a.stock_code for a in allocation.allocations}:
        raise _fail("candidates", "must be the candidates of the allocation")
    if allocation_policy.missing(constraints):
        raise _fail("allocation_policy", "must be complete when the allocation is available")

    with localcontext() as context:
        context.prec = _EXACT_PRECISION
        return _evaluate(
            owner=owner,
            allocation=allocation,
            candidates={c.stock_code: c for c in cands},
            sources=held,
            constraints=constraints,
            allocation_policy=allocation_policy,
            policy=policy,
            portfolio_value=total_value,
            sector_exposure=sectors,
            high_risk_exposure=risk_exposure,
        )


# --- 評価の本体 -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rules:
    """回転の方針が完全(`missing()` が空)のときの、None を除いた決定値."""

    min_improvement: Decimal
    ratio: Decimal
    confidence_floor: int


def _rules(policy: RotationPolicy) -> _Rules:
    """`policy.missing()` が空だと確かめた後に呼ぶ(全項目が非 None)."""
    return _Rules(
        min_improvement=cast(Decimal, policy.min_raer_improvement),
        ratio=cast(Decimal, policy.proceeds_inclusion_ratio),
        confidence_floor=confidence_rank(cast(ConfidenceLevel, policy.min_confidence)),
    )


def _improvement(target: AllocationCandidate, source: RotationSource) -> Decimal:
    return cast(Decimal, target.raer.value) - cast(Decimal, source.raer.value)


def _screen(
    target: AllocationCandidate,
    sources: Sequence[RotationSource],
    candidate_codes: set[str],
    key: str,
) -> tuple[TargetReason | None, list[RotationSource]]:
    """評価の対象にできる入替元(適格 -> RAER が使える -> 比較可能)。空になった段階の理由を返す."""
    pool = [
        s
        for s in sources
        if s.eligibility is SourceEligibility.ELIGIBLE and s.stock_code not in candidate_codes
    ]
    if not pool:
        return TargetReason.NO_ELIGIBLE_SOURCE, []
    pool = [s for s in pool if _usable(s.raer)]
    if not pool:
        return TargetReason.SOURCE_RAER_UNAVAILABLE, []
    pool = [s for s in pool if s.raer.comparison_key == key]
    if not pool:
        return TargetReason.RAER_NOT_COMPARABLE, []
    return None, pool


@dataclass
class _State:
    """同じ回の提案の途中経過(先の提案が使った資金・変えた保有)."""

    cash: Decimal
    total: Decimal | None
    sector: dict[str, Decimal]
    high_risk: Decimal


def _relieved(value: Decimal, sold: Decimal) -> Decimal:
    return max(Decimal(0), value - sold)


def _buy_after_sale(
    *,
    owner: str,
    target: AllocationCandidate,
    demand: int,
    state: _State,
    source: RotationSource,
    sold_shares: int,
    ratio: Decimal,
    constraints: AllocationConstraints,
    allocation_policy: AllocationPolicy,
) -> int:
    """売却後の状態で、購入候補 1 件を optimize_allocation に再判定させた購入株数(需要の範囲)."""
    estimated = source.current_price * sold_shares
    sector = dict(state.sector)
    if source.sector is not None:
        sector[source.sector] = _relieved(sector.get(source.sector, Decimal(0)), estimated)
    high_risk = state.high_risk
    if _in_force(constraints.max_high_risk_ratio) and _is_high_risk(source.raer, constraints):
        high_risk = _relieved(high_risk, estimated)
    result = optimize_allocation(
        owner=owner,
        available_cash=state.cash + estimated * ratio,
        portfolio_value=None if state.total is None else _relieved(state.total, estimated),
        candidates=[target],
        constraints=constraints,
        policy=allocation_policy,
        sector_exposure=sector,
        high_risk_exposure=high_risk,
    )
    shares = sum(
        (a.shares for a in result.allocations if a.decision is CandidateDecision.ALLOCATED), 0
    )
    return min(shares, demand)


def _evaluate(
    *,
    owner: str,
    allocation: AllocationResult,
    candidates: Mapping[str, AllocationCandidate],
    sources: Sequence[RotationSource],
    constraints: AllocationConstraints,
    allocation_policy: AllocationPolicy,
    policy: RotationPolicy,
    portfolio_value: Decimal | None,
    sector_exposure: Mapping[str, Decimal] | None,
    high_risk_exposure: Decimal | None,
) -> RotationResult:
    key = cast(str, allocation_policy.comparison_key)
    cash = cast(Decimal, allocation.remaining_cash)
    targets = [
        a for a in allocation.allocations if a.reason_code is ReasonCode.INSUFFICIENT_CASH_REMAINING
    ]
    candidate_codes = set(candidates)
    screens = {
        t.stock_code: _screen(candidates[t.stock_code], sources, candidate_codes, key)
        for t in targets
    }
    # 評価の統計(方針が未決定でも残す): 比較可能な組の数・最良の改善幅
    improvements = {
        t.stock_code: [_improvement(candidates[t.stock_code], s) for s in screens[t.stock_code][1]]
        for t in targets
    }
    pairs = sum(len(v) for v in improvements.values())
    best_all = max((max(v) for v in improvements.values() if v), default=None)

    def best_of(code: str) -> Decimal | None:
        return max(improvements[code], default=None)

    missing = policy.missing()
    if missing:
        return RotationResult(
            owner=owner,
            status=RotationStatus.SHADOW_ONLY,
            unavailable_reason=None,
            proposals=(),
            targets=tuple(
                TargetEvaluation(
                    t.stock_code, TargetReason.POLICY_INCOMPLETE, best_of(t.stock_code)
                )
                for t in targets
            ),
            evaluated_pairs=pairs,
            best_improvement=best_all,
            starting_cash=cash,
            ending_cash=cash,
            policy_missing=missing,
        )

    ratio_in_force = [
        _in_force(constraints.max_single_stock_ratio),
        _in_force(constraints.max_sector_ratio),
        _in_force(constraints.max_high_risk_ratio),
    ]
    if targets and (
        (any(ratio_in_force) and (portfolio_value is None or portfolio_value <= 0))
        or (ratio_in_force[1] and sector_exposure is None)
        or (ratio_in_force[2] and high_risk_exposure is None)
    ):
        return _unavailable(owner, RotationUnavailableReason.PORTFOLIO_VALUE_UNAVAILABLE, ())

    rules = _rules(policy)
    state = _State(
        cash=cash,
        total=None if portfolio_value is None else portfolio_value + allocation.total_allocated,
        sector=dict(sector_exposure or {}),
        high_risk=high_risk_exposure or Decimal(0),
    )
    for item in allocation.allocations:
        if item.decision is not CandidateDecision.ALLOCATED:
            continue
        done = candidates[item.stock_code]
        if done.sector is not None:
            state.sector[done.sector] = state.sector.get(done.sector, Decimal(0)) + item.amount
        if _in_force(constraints.max_high_risk_ratio) and _is_high_risk(done.raer, constraints):
            state.high_risk += item.amount

    used: set[str] = set()
    proposals: list[RotationProposal] = []
    evaluations: list[TargetEvaluation] = []
    for t in targets:
        target = candidates[t.stock_code]
        proposal, reason = _propose(
            owner=owner,
            target=target,
            demand=t.cap_limited_shares,
            screen=screens[t.stock_code],
            rules=rules,
            used=used,
            state=state,
            constraints=constraints,
            allocation_policy=allocation_policy,
        )
        if proposal is not None:
            proposals.append(proposal)
        evaluations.append(TargetEvaluation(t.stock_code, reason, best_of(t.stock_code)))
    return RotationResult(
        owner=owner,
        status=RotationStatus.ROTATION_PROPOSED if proposals else RotationStatus.NO_ROTATION,
        unavailable_reason=None,
        proposals=tuple(proposals),
        targets=tuple(evaluations),
        evaluated_pairs=pairs,
        best_improvement=best_all,
        starting_cash=cash,
        ending_cash=state.cash,
        policy_missing=(),
    )


def _preference(target: AllocationCandidate, pool: list[RotationSource]) -> list[RotationSource]:
    """改善幅の降順 -> stock_code 昇順(Decimal を符号反転せず、安定ソートを後ろの鍵から掛ける)."""
    ordered = sorted(pool, key=lambda s: s.stock_code)
    ordered.sort(key=lambda s: _improvement(target, s), reverse=True)
    return ordered


def _propose(
    *,
    owner: str,
    target: AllocationCandidate,
    demand: int,
    screen: tuple[TargetReason | None, list[RotationSource]],
    rules: _Rules,
    used: set[str],
    state: _State,
    constraints: AllocationConstraints,
    allocation_policy: AllocationPolicy,
) -> tuple[RotationProposal | None, TargetReason]:
    """対象 1 件の提案(なければその理由)。提案したら state・used を更新する."""
    reason, pool = screen
    if reason is not None:
        return None, reason
    pool = [s for s in pool if _improvement(target, s) >= rules.min_improvement]
    if not pool:
        return None, TargetReason.INSUFFICIENT_IMPROVEMENT
    pool = [
        s
        for s in pool
        if min(confidence_rank(target.raer.confidence), confidence_rank(s.raer.confidence))
        >= rules.confidence_floor
    ]
    if not pool:
        return None, TargetReason.CONFIDENCE_BELOW_MINIMUM
    pool = [s for s in pool if s.stock_code not in used]
    if not pool:
        return None, TargetReason.SOURCE_ALREADY_USED
    for source in _preference(target, pool):
        best_shares, best_sold = 0, 0
        for step in range(1, source.shares_held // source.minimum_trading_unit + 1):
            sold = step * source.minimum_trading_unit
            shares = _buy_after_sale(
                owner=owner,
                target=target,
                demand=demand,
                state=state,
                source=source,
                sold_shares=sold,
                ratio=rules.ratio,
                constraints=constraints,
                allocation_policy=allocation_policy,
            )
            if shares > best_shares:
                best_shares, best_sold = shares, sold
                if best_shares == demand:
                    break  # これ以上は増えない(需要を満たした。売却は最小)
        if best_shares > 0:
            used.add(source.stock_code)
            return _commit(
                owner=owner,
                target=target,
                source=source,
                buy_shares=best_shares,
                sold_shares=best_sold,
                rules=rules,
                state=state,
                constraints=constraints,
            ), TargetReason.PROPOSED
    return None, TargetReason.CANNOT_FUND_PURCHASE


def _commit(
    *,
    owner: str,
    target: AllocationCandidate,
    source: RotationSource,
    buy_shares: int,
    sold_shares: int,
    rules: _Rules,
    state: _State,
    constraints: AllocationConstraints,
) -> RotationProposal:
    estimated = source.current_price * sold_shares
    counted = estimated * rules.ratio
    amount = target.current_price * buy_shares
    state.cash = state.cash + counted - amount
    if state.total is not None:
        state.total = _relieved(state.total + amount, estimated)
    if target.sector is not None:
        state.sector[target.sector] = state.sector.get(target.sector, Decimal(0)) + amount
    if source.sector is not None:
        state.sector[source.sector] = _relieved(
            state.sector.get(source.sector, Decimal(0)), estimated
        )
    if _in_force(constraints.max_high_risk_ratio):
        if _is_high_risk(target.raer, constraints):
            state.high_risk += amount
        if _is_high_risk(source.raer, constraints):
            state.high_risk = _relieved(state.high_risk, estimated)
    return RotationProposal(
        proposal_id=f"rotation:{owner}:{source.stock_code}:{target.stock_code}",
        sell_leg=SellLeg(
            stock_code=source.stock_code,
            sell_shares=sold_shares,
            estimated_proceeds=estimated,
            counted_proceeds=counted,
            exit_class=ExitClass.CAPITAL_ROTATION,
            quantity_kind=(
                SellQuantityKind.FULL
                if sold_shares == source.shares_held
                else SellQuantityKind.PARTIAL
            ),
        ),
        buy_leg=BuyLeg(
            stock_code=target.stock_code,
            shares=buy_shares,
            amount=amount,
            price_tier_used=target.price_tier,
        ),
        improvement=_improvement(target, source),
        projected_remaining_cash=state.cash,
    )
