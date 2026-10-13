"""購入側 Shadow の入力の組み立て(Issue #603 PR-F1。純粋関数)。

## 位置づけ

読み取った記録(推奨・保有・買付余力)から、PR-C の `optimize_allocation` が受け取る入力
(`AllocationCandidate` の列・`portfolio_value`・`sector_exposure`・`high_risk_exposure`・
`available_cash`)を作る。除外・欠測・既定値の適用は、件数と理由の `AssemblyReport` に残す。

**I/O・時計・ログ・AWS・services / infrastructure に触れない**(`now` は引数)。どこからも
import されない(dormant)。読取(PR-F2)・RAER の算出(#602)・配分の計算(PR-C)・記録(PR-F2)・
方針値(PR-E)は含まない。

## 本流(既存の `PortfolioExposureAggregate`)との違い

本流は時価の可用性と業種の可用性を独立した 2 軸にし、業種が 1 件不明でも銘柄集中度は評価する
(Issue #82)。配分側は承認済みの PR-C の契約に従い、業種上限が効くとき `sector_exposure` が
None なら日全体が UNAVAILABLE になる。これは逸脱ではなく、配分が『その日の結論』を出すための
承認済みの規則である。『業種不明による UNAVAILABLE の日数』は `AssemblyReport` で後から数えられる。

## 欠測の扱い(承認資料 B のとおり。中立値で補完しない)

* 価格・業種・買付余力・RAER・リスクの印が欠けたとき、0・直近値・既定の業種で埋めない。
  除外するか、None(= PR-C の UNAVAILABLE / SECTOR_UNKNOWN / RAER_UNAVAILABLE)にする
* `high_risk_exposure` は、全保有のうち high-risk の語(`constraints.high_risk_flags`)の観測が
  できているときだけ数値にする。1 件でも観測できない(語が未観測・推奨なし)保有があれば None
  (= high-risk の上限が効くので PR-C は PORTFOLIO_VALUE_UNAVAILABLE)。plan A(未観測を
  high-risk として数える)は承認された仕様ではないため採らない
* 業種の不明は `BuyIndustrySector.UNKNOWN` の 1 表現だけ(`sector_known` の別の真偽値を持たない)

## 段階(price tier)

BUY 系の買い判定から 1 対 1 で決まる(STRONG_BUY = STRONG / BUY = STANDARD /
SMALL_ENTRY = ENTRY。`buy_decision.py` の `_price_tier_action` と同じ対応)。対応は BUY 系の全てを
覆うため、判断 B の (b)『段階が不明のときは ENTRY』を適用する場面は F1 には無い(契約テストが
`BUY_FAMILY_ACTIONS` の全数を覆うことを固定し、BUY 系が増えたら落ちる)。

## 個人情報

owner は引数の文字列で、記録・ログ・例外のメッセージに出さない。保有の銘柄別の株数・取得単価・
損益は入力に持たない(評価額の計算にだけ株数を使い、出力に残さない)。例外のメッセージは
項目名と固定の問題名のみ(値を含めない。#909 の教訓)。例外の構築は `_fail` の 1 箇所だけ。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from jstock_advisor.domain.entities.enums import BUY_FAMILY_ACTIONS, BuyAction, BuyIndustrySector
from jstock_advisor.domain.jst import evaluation_date_jst, to_jst
from jstock_advisor.domain.signals.allocation_policy_config import AllocationPolicyBundle
from jstock_advisor.domain.signals.portfolio_allocation import optimize_allocation
from jstock_advisor.domain.signals.portfolio_allocation_types import (
    AllocationCandidate,
    AllocationResult,
    CandidateKind,
    PriceTier,
)
from jstock_advisor.domain.valuation.raer import RaerResult

CONTRACT_VERSION: Final = "F603-v1"

#: BUY 系の買い判定 -> 価格帯の段階(buy_decision.py の `_price_tier_action` と同じ対応)
_TIER_OF_ACTION: Final[Mapping[BuyAction, PriceTier]] = MappingProxyType(
    {
        BuyAction.STRONG_BUY: PriceTier.STRONG,
        BuyAction.BUY: PriceTier.STANDARD,
        BuyAction.SMALL_ENTRY: PriceTier.ENTRY,
    }
)


class AllocationInputsError(ValueError):
    """入力の矛盾(型違反・owner の混在・保有の重複など)。メッセージに値を含めない。"""


def _fail(name: str, problem: str) -> AllocationInputsError:
    return AllocationInputsError(f"{name}: {problem}")


class CandidateExclusionReason(StrEnum):
    """候補を配分の対象から除外した理由(件数で残す)。"""

    DUPLICATE_STOCK_CODE = "DUPLICATE_STOCK_CODE"
    ACTION_MISSING = "ACTION_MISSING"
    NOT_BUY_FAMILY = "NOT_BUY_FAMILY"
    PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"
    TRADING_UNIT_MISMATCH = "TRADING_UNIT_MISMATCH"
    HOLDING_VALUE_UNAVAILABLE = "HOLDING_VALUE_UNAVAILABLE"


class IncompleteReason(StrEnum):
    """入力が完全でなく、配分が算出不能になりうる理由(記録する)。"""

    CASH_NOT_REGISTERED = "CASH_NOT_REGISTERED"
    NO_HOLDINGS = "NO_HOLDINGS"
    HOLDING_PRICE_MISSING = "HOLDING_PRICE_MISSING"
    HOLDING_SECTOR_UNKNOWN = "HOLDING_SECTOR_UNKNOWN"
    HOLDING_RISK_UNKNOWN = "HOLDING_RISK_UNKNOWN"


class PortfolioValueBasis(StrEnum):
    COMPLETE = "COMPLETE"
    NO_HOLDINGS = "NO_HOLDINGS"
    PRICE_MISSING = "PRICE_MISSING"


_INCOMPLETE_ORDER: Final = tuple(IncompleteReason)


# --- 入力の型 ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateRecord:
    """購入候補 1 件(呼び出し側 = PR-F2 が推奨から写す)。RAER は呼び出し側が渡す。"""

    stock_code: str
    buy_action: BuyAction | None
    current_price: Decimal | None
    sector: BuyIndustrySector
    trading_unit: int | None
    raer: RaerResult

    def __post_init__(self) -> None:
        _text("candidate.stock_code", self.stock_code)
        if self.buy_action is not None and not isinstance(self.buy_action, BuyAction):
            raise _fail("candidate.buy_action", "has the wrong type")
        _optional_decimal("candidate.current_price", self.current_price)
        if not isinstance(self.sector, BuyIndustrySector):
            raise _fail("candidate.sector", "must be a BuyIndustrySector")
        if self.trading_unit is not None:
            _int("candidate.trading_unit", self.trading_unit, minimum=1)
        if not isinstance(self.raer, RaerResult):
            raise _fail("candidate.raer", "has the wrong type")


@dataclass(frozen=True)
class RiskObserved:
    """保有の銘柄の RiskFlag の観測結果。

    flags = 観測できて該当した語 / unobserved = 観測できなかった語(『リスクなし』とは別)。
    """

    flags: frozenset[str]
    unobserved: frozenset[str]

    def __post_init__(self) -> None:
        for name, value in (("risk.flags", self.flags), ("risk.unobserved", self.unobserved)):
            if not isinstance(value, frozenset) or any(not isinstance(v, str) for v in value):
                raise _fail(name, "must be a frozenset of str")
        if self.flags & self.unobserved:
            raise _fail("risk", "a word cannot be both flagged and unobserved")


@dataclass(frozen=True)
class RiskNotRecorded:
    """推奨が無い保有(解析の失敗・除外)。印を観測できない。"""


HoldingRisk = RiskObserved | RiskNotRecorded


@dataclass(frozen=True)
class HoldingRecord:
    """owner の保有 1 銘柄。株数は評価額の計算にだけ使い、出力に残さない。"""

    owner: str
    stock_code: str
    shares: int
    current_price: Decimal | None
    sector: BuyIndustrySector
    risk: HoldingRisk

    def __post_init__(self) -> None:
        _text("holding.owner", self.owner)
        _text("holding.stock_code", self.stock_code)
        _int("holding.shares", self.shares, minimum=1)
        _optional_decimal("holding.current_price", self.current_price)
        if not isinstance(self.sector, BuyIndustrySector):
            raise _fail("holding.sector", "must be a BuyIndustrySector")
        if not isinstance(self.risk, RiskObserved | RiskNotRecorded):
            raise _fail("holding.risk", "has the wrong type")


@dataclass(frozen=True)
class CashInput:
    """買付余力。amount が None は『未登録』(0 円ではない)。"""

    amount: Decimal | None
    last_reconciled_at: dt.datetime | None

    def __post_init__(self) -> None:
        if self.amount is not None:
            number = _optional_decimal("cash.amount", self.amount)
            if number is not None and number < 0:
                raise _fail("cash.amount", "must not be negative")
        if self.last_reconciled_at is not None:
            if not isinstance(self.last_reconciled_at, dt.datetime):
                raise _fail("cash.last_reconciled_at", "must be a datetime")
            _aware("cash.last_reconciled_at", self.last_reconciled_at)


# --- 出力の型 ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class AssemblyReport:
    """組み立ての報告。件数と理由だけ(金額・株数・銘柄コード・owner を含めない)。"""

    candidate_records: int
    candidates: int
    exclusions: Mapping[CandidateExclusionReason, int]
    holdings: int
    holdings_price_missing: int
    holdings_sector_unknown: int
    holdings_risk_unknown: int
    owner_holds_candidate: int
    portfolio_value_basis: PortfolioValueBasis
    cash_age_days: int | None
    incomplete_reasons: tuple[IncompleteReason, ...]


@dataclass(frozen=True)
class AllocationInputs:
    """PR-C の `optimize_allocation` へ渡す入力一式。"""

    owner: str
    available_cash: Decimal | None
    portfolio_value: Decimal | None
    sector_exposure: Mapping[str, Decimal] | None
    high_risk_exposure: Decimal | None
    candidates: tuple[AllocationCandidate, ...]
    report: AssemblyReport

    def optimize(self, bundle: AllocationPolicyBundle) -> AllocationResult:
        """`optimize_allocation` へ渡すだけ(引数の取り違えを、この 1 箇所に閉じ込める)。"""
        return optimize_allocation(
            owner=self.owner,
            available_cash=self.available_cash,
            portfolio_value=self.portfolio_value,
            candidates=self.candidates,
            constraints=bundle.constraints,
            policy=bundle.policy,
            sector_exposure=self.sector_exposure,
            high_risk_exposure=self.high_risk_exposure,
        )


# --- 検証の補助 -------------------------------------------------------------------------------


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(name, "must be a non-empty str")
    return value


def _int(name: str, value: object, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(name, "must be an int")
    if value < minimum:
        raise _fail(name, "is below its minimum")
    return value


def _optional_decimal(name: str, value: object) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal):
        raise _fail(name, "must be a Decimal")
    if not value.is_finite():
        raise _fail(name, "must be finite")
    return value


def _aware(name: str, value: object) -> dt.datetime:
    """timezone-aware な日時だけを受ける(naive を暗黙に UTC・JST 扱いしない)。"""
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _fail(name, "must be a timezone-aware datetime")
    return value


def _usable_price(price: Decimal | None) -> Decimal | None:
    """価格は正のときだけ使う。0 以下・欠測は『価格が無い』(0 や直近値で補完しない)。"""
    return price if price is not None and price > 0 else None


# --- 本体 -------------------------------------------------------------------------------------


def _high_risk_observed(risk: HoldingRisk, high_risk_flags: frozenset[str]) -> bool:
    """high-risk の語の全てを観測できているか。推奨なし・語が未観測なら False。"""
    if isinstance(risk, RiskNotRecorded):
        return False
    return not (risk.unobserved & high_risk_flags)


def _cash_age_days(now: dt.datetime, last_reconciled_at: dt.datetime | None) -> int | None:
    """最終棚卸しからの経過日数(JST の暦日の差)。未棚卸し・未来の時刻は None。

    記録するだけで止めない(古いときは参考扱い。USER 決定)。
    """
    if last_reconciled_at is None:
        return None
    days = (evaluation_date_jst(now) - to_jst(last_reconciled_at).date()).days
    return days if days >= 0 else None


def _validate_holdings(owner: str, holdings: Sequence[HoldingRecord]) -> None:
    seen: set[str] = set()
    for holding in holdings:
        if not isinstance(holding, HoldingRecord):
            raise _fail("holdings", "has the wrong type")
        if holding.owner != owner:
            raise _fail("holdings", "mixes owners")
        if holding.stock_code in seen:
            raise _fail("holdings", "repeats a stock")
        seen.add(holding.stock_code)


def assemble_allocation_inputs(
    *,
    owner: str,
    now: dt.datetime,
    candidates: Sequence[CandidateRecord],
    holdings: Sequence[HoldingRecord],
    cash: CashInput,
    bundle: AllocationPolicyBundle,
) -> AllocationInputs:
    """owner 1 人分の入力を組み立てる(純粋関数。同じ入力は同じ出力。入力の並びに依らない)。

    owner の混在・保有の重複・型違反は `AllocationInputsError`(値を含めない)。それ以外の
    欠測は例外にせず、除外・None・理由として `AssemblyReport` に残す。
    """
    _text("owner", owner)
    _aware("now", now)
    if not isinstance(cash, CashInput):
        raise _fail("cash", "has the wrong type")
    if not isinstance(bundle, AllocationPolicyBundle):
        raise _fail("bundle", "has the wrong type")
    if any(not isinstance(c, CandidateRecord) for c in candidates):
        raise _fail("candidates", "has the wrong type")
    _validate_holdings(owner, holdings)

    constraints = bundle.constraints
    high_risk_flags = constraints.high_risk_flags

    # --- 保有 -> 評価額・業種別・high-risk ---
    held_value: dict[str, Decimal] = {}
    price_missing = sum(1 for h in holdings if _usable_price(h.current_price) is None)
    sector_unknown = sum(1 for h in holdings if h.sector is BuyIndustrySector.UNKNOWN)
    risk_unknown = sum(1 for h in holdings if not _high_risk_observed(h.risk, high_risk_flags))
    for holding in holdings:
        price = _usable_price(holding.current_price)
        if price is not None:
            held_value[holding.stock_code] = price * holding.shares

    portfolio_value: Decimal | None
    sector_exposure: Mapping[str, Decimal] | None
    high_risk_exposure: Decimal | None
    if not holdings:
        basis = PortfolioValueBasis.NO_HOLDINGS
        portfolio_value = Decimal(0)
        sector_exposure = MappingProxyType({})
        high_risk_exposure = Decimal(0)
    elif price_missing:
        basis = PortfolioValueBasis.PRICE_MISSING
        portfolio_value = None
        sector_exposure = None
        high_risk_exposure = None
    else:
        basis = PortfolioValueBasis.COMPLETE
        portfolio_value = sum(held_value.values(), Decimal(0))
        if sector_unknown:
            sector_exposure = None
        else:
            by_sector: dict[str, Decimal] = {}
            for holding in holdings:
                key = holding.sector.value
                by_sector[key] = by_sector.get(key, Decimal(0)) + held_value[holding.stock_code]
            sector_exposure = MappingProxyType(by_sector)
        if risk_unknown:
            high_risk_exposure = None
        else:
            high_risk_exposure = sum(
                (
                    held_value[h.stock_code]
                    for h in holdings
                    if isinstance(h.risk, RiskObserved) and (h.risk.flags & high_risk_flags)
                ),
                Decimal(0),
            )

    # --- 候補 ---
    exclusions: dict[CandidateExclusionReason, int] = {}

    def exclude(reason: CandidateExclusionReason) -> None:
        exclusions[reason] = exclusions.get(reason, 0) + 1

    code_counts: dict[str, int] = {}
    for record in candidates:
        code_counts[record.stock_code] = code_counts.get(record.stock_code, 0) + 1
    held_codes = {h.stock_code for h in holdings}

    assembled: list[AllocationCandidate] = []
    owner_holds_candidate = 0
    for record in sorted(candidates, key=lambda c: c.stock_code):
        if code_counts[record.stock_code] > 1:
            exclude(CandidateExclusionReason.DUPLICATE_STOCK_CODE)
            continue
        if record.buy_action is None:
            exclude(CandidateExclusionReason.ACTION_MISSING)
            continue
        if record.buy_action not in BUY_FAMILY_ACTIONS:
            exclude(CandidateExclusionReason.NOT_BUY_FAMILY)
            continue
        price = _usable_price(record.current_price)
        if price is None:
            exclude(CandidateExclusionReason.PRICE_UNAVAILABLE)
            continue
        if (
            record.trading_unit is not None
            and record.trading_unit != constraints.minimum_trading_unit
        ):
            exclude(CandidateExclusionReason.TRADING_UNIT_MISMATCH)
            continue
        if record.stock_code not in held_codes:
            kind, position_value = CandidateKind.NEW_BUY, Decimal(0)
        elif record.stock_code in held_value:
            kind, position_value = CandidateKind.ADDITIONAL_BUY, held_value[record.stock_code]
        else:
            exclude(CandidateExclusionReason.HOLDING_VALUE_UNAVAILABLE)
            continue
        if kind is CandidateKind.ADDITIONAL_BUY:
            owner_holds_candidate += 1
        assembled.append(
            AllocationCandidate(
                stock_code=record.stock_code,
                kind=kind,
                buy_action=record.buy_action,
                current_price=price,
                price_as_of=None,
                price_tier=_TIER_OF_ACTION[record.buy_action],
                raer=record.raer,
                current_position_value=position_value,
                sector=(
                    None if record.sector is BuyIndustrySector.UNKNOWN else record.sector.value
                ),
            )
        )

    # --- 買付余力・理由 ---
    reasons = {
        IncompleteReason.CASH_NOT_REGISTERED: cash.amount is None,
        IncompleteReason.NO_HOLDINGS: basis is PortfolioValueBasis.NO_HOLDINGS,
        IncompleteReason.HOLDING_PRICE_MISSING: price_missing > 0,
        IncompleteReason.HOLDING_SECTOR_UNKNOWN: sector_unknown > 0,
        IncompleteReason.HOLDING_RISK_UNKNOWN: risk_unknown > 0,
    }
    report = AssemblyReport(
        candidate_records=len(candidates),
        candidates=len(assembled),
        exclusions=MappingProxyType(dict(sorted(exclusions.items()))),
        holdings=len(holdings),
        holdings_price_missing=price_missing,
        holdings_sector_unknown=sector_unknown,
        holdings_risk_unknown=risk_unknown,
        owner_holds_candidate=owner_holds_candidate,
        portfolio_value_basis=basis,
        cash_age_days=_cash_age_days(now, cash.last_reconciled_at),
        incomplete_reasons=tuple(r for r in _INCOMPLETE_ORDER if reasons[r]),
    )
    return AllocationInputs(
        owner=owner,
        available_cash=cash.amount,
        portfolio_value=portfolio_value,
        sector_exposure=sector_exposure,
        high_risk_exposure=high_risk_exposure,
        candidates=tuple(assembled),
        report=report,
    )
