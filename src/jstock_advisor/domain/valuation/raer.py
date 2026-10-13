"""RAER(リスク調整後期待リターン)の型・状態・比較可能性の契約(Issue #602 PR-B1。dormant)。

#128 の RAER を、**式を決めずに**扱えるようにするための契約だけを置く。どこからも import されない
(配線なし・保存なし・flag なし)。USER 承認: #122 issuecomment-6102822046 の『1. #602 PR-B1』。

## この module が固定するもの(USER の案 Y の必須条件。#122 …6100082651)

  ・**適用できた軸と、できなかった軸を記録する**: 5 軸を固定順で必ず持つ。
    欠落した軸も UNAVAILABLE として残す
  ・**欠測を『リスクがない』と解釈しない**: `risk_flags_available = False` は『リスクなし』ではなく
    『不明』。空集合の `risk_flags` と区別する(`risk_flags_available = True` かつ空集合 =
    『確認したがリスクなし』)
  ・**暫定計算であることを状態・モデル版で識別する**: `RaerState.PROVISIONAL` と、暫定の印を含む
    `MODEL_VERSION`(literal で固定)
  ・**適用軸が異なる結果をそのまま比較しない**: `comparison_key` = model_version + 昇順の適用軸。
    key が違えば `require_comparable` が拒否する(比較可能性の判定の入口は #603。本 module は
    key の作り方と比較関数を固定する)

## ★ この module が決めない・実装しないもの(未決。PR-B2 以降。USER の承認対象外)★

  ・RAER の値(`value`)。**evaluate_raer は常に value を作らない(UNDETERMINED)** = 射影した
    `RaerResult.state` は常に UNAVAILABLE(理由 COMPOSITION_RULE_NOT_DECIDED)
  ・軸ごとの調整値・合成式・単位(X-1)/ confidence の対応(X-2)/ 必須の軸・最低軸数(X-3)
  ・軸ごとの入力の計算(company quality・risk code の判定・earnings trend 等)/ 記録先(X-4)/
    永続化・配線

`RaerResult` の型そのものは、AVAILABLE / PROVISIONAL に value(Decimal)を持てる(#603 の optimizer が
合成データで検証できるように。JIRO の合意 #602 …6100342648)。不変条件が、state と value・
comparison_key・軸の記録の整合を強制する。本 module の関数がそれらの state を作るのは、値の式が
決まった後(PR-B2)である。

## 持たないもの

  時計・営業日(evaluation_date は呼び出し側が渡す記録用の値)/ available_cash・取得単価
  (#884 の PortfolioContext と同じ規約)/ services・infrastructure・lambda_handlers・
  exit_architecture への依存

## PR-B2(#602。値の式の実装)での拡張 — 後方互換

値を作る式は `raer_composition.py` にあり、本 module は**データ型だけ**を持つ(式を持たない)。
`RaerEvaluation.composition`(任意)と `RaerComposition`・`AxisContribution`・`RiskItem` を足した。
`composition = None` かつ `model_version = MODEL_VERSION`(構造版)の範囲は、B1 の契約のまま
(5 軸の固定順・欠測とリスクなしの区別・comparison_key・常に UNAVAILABLE)。
`model_version = COMBINED_MODEL_VERSION` の評価は、composition を持つ(値あり)か、算出不能の理由
(BASE_UNDETERMINED・REQUIRED_AXIS_MISSING・AXES_BELOW_MINIMUM)を持つ。

★ 暫定(PROVISIONAL)の値は、適用できない軸の減点を含まないため、5 軸すべてを適用した値より
**上振れしうる**。混ぜて比べないことは comparison_key(適用軸を含む)が守る: 適用軸が異なれば
are_comparable は False。軸 4 が未実装の間、AVAILABLE は到達しない(常に PROVISIONAL)。
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import (
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    localcontext,
)
from enum import StrEnum
from typing import Final

from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.valuation.expected_return import ExpectedReturnResult

#: 構造だけの版(値の式を持たない)。末尾の `-provisional-axes-y` が、案 Y の暫定計算であることの印。
#: 式が決まったら版を変える(本値を literal で固定する契約テストも意図した変更として更新する)。
PROVISIONAL_MARK: Final = "-provisional-axes-y"
MODEL_VERSION: Final = "raer-structure-v1" + PROVISIONAL_MARK
#: PR-B2 の値を作る版。軸 4 が未実装の間は常に暫定(末尾の印)。係数を変えるときはこの版も変える
#: (A-4『調整したらモデル版を更新し、旧版の結果と混ぜて比べない』)。literal 固定の契約テストあり。
COMBINED_MODEL_VERSION: Final = "raer-combined-v1" + PROVISIONAL_MARK


class AxisCode(StrEnum):
    """RAER の調整軸(5 つ。#602 …5841996060)。定義の順が固定の順序。"""

    FAIR_VALUE_CONFIDENCE = "FAIR_VALUE_CONFIDENCE"
    COMPANY_QUALITY = "COMPANY_QUALITY"
    BUSINESS_RISK = "BUSINESS_RISK"
    DATA_QUALITY = "DATA_QUALITY"
    EARNINGS_TREND = "EARNINGS_TREND"


AXIS_ORDER: Final[tuple[AxisCode, ...]] = tuple(AxisCode)


class AxisStatus(StrEnum):
    APPLIED = "APPLIED"  # 適用できた
    NOT_APPLIED = "NOT_APPLIED"  # その銘柄には適用対象外
    UNAVAILABLE = "UNAVAILABLE"  # 入力が欠測・未実装で適用できない


class AxisReasonCode(StrEnum):
    """軸が APPLIED でない理由(軸ごと。必須)。"""

    AXIS_NOT_PROVIDED = "AXIS_NOT_PROVIDED"  # 呼び出し側がこの軸の状態を渡さなかった
    INPUT_MISSING = "INPUT_MISSING"  # 軸の入力が欠測
    NOT_APPLICABLE_TO_STOCK = "NOT_APPLICABLE_TO_STOCK"  # その銘柄には適用対象外
    AXIS_NOT_IMPLEMENTED = "AXIS_NOT_IMPLEMENTED"  # 軸の算出がまだ実装されていない(例: 軸 4)
    RISK_FLAGS_NOT_OBSERVED = (
        "RISK_FLAGS_NOT_OBSERVED"  # 軸 3 が適用とされたが risk code の観測が無い
    )
    COVERAGE_BELOW_MINIMUM = (
        "COVERAGE_BELOW_MINIMUM"  # 入力の充足度(coverage)が既存の下限に満たない(PR-B2)
    )


class RaerReasonCode(StrEnum):
    """RAER 全体の理由(evaluate_raer / compose_raer が記録する)。"""

    COMPOSITION_RULE_NOT_DECIDED = "COMPOSITION_RULE_NOT_DECIDED"  # 合成式・単位が未決(X-1)
    EXPECTED_RETURN_UPSIDE_UNDETERMINED = (
        "EXPECTED_RETURN_UPSIDE_UNDETERMINED"  # 土台の UPSIDE に値が無い
    )
    # --- PR-B2 で追加(compose_raer が算出不能のときに記録する)---
    EXPECTED_RETURN_INCOME_UNDETERMINED = (
        "EXPECTED_RETURN_INCOME_UNDETERMINED"  # 土台の INCOME(総合利回り)に値が無い
    )
    BASE_UNDETERMINED = "BASE_UNDETERMINED"  # 土台(UPSIDE + INCOME)のどちらかが不明(A-2)
    REQUIRED_AXIS_MISSING = "REQUIRED_AXIS_MISSING"  # 必須軸(Fair Value の信頼度)が無い(A-3)
    AXES_BELOW_MINIMUM = "AXES_BELOW_MINIMUM"  # 適用できた軸が最低数に満たない(A-3)

    @property
    def is_combined_unavailable(self) -> bool:
        """compose_raer が『値を作れない』ときに記録する理由か(3 種 + 土台の内訳 2 種)。"""
        return self in _COMBINED_UNAVAILABLE_REASONS


_COMBINED_UNAVAILABLE_REASONS: Final = frozenset(
    {
        RaerReasonCode.EXPECTED_RETURN_UPSIDE_UNDETERMINED,
        RaerReasonCode.EXPECTED_RETURN_INCOME_UNDETERMINED,
        RaerReasonCode.BASE_UNDETERMINED,
        RaerReasonCode.REQUIRED_AXIS_MISSING,
        RaerReasonCode.AXES_BELOW_MINIMUM,
    }
)


class RaerState(StrEnum):
    AVAILABLE = "AVAILABLE"  # 5 軸すべてを適用して算出
    PROVISIONAL = "PROVISIONAL"  # 案 Y による暫定の RAER(適用できない軸がある)
    UNAVAILABLE = "UNAVAILABLE"  # 算出できない


class RiskFlag(StrEnum):
    """軸 3(事業・業種 risk)の語彙(OD4 = B の 6 risk code。#122 …6100082651)。"""

    CYCLICAL_INDUSTRY = "cyclical_industry"
    MAJOR_CUSTOMER_DEPENDENCY = "major_customer_dependency"
    TEMPORARY_EARNINGS_BOOST_RISK = "temporary_earnings_boost_risk"
    EARNINGS_WITHIN_3_BUSINESS_DAYS = "earnings_within_3_business_days"
    EARNINGS_WITHIN_7_BUSINESS_DAYS = "earnings_within_7_business_days"
    SMALL_CAP_OR_LOW_LIQUIDITY = "small_cap_or_low_liquidity"


class RiskContributionStatus(StrEnum):
    """軸 3 のリスク 1 件が、減点に数えられたか、重複で除かれたか(PR-B2。追加条件 3)。"""

    COUNTED = "COUNTED"
    EXCLUDED_DUPLICATE = "EXCLUDED_DUPLICATE"


class SpecFlag(StrEnum):
    """結果に必ず付く仕様の印(固定の語。PR-B2。追加条件 1)。"""

    #: 上値余地は期間を持たず、総合利回りは年間。年率換算せずに単純合計した暫定仕様
    HORIZON_MISMATCH_PROVISIONAL = "HORIZON_MISMATCH_PROVISIONAL"


_AXIS_VALUES: Final = frozenset(axis.value for axis in AxisCode)
_RISK_FLAG_VALUES: Final = frozenset(flag.value for flag in RiskFlag)

#: 金額・%の厳密な計算に使う精度(通常の context の桁数で丸めない。capital_rotation.py と同じ作法)
EXACT_PRECISION: Final = 400


def exact_decimal_context(*, allow_inexact: bool = False) -> Context:
    """丸めを許さない Decimal の context(Inexact をトラップする)。

    `allow_inexact = True` は『丸めを 1 か所だけ許す』場所(営業トレンド点の除算)専用。
    """
    traps = [InvalidOperation, DivisionByZero, Overflow]
    if not allow_inexact:
        traps.append(Inexact)
    return Context(prec=EXACT_PRECISION, traps=traps)


class RaerNotComparableError(ValueError):
    """適用軸・モデル版が異なる(または比較できない状態の)RAER を比べようとした。"""


def make_comparison_key(model_version: str, axes_applied: Iterable[AxisCode | str]) -> str:
    """比較可能性の鍵 = model_version + 昇順の適用軸。軸の渡し順・重複に依らない。

    同じ model_version・同じ適用軸の集合なら必ず同じ値になる(空白・大小の揺れなし)。
    適用軸が無い、または未知の軸があれば拒否する。
    """
    values = {axis.value if isinstance(axis, AxisCode) else str(axis) for axis in axes_applied}
    if not values:
        raise ValueError("適用軸が無い RAER に comparison_key は作れない")
    unknown = values - _AXIS_VALUES
    if unknown:
        raise ValueError(f"未知の軸: {sorted(unknown)}")
    return f"{model_version}|{','.join(sorted(values))}"


@dataclass(frozen=True)
class AxisObservation:
    """呼び出し側が渡す、軸 1 つの状態(軸の計算は B1 の範囲外。状態と理由のみ)。"""

    status: AxisStatus
    reasons: tuple[AxisReasonCode, ...] = ()


@dataclass(frozen=True)
class AxisAdjustment:
    """軸 1 つの記録。**調整値は持たない**(式が未決。PR-B2)。"""

    axis: AxisCode
    status: AxisStatus
    reasons: tuple[AxisReasonCode, ...] = ()

    def __post_init__(self) -> None:
        if self.status is AxisStatus.APPLIED:
            if self.reasons:
                raise ValueError("適用できた軸は理由を持たない")
        elif not self.reasons:
            raise ValueError("適用できなかった軸は理由が要る")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("軸の理由コードが重複している")


@dataclass(frozen=True)
class AxisContribution:
    """軸 1 つの寄与度 = その軸が RAER から引いた量(%ポイント。0 以上)。**適用できた軸だけ**が持つ。

    適用済みで 0 は許す(U ≤ 0 の軸 1・『確認したがリスクなし』の軸 3・s ≥ 0 の軸 5)。未適用の軸に
    0 を置くことは許さない(『減点 0 と判定した』と『適用できなかった』を区別するため)。
    provenance = この寄与度を作った入力の根拠(例: 充足度・閾値・使った成分・重み・モデル版)。
    """

    axis: AxisCode
    pct_points: Decimal
    provenance: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.axis, AxisCode):
            raise ValueError("寄与度の軸は AxisCode でなければならない")
        if not isinstance(self.pct_points, Decimal) or not self.pct_points.is_finite():
            raise ValueError("寄与度は有限の Decimal でなければならない")
        if self.pct_points < 0:
            raise ValueError("寄与度は 0 以上(減点だけ。加点は無い)")


@dataclass(frozen=True)
class RiskItem:
    """軸 3 のリスク 1 件の記録(減点に数えたか、重複で除いたか)。deduction_pct = その語の引く量。"""

    flag: RiskFlag
    deduction_pct: Decimal
    status: RiskContributionStatus

    def __post_init__(self) -> None:
        if not isinstance(self.flag, RiskFlag):
            raise ValueError("RiskItem の flag は RiskFlag の語彙だけ")
        if not isinstance(self.deduction_pct, Decimal) or not self.deduction_pct.is_finite():
            raise ValueError("リスクの引く量は有限の Decimal でなければならない")
        if self.deduction_pct < 0:
            raise ValueError("リスクの引く量は 0 以上")


@dataclass(frozen=True)
class RaerComposition:
    """RAER の値と、その内訳(PR-B2。shadow の記録に残す想定)。式は持たない(raer_composition.py)。

    不変条件(すべて違反は ValueError)
      I1  寄与度を持つ軸の集合 = 適用できた軸(RaerEvaluation 側で axes_applied と照合する)
      I2  寄与度は 0 以上。適用済みで 0 は許す
      I3  軸 3 の寄与度 = COUNTED の合計 − 上限で切った量。EXCLUDED_DUPLICATE は寄与度に入らない
      I4  土台 − Σ寄与度 = value(Decimal の厳密一致)/ 土台 = 上値余地 + 総合利回り
    """

    upside_pct: Decimal
    income_pct: Decimal
    base_pct: Decimal
    value: Decimal
    contributions: tuple[AxisContribution, ...]
    risk_items: tuple[RiskItem, ...]
    risk_cap_cut_pct: Decimal
    spec_flags: frozenset[SpecFlag]
    coefficient_set_id: str
    confidence: ConfidenceLevel
    state: RaerState
    provenance: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name in ("upside_pct", "income_pct", "base_pct", "value", "risk_cap_cut_pct"):
            number = getattr(self, name)
            if not isinstance(number, Decimal) or not number.is_finite():
                raise ValueError(f"{name} は有限の Decimal でなければならない")
        if self.state not in (RaerState.AVAILABLE, RaerState.PROVISIONAL):
            raise ValueError("値を持つ composition の状態は AVAILABLE / PROVISIONAL だけ")
        if not isinstance(self.confidence, ConfidenceLevel):
            raise ValueError("composition は信頼度を持つ")
        if SpecFlag.HORIZON_MISMATCH_PROVISIONAL not in self.spec_flags:
            raise ValueError("時間軸の違いを暫定仕様として記録する印が要る(追加条件 1)")
        if not self.coefficient_set_id:
            raise ValueError("係数の識別子が要る")
        order = [c.axis for c in self.contributions]
        if len(set(order)) != len(order) or order != [a for a in AXIS_ORDER if a in set(order)]:
            raise ValueError("寄与度は軸ごとに 1 件・固定の順序")
        with localcontext(exact_decimal_context()):
            if self.base_pct != self.upside_pct + self.income_pct:
                raise ValueError("土台 = 上値余地 + 総合利回り")
            total = sum((c.pct_points for c in self.contributions), start=Decimal(0))
            if self.value != self.base_pct - total:
                raise ValueError("土台 − Σ寄与度 = value(恒等式)")
            if self.risk_cap_cut_pct < 0:
                raise ValueError("上限で切った量は 0 以上")
            risk_contrib = [c for c in self.contributions if c.axis is AxisCode.BUSINESS_RISK]
            flags = [i.flag for i in self.risk_items]
            if len(set(flags)) != len(flags):
                raise ValueError("リスクの記録が重複している")
            if risk_contrib:
                counted = sum(
                    (
                        i.deduction_pct
                        for i in self.risk_items
                        if i.status is RiskContributionStatus.COUNTED
                    ),
                    start=Decimal(0),
                )
                if self.risk_cap_cut_pct > counted:
                    raise ValueError("上限で切った量が数えた合計を超えている")
                if risk_contrib[0].pct_points != counted - self.risk_cap_cut_pct:
                    raise ValueError("軸 3 の寄与度 = 数えた合計 − 上限で切った量")
            elif self.risk_items or self.risk_cap_cut_pct != 0:
                raise ValueError("軸 3 が適用されていないのにリスクの記録を持てない")
        applied_count = len(self.contributions)
        if self.state is RaerState.AVAILABLE and applied_count != len(AXIS_ORDER):
            raise ValueError("AVAILABLE は 5 軸すべてを適用したときだけ")
        if self.state is RaerState.PROVISIONAL and applied_count == len(AXIS_ORDER):
            raise ValueError("PROVISIONAL は適用できない軸があるときだけ")


@dataclass(frozen=True)
class RaerEvaluation:
    """RAER の詳細な記録(shadow の記録に残す想定)。5 軸を固定順で必ず持つ。

    ・`composition = None` かつ構造版(`MODEL_VERSION`): B1 の契約 = value を持たず、`state` は常に
      UNAVAILABLE(COMPOSITION_RULE_NOT_DECIDED を必ず持つ)
    ・`model_version = COMBINED_MODEL_VERSION`(PR-B2): `composition` があれば値あり(state・value・
      信頼度は composition から)。無ければ算出不能で、理由(BASE_UNDETERMINED・
      REQUIRED_AXIS_MISSING・
      AXES_BELOW_MINIMUM のいずれか)を持つ

    `comparison_key` は**記録用**で、適用軸が 1 つでもあれば state に依らず作る(どの軸で評価したかを
    後から説明できるように)。比較に使う `RaerResult.comparison_key` は別物で、UNAVAILABLE では None
    (UNAVAILABLE は比較の対象にならない)。
    """

    model_version: str
    evaluation_date: date
    expected_return: ExpectedReturnResult
    axes: tuple[AxisAdjustment, ...]
    risk_flags: frozenset[RiskFlag]
    risk_flags_available: bool
    reasons: tuple[RaerReasonCode, ...]
    composition: RaerComposition | None = None

    def __post_init__(self) -> None:
        if tuple(a.axis for a in self.axes) != AXIS_ORDER:
            raise ValueError("5 軸を固定の順序で必ず持つ")
        risk_axis = self.axes[AXIS_ORDER.index(AxisCode.BUSINESS_RISK)]
        if self.risk_flags_available != (risk_axis.status is AxisStatus.APPLIED):
            raise ValueError("risk_flags_available は軸 3 が適用されたときだけ True")
        if not self.risk_flags_available and self.risk_flags:
            raise ValueError("risk の観測が無いのに risk_flags を持てない(不明を空集合にしない)")
        combined = self.model_version == COMBINED_MODEL_VERSION
        if self.composition is not None and not combined:
            raise ValueError("composition は組合せ版(COMBINED_MODEL_VERSION)だけが持つ")
        if not combined and RaerReasonCode.COMPOSITION_RULE_NOT_DECIDED not in self.reasons:
            raise ValueError("B1 は合成式が未決の理由を常に持つ")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("RAER の理由コードが重複している")
        if combined:
            self._check_combined()

    def _check_combined(self) -> None:
        if RaerReasonCode.COMPOSITION_RULE_NOT_DECIDED in self.reasons:
            raise ValueError("組合せ版は合成式が未決の理由を持たない")
        if self.composition is None:
            if not self.reasons or not all(r.is_combined_unavailable for r in self.reasons):
                raise ValueError("組合せ版の算出不能は、算出不能の理由だけを持つ")
            decisive = {
                RaerReasonCode.BASE_UNDETERMINED,
                RaerReasonCode.REQUIRED_AXIS_MISSING,
                RaerReasonCode.AXES_BELOW_MINIMUM,
            }
            if not decisive & set(self.reasons):
                raise ValueError(
                    "組合せ版の算出不能は、土台・必須軸・最低軸数のいずれかの理由が要る"
                )
            return
        if self.reasons:
            raise ValueError("値を持つ組合せ版は算出不能の理由を持たない")
        if {c.axis for c in self.composition.contributions} != set(self.axes_applied):
            raise ValueError("寄与度を持つ軸の集合は、適用できた軸と一致しなければならない(I1)")
        if self.composition.state is RaerState.AVAILABLE and self.axes_not_applied:
            raise ValueError("AVAILABLE は 5 軸すべてを適用したときだけ")

    @property
    def state(self) -> RaerState:
        if self.composition is not None:
            return self.composition.state
        return RaerState.UNAVAILABLE  # 値を作っていない(構造版・または組合せ版の算出不能)

    @property
    def axes_applied(self) -> frozenset[AxisCode]:
        return frozenset(a.axis for a in self.axes if a.status is AxisStatus.APPLIED)

    @property
    def axes_not_applied(self) -> frozenset[AxisCode]:
        return frozenset(a.axis for a in self.axes if a.status is not AxisStatus.APPLIED)

    @property
    def comparison_key(self) -> str | None:
        """適用軸が 1 つも無ければ None(比較の鍵を作れない)。"""
        applied = self.axes_applied
        return make_comparison_key(self.model_version, applied) if applied else None


@dataclass(frozen=True)
class RaerResult:
    """#603 / #604 が受け取る最小の境界(rev1.1。JIRO の合意済み)。軸の中身(調整値)に依存しない。

    `comparison_key` は**比較に使う鍵**で、AVAILABLE / PROVISIONAL のときだけ持ち、UNAVAILABLE では
    None(`RaerEvaluation.comparison_key` は記録用で、UNAVAILABLE でも作られる)。
    """

    state: RaerState
    value: Decimal | None
    confidence: ConfidenceLevel | None
    risk_flags: frozenset[str]
    risk_flags_available: bool
    model_version: str
    comparison_key: str | None
    axes_applied: frozenset[str]
    axes_not_applied: frozenset[str]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.axes_applied & self.axes_not_applied:
            raise ValueError("適用した軸と適用できなかった軸が重なっている")
        if self.axes_applied | self.axes_not_applied != _AXIS_VALUES:
            raise ValueError("5 軸すべてを、適用 / 不適用のどちらかに記録する")
        if self.risk_flags - _RISK_FLAG_VALUES:
            raise ValueError("risk_flags の語彙外の値")
        if self.risk_flags_available != (AxisCode.BUSINESS_RISK.value in self.axes_applied):
            raise ValueError("risk_flags_available は軸 3 が適用されたときだけ True")
        if not self.risk_flags_available and self.risk_flags:
            raise ValueError("risk の観測が無いのに risk_flags を持てない(不明を空集合にしない)")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("理由コードが重複している")
        if self.state is RaerState.UNAVAILABLE:
            if self.value is not None or self.comparison_key is not None:
                raise ValueError("UNAVAILABLE は value も comparison_key も持たない")
            if self.confidence is not None:
                raise ValueError("UNAVAILABLE は confidence を持たない")
            if not self.reason_codes:
                raise ValueError("UNAVAILABLE は理由が要る")
            return
        if self.value is None or not self.value.is_finite():
            raise ValueError("AVAILABLE / PROVISIONAL は有限の value が要る")
        if not self.axes_applied:
            raise ValueError("AVAILABLE / PROVISIONAL は適用した軸が要る")
        if self.comparison_key != make_comparison_key(self.model_version, self.axes_applied):
            raise ValueError(
                "comparison_key は model_version と適用軸から決まる値でなければならない"
            )
        if self.state is RaerState.AVAILABLE:
            if self.axes_not_applied:
                raise ValueError("AVAILABLE は 5 軸すべてを適用したときだけ")
        else:
            if not self.axes_not_applied:
                raise ValueError("PROVISIONAL は適用できない軸があるときだけ")
            if PROVISIONAL_MARK not in self.model_version:
                raise ValueError("PROVISIONAL は model_version に暫定の印を含む")
            if not self.reason_codes:
                raise ValueError("PROVISIONAL は適用できなかった理由が要る")


def are_comparable(a: RaerResult, b: RaerResult) -> bool:
    """両方が AVAILABLE / PROVISIONAL で、comparison_key が等しいときだけ True。

    UNAVAILABLE は『除外して理由を残す』対象で、比較不能の判定には使わない(#603 の入口で扱う)。
    """
    usable = {RaerState.AVAILABLE, RaerState.PROVISIONAL}
    # AVAILABLE / PROVISIONAL は必ず comparison_key を持つ(RaerResult の不変条件)。
    # 状態の確認を key の None 判定に任せず、明示的に行う
    return a.state in usable and b.state in usable and a.comparison_key == b.comparison_key


def require_comparable(a: RaerResult, b: RaerResult) -> None:
    """比較できない 2 つを比べようとしたら拒否する(値の大小を比べる前に呼ぶ)。"""
    if not are_comparable(a, b):
        raise RaerNotComparableError(
            "適用軸・モデル版が異なる、または比較できない状態の RAER は比較できない"
        )


def evaluate_raer(
    *,
    evaluation_date: date,
    expected_return: ExpectedReturnResult,
    axes: Mapping[AxisCode, AxisObservation],
    risk_flags: Collection[RiskFlag] | None,
) -> RaerEvaluation:
    """軸の状態を記録して RaerEvaluation を返す(純粋関数)。**値は作らない**(UNDETERMINED)。

    ・5 軸を固定順で必ず持つ。渡されなかった軸は UNAVAILABLE(AXIS_NOT_PROVIDED)として残す
    ・軸 3 が適用とされても risk の観測(risk_flags)が None なら、『リスクなし』にせず UNAVAILABLE
      (RISK_FLAGS_NOT_OBSERVED)とする。risk_flags_available は軸 3 が適用されたときだけ True
    ・軸 3 が適用でないのに risk_flags が渡されたら拒否する(観測の無い軸の語を信じない)
    """
    if evaluation_date != expected_return.evaluation_date:
        raise ValueError("RAER の評価日は Expected Return の評価日と一致しなければならない")
    unknown = [key for key in axes if key not in AXIS_ORDER]
    if unknown:
        raise ValueError(f"未知の軸: {unknown}")
    flags = frozenset(risk_flags) if risk_flags is not None else None
    if flags is not None and any(not isinstance(f, RiskFlag) for f in flags):
        raise ValueError("risk_flags は RiskFlag の語彙だけ")

    adjustments: list[AxisAdjustment] = []
    for axis in AXIS_ORDER:
        observation = axes.get(axis)
        if observation is None:
            adjustments.append(
                AxisAdjustment(axis, AxisStatus.UNAVAILABLE, (AxisReasonCode.AXIS_NOT_PROVIDED,))
            )
            continue
        if (
            axis is AxisCode.BUSINESS_RISK
            and observation.status is AxisStatus.APPLIED
            and flags is None
        ):
            adjustments.append(
                AxisAdjustment(
                    axis, AxisStatus.UNAVAILABLE, (AxisReasonCode.RISK_FLAGS_NOT_OBSERVED,)
                )
            )
            continue
        adjustments.append(AxisAdjustment(axis, observation.status, observation.reasons))

    risk_axis = adjustments[AXIS_ORDER.index(AxisCode.BUSINESS_RISK)]
    risk_available = risk_axis.status is AxisStatus.APPLIED
    if not risk_available and flags:
        raise ValueError("軸 3 が適用されていないのに risk_flags が渡された")

    reasons = [RaerReasonCode.COMPOSITION_RULE_NOT_DECIDED]
    upside = expected_return.upside.value
    if upside is None or not math.isfinite(upside):
        reasons.append(RaerReasonCode.EXPECTED_RETURN_UPSIDE_UNDETERMINED)

    return RaerEvaluation(
        model_version=MODEL_VERSION,
        evaluation_date=evaluation_date,
        expected_return=expected_return,
        axes=tuple(adjustments),
        risk_flags=flags if (risk_available and flags is not None) else frozenset(),
        risk_flags_available=risk_available,
        reasons=tuple(reasons),
    )


def to_raer_result(evaluation: RaerEvaluation) -> RaerResult:
    """RaerEvaluation から #603 / #604 向けの最小の境界へ射影する(純粋関数)。

    構造版(composition なし)と組合せ版の算出不能は、常に UNAVAILABLE
    (value も comparison_key も confidence も持たない)。組合せ版で値があるときは、
    state・value・信頼度・comparison_key(model_version + 適用軸)を持つ。
    適用 / 不適用の軸と risk の記録、理由(RAER 全体 + 軸ごと)は残す。
    """
    reason_codes = tuple(reason.value for reason in evaluation.reasons) + tuple(
        f"{adjustment.axis.value}:{reason.value}"
        for adjustment in evaluation.axes
        for reason in adjustment.reasons
    )
    composition = evaluation.composition
    return RaerResult(
        state=evaluation.state,
        value=None if composition is None else composition.value,
        confidence=None if composition is None else composition.confidence,
        risk_flags=frozenset(flag.value for flag in evaluation.risk_flags),
        risk_flags_available=evaluation.risk_flags_available,
        model_version=evaluation.model_version,
        comparison_key=None if composition is None else evaluation.comparison_key,
        axes_applied=frozenset(axis.value for axis in evaluation.axes_applied),
        axes_not_applied=frozenset(axis.value for axis in evaluation.axes_not_applied),
        reason_codes=reason_codes,
    )
