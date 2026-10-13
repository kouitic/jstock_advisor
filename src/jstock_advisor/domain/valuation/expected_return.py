"""Expected Return の型と UPSIDE / INCOME の純粋計算(Issue #601 PR-1a。dormant)。

#128 の Expected Return は、新規 BUY 候補・追加購入候補・既存保有銘柄を**現在価格基準**で比べる
共通の尺度である。本 module は、その最初の部品(型・UPSIDE・INCOME・欠測の状態)だけを置く。
どこからも import されない(配線なし・保存なし・flag なし)。

性質(契約テストで固定する)
  ・現在価格基準: UPSIDE は現在価格と Fair Value(中立)だけから計算する。**取得価格・含み益は
    引数に持たない**(USER 決定 2026-10-08。#128 …6054669876 §5)
  ・値を捏造しない: 主要入力が欠測・不適格なら、0 や中立値で埋めず、値を作らずに理由コードを返す。
    理由は入力ごとに累積する
  ・INCOME は #55 の真理値表(`yield_calc.compute_total_yield_pct` の A〜H)をそのまま使う。
    配当の None から 0 を推測しない
  ・★ 確定しないもの(UNDETERMINED で表現する。USER 承認の範囲 #122 …6100082651)★
      composite(合成式が未決)・annualized(年率換算。到達期間を信頼できる形で推定できない間は
      行わない)・revision(Fair Value Revision。規則は PR-2)。重み・閾値は持たない
  ・Revision を UPSIDE に足し込まない設計の前提: Fair Value の変化は UPSIDE(現在の Fair Value と
    現在価格の差)に既に反映されている。足すと同じ事実を 2 回数える(二重計上)
  ・時計・営業日・timezone を読まない: evaluation_date は呼び出し側が渡す記録用の値
  ・available_cash を持たない(資金制約は Expected Return の外。#884 の PortfolioContext と同じ規約)

本 module に置かないもの
  ・ExpectedReturnVerdict(exit_architecture)への写像 = PR-1b(package の中に置く。C0 の契約テストが
    『package の外の src は exit_architecture を import しない』ことを固定しているため、本 module は
    exit_architecture を import しない)
  ・RAER(#602)・Fair Value Revision の規則(PR-2)・DecisionSnapshot / AuditLog への記録
    (shadow の配線)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.entities.valuation import FairValueUnusableReasonCode
from jstock_advisor.domain.price_freshness import PriceFreshnessVerdict
from jstock_advisor.domain.valuation.yield_calc import BenefitProgramState, compute_total_yield_pct

#: 式・規則の版。composite が決まる前の『構成要素のみ』の版であることを示す。
MODEL_VERSION = "er-components-v1"


class ReasonCode(StrEnum):
    """値を作れない理由(構成要素ごと)。値を捏造しないための記録。"""

    CURRENT_PRICE_UNAVAILABLE = "CURRENT_PRICE_UNAVAILABLE"  # None / 0 以下 / 非有限
    CURRENT_PRICE_STALE = "CURRENT_PRICE_STALE"  # 鮮度の判定が HARD_STOP / DATA_INSUFFICIENT
    FAIR_VALUE_UNAVAILABLE = "FAIR_VALUE_UNAVAILABLE"  # 中立値が None / 0 以下 / 非有限
    FAIR_VALUE_NOT_USABLE = "FAIR_VALUE_NOT_USABLE"  # usable_for_trading_judgment = False
    DIVIDEND_UNKNOWN = "DIVIDEND_UNKNOWN"  # 予想配当が不明(None から 0 を推測しない)
    BENEFIT_UNVALUABLE = "BENEFIT_UNVALUABLE"  # 優待の制度はあるが値付けできない
    REVISION_NOT_IMPLEMENTED = "REVISION_NOT_IMPLEMENTED"  # Fair Value Revision の規則は PR-2
    COMPOSITION_RULE_NOT_DECIDED = "COMPOSITION_RULE_NOT_DECIDED"  # composite の合成式が未決
    HORIZON_NOT_ESTIMATED = "HORIZON_NOT_ESTIMATED"  # 年率換算に要る到達期間を推定していない


class Note(StrEnum):
    """値の意味を限定する注記(値の有無には影響しない)。"""

    PRICE_FRESHNESS_WARNING = "PRICE_FRESHNESS_WARNING"  # 価格が 1 取引セッション前(判定は継続)
    BENEFIT_NO_PROGRAM_OR_UNREGISTERED = "BENEFIT_NO_PROGRAM_OR_UNREGISTERED"
    # 優待は『制度なし(未登録を含む)』として寄与 0 で確定している(既存の NO_PROGRAM 契約の再利用。
    # USER 決定 D-601-4)。**未登録を制度が存在しない確証にしない**: 登録は少数のため、非保有銘柄の
    # INCOME は配当のみの下限値になりうる。この注記が付いた INCOME は、その事実を区別して扱うこと


@dataclass(frozen=True)
class ComponentResult:
    """構成要素 1 つの結果。確定した値、または理由コード(値なし)のどちらか一方。"""

    value: float | None = None
    reasons: tuple[ReasonCode, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        if (self.value is None) == (not self.reasons):
            raise ValueError("確定した値か理由コードのどちらか一方だけを持つ")
        if self.value is not None and not math.isfinite(self.value):
            raise ValueError("確定した値は有限でなければならない")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("理由コードが重複している")

    @property
    def is_determined(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class FairValueInput:
    """Fair Value の入力。`FairValueRange` の必要な項目だけを受ける(取得は呼び出し側)。"""

    neutral: Decimal | None
    usable_for_trading_judgment: bool
    unusable_reason_code: FairValueUnusableReasonCode | None = None
    # Fair Value の信頼度。**記録するだけ**で、UPSIDE の値は変えない(信頼度による調整は
    # #602 の RAER)。USER 決定 D-601-5: usable なら信頼度 LOW でも値と信頼度を記録する
    confidence: ConfidenceLevel | None = None


@dataclass(frozen=True)
class IncomeInput:
    """総合利回りの入力(配当 + 優待)。`yield_calc` の関数が返す値をそのまま受ける。"""

    dividend_yield_pct: float | None
    benefit_yield_pct: float | None
    benefit_state: BenefitProgramState


@dataclass(frozen=True)
class ExpectedReturnResult:
    """Expected Return の結果(構成要素ごと)。composite は PR-1a では常に UNDETERMINED。"""

    model_version: str
    evaluation_date: date
    upside: ComponentResult
    income: ComponentResult
    revision: ComponentResult
    composite: ComponentResult
    annualized: ComponentResult
    notes: tuple[Note, ...] = ()
    fair_value_confidence: ConfidenceLevel | None = None

    @property
    def unavailable_reasons(self) -> tuple[ReasonCode, ...]:
        """主要な構成要素(UPSIDE・INCOME)の理由コード。重複を除き、出現順。"""
        seen: list[ReasonCode] = []
        for reason in (*self.upside.reasons, *self.income.reasons):
            if reason not in seen:
                seen.append(reason)
        return tuple(seen)


_STALE_VERDICTS = frozenset(
    {PriceFreshnessVerdict.HARD_STOP, PriceFreshnessVerdict.DATA_INSUFFICIENT}
)


def _usable_decimal(value: Decimal | None) -> bool:
    return value is not None and value.is_finite() and value > 0


def _usable_yield(value: float | None) -> float | None:
    """非有限・負の利回りは『不明』として扱う(None と同じ。0 や他の値へ置き換えない)。"""
    if value is None or not math.isfinite(value) or value < 0:
        return None
    return value


def compute_upside(
    *,
    current_price: Decimal | None,
    price_freshness: PriceFreshnessVerdict,
    fair_value: FairValueInput,
) -> ComponentResult:
    """UPSIDE = (Fair Value〔中立〕/ 現在価格 - 1) × 100(%)。現在価格基準。

    Fair Value が現在価格を下回れば負の値(切り捨てない。『売り』の根拠にはしない。使い方は利用側)。
    入力が欠測・不適格なら値を作らず、入力ごとの理由を累積して返す。
    """
    reasons: list[ReasonCode] = []
    detail = ""
    if not _usable_decimal(current_price):
        reasons.append(ReasonCode.CURRENT_PRICE_UNAVAILABLE)
    elif price_freshness in _STALE_VERDICTS:
        reasons.append(ReasonCode.CURRENT_PRICE_STALE)
    if not _usable_decimal(fair_value.neutral):
        reasons.append(ReasonCode.FAIR_VALUE_UNAVAILABLE)
    elif not fair_value.usable_for_trading_judgment:
        reasons.append(ReasonCode.FAIR_VALUE_NOT_USABLE)
        if fair_value.unusable_reason_code is not None:
            detail = f"fair_value_unusable_reason_code={fair_value.unusable_reason_code.value}"
    if reasons:
        return ComponentResult(reasons=tuple(reasons), detail=detail)
    assert current_price is not None and fair_value.neutral is not None  # for type narrowing
    pct = float((fair_value.neutral / current_price - Decimal(1)) * Decimal(100))
    if not math.isfinite(pct):
        return ComponentResult(
            reasons=(ReasonCode.FAIR_VALUE_UNAVAILABLE,), detail="non-finite upside"
        )
    return ComponentResult(value=pct)


def compute_income(income: IncomeInput) -> ComponentResult:
    """INCOME = 総合利回り(配当 + 優待)。#55 の真理値表(A〜H)に従い、不明なら値を作らない。"""
    dividend = _usable_yield(income.dividend_yield_pct)
    benefit = _usable_yield(income.benefit_yield_pct)
    total = compute_total_yield_pct(dividend, benefit, benefit_state=income.benefit_state)
    if total is not None:
        return ComponentResult(value=float(total))
    reasons: list[ReasonCode] = []
    if dividend is None:
        reasons.append(ReasonCode.DIVIDEND_UNKNOWN)
    if income.benefit_state is BenefitProgramState.UNVALUABLE or (
        income.benefit_state is BenefitProgramState.VALUED and benefit is None
    ):
        reasons.append(ReasonCode.BENEFIT_UNVALUABLE)
    return ComponentResult(reasons=tuple(reasons))


def compute_expected_return(
    *,
    evaluation_date: date,
    current_price: Decimal | None,
    price_freshness: PriceFreshnessVerdict,
    fair_value: FairValueInput,
    income: IncomeInput,
) -> ExpectedReturnResult:
    """Expected Return の構成要素(UPSIDE・INCOME)を計算する。純粋関数。

    revision・composite・annualized は常に UNDETERMINED(理由つき)で、値を作らない。
    取得価格・含み益・available_cash・時計は引数に持たない。
    """
    notes: list[Note] = []
    if price_freshness is PriceFreshnessVerdict.WARNING:
        notes.append(Note.PRICE_FRESHNESS_WARNING)
    if income.benefit_state is BenefitProgramState.NO_PROGRAM:
        notes.append(Note.BENEFIT_NO_PROGRAM_OR_UNREGISTERED)
    return ExpectedReturnResult(
        model_version=MODEL_VERSION,
        evaluation_date=evaluation_date,
        upside=compute_upside(
            current_price=current_price, price_freshness=price_freshness, fair_value=fair_value
        ),
        income=compute_income(income),
        revision=ComponentResult(reasons=(ReasonCode.REVISION_NOT_IMPLEMENTED,)),
        composite=ComponentResult(reasons=(ReasonCode.COMPOSITION_RULE_NOT_DECIDED,)),
        annualized=ComponentResult(reasons=(ReasonCode.HORIZON_NOT_ESTIMATED,)),
        notes=tuple(notes),
        fair_value_confidence=fair_value.confidence,
    )
