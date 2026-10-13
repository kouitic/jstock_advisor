"""RAER の組合せの式(Issue #602 PR-B2。dormant)。承認済みの暫定式 A-1〜A-5 の実装。

USER 承認: #122 issuecomment-6114461065【1】(A-1〜A-5 + 追加条件 3 点。設計承認)。
確定仕様の整理 = #602 issuecomment-6114608473。設計 rev1〜rev3 = #602 issuecomment-6137191422・
…6137343422・…6137442140(サブちゃんの設計レビュー PASS = …6137527672)。
どこからも import されない(配線なし・保存なし・flag なし)。

## 式(単位はすべて『現在価格に対する %』。Decimal で厳密に計算する)

```
RAER = U' + I - D            U' = max(U, 0) x f1 + min(U, 0)(上値余地のマイナス分は割り引かない)
                             D  = d2 + d3 + d5(適用できない軸は 0 ではなく『適用なし』と記録)
軸 1  f1 = fv_confidence_factor[Fair Value の信頼度]。寄与度 = max(U, 0) x (1 - f1)
軸 2  d2 = quality_deduction_max x (1 - Q / quality_max_score)。Q は保有側の企業品質(0〜50)
軸 3  d3 = min(重複を除いた各リスクの引く量の合計, risk_deduction_cap)
軸 4  常に適用なし(AXIS_NOT_IMPLEMENTED)
軸 5  d5 = trend_deduction_scale x max(0, -s) / 100(悪化だけ減点。改善は加点しない。配当方向なし)
土台  E = U + I。どちらかが不明なら算出不能(片方だけで合計しない)。年率換算・Revision は入れない
状態  適用軸が 5 つなら AVAILABLE、それ以外は PROVISIONAL(軸 4 が未実装の間は常に PROVISIONAL)
```

## この module が守ること

  ・**float で計算しない**: 外部から入る数は `to_decimal`(= `Decimal(repr(x))`)の 1 か所で取り込む。
    丸めるのは営業トレンド点の除算の 1 か所だけ(局所 context の精度 400 → 小数 6 桁・偶数丸め)。
    他の演算は有限小数で厳密。恒等式(土台 − Σ寄与度 = value)は Decimal の厳密一致
  ・**欠測を 0 や中立値で補完しない**: 欠測の軸は UNAVAILABLE(理由つき)。土台が不明なら RAER 全体が
    UNAVAILABLE。範囲外・非有限・型の誤りは ValueError(呼び出し側の契約違反。PR-F が隔離する)
  ・**係数を literal で持たない**: 係数は引数(`RaerCoefficients`。出荷値は PR-E の YAML)。閾値
    (充足度の下限・営業トレンドの下限)も既存の設定値を呼び出し側が渡し、記録に残す
  ・時計・I/O・永続化・ログ・services / infrastructure / lambda_handlers への依存を持たない

## 入力の契約(PR-F への引き渡し契約 H1〜H6。#602 …6137442140 の R8)

  ・企業品質は**保有側**の 0〜50 点(`holding_quality_score_0_50`)。BUY 側の
    `company_quality_score` は 100 点満点で別の尺度(渡さない。範囲外は ValueError)。
    充足度(coverage_ratio)と既存の下限を必須とする
  ・営業トレンド点 s は**既存の総合トレンド点(4 成分)とは別物**(営業利益・営業 CF の 2 成分のみ。
    配当方向・加速度を受け取らない)。`trend_score_for_raer` が既存の評価状態・充足度の下限を強制する

## 信頼度(X-2。判断資料 #602 …6114088624 の X-2。1 関数 + 2 定数に局所化)

RAER の信頼度は RAER の値(%)には影響しない記録用のラベル。ただし #603 の配分の順位の第 2 キー
(value 降順に次ぐ)に使われる。PROVISIONAL の間は最大 MEDIUM、適用軸が 3 以下なら LOW。
軸 4 が未実装の間は HIGH に到達しない(実質 LOW / MEDIUM の 2 値)。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from jstock_advisor.domain.entities.enums import ConfidenceLevel, EarningsTrendEvaluationState
from jstock_advisor.domain.valuation.expected_return import ExpectedReturnResult
from jstock_advisor.domain.valuation.raer import (
    AXIS_ORDER,
    COMBINED_MODEL_VERSION,
    AxisAdjustment,
    AxisCode,
    AxisContribution,
    AxisReasonCode,
    AxisStatus,
    RaerComposition,
    RaerEvaluation,
    RaerReasonCode,
    RaerState,
    RiskContributionStatus,
    RiskFlag,
    RiskItem,
    SpecFlag,
    exact_decimal_context,
)

#: 係数の識別子の版(識別子の作り方を変えたら上げる)
COEFFICIENT_SET_ID_VERSION: Final = "c1"
#: 営業トレンド点 s の丸め(除算の 1 か所だけ)
TREND_SCORE_QUANTUM: Final = Decimal("0.000001")

# --- 信頼度の規則(X-2。ここだけを変えれば済むよう局所化する)---
#: PROVISIONAL の間の信頼度の上限(軸 4 が無いため)
PROVISIONAL_CONFIDENCE_CEILING: Final = ConfidenceLevel.MEDIUM
#: 適用軸がこの数以下なら信頼度は LOW
LOW_CONFIDENCE_MAX_APPLIED_AXES: Final = 3

_HUNDRED: Final = Decimal(100)
_RISK_ORDER: Final = tuple(RiskFlag)
_CONFIDENCE_ORDER: Final = (ConfidenceLevel.LOW, ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH)


class TrendComponent(StrEnum):
    """営業トレンド点 s を作った成分(営業利益・営業 CF の 2 成分のみ。配当方向・加速度は無い)。"""

    OPERATING_INCOME = "OPERATING_INCOME"
    OPERATING_CASHFLOW = "OPERATING_CASHFLOW"


def to_decimal(value: object, name: str = "value") -> Decimal:
    """外部から入る数の**唯一の取り込み口**。float は `Decimal(repr(x))` で正確に取り込む。

    bool・文字列・非有限は拒否する(黙って 0 や既定値にしない)。以降の計算に float を使わない。
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} は数値でなければならない(bool 不可)")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(repr(value))
    else:
        raise ValueError(f"{name} は数値でなければならない")
    if not result.is_finite():
        raise ValueError(f"{name} は有限でなければならない")
    return result


def _text(value: Decimal) -> str:
    """Decimal の正準な文字列(0.85 と 0.850 を同じにする。-0 を 0 にする)。"""
    return format(value.normalize() + Decimal(0), "f")


def _require_decimal_range(
    name: str, value: object, low: Decimal | None = None, high: Decimal | None = None
) -> Decimal:
    number = to_decimal(value, name)
    if low is not None and number < low:
        raise ValueError(f"{name} は {low} 以上でなければならない")
    if high is not None and number > high:
        raise ValueError(f"{name} は {high} 以下でなければならない")
    return number


# --- 係数 --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RaerCoefficients:
    """RAER の式の係数(承認済みの初期値を YAML 〔PR-E〕から受ける)。literal の既定値を持たない。

    係数が 1 つでも変われば `coefficient_set_id` が変わる。識別子の算出は**この 1 か所だけ**(PR-E は
    YAML から再計算せず、読んだ係数から作った識別子がこの算出と一致することだけを確かめる)。
    識別子には COMBINED_MODEL_VERSION も含める(係数が同じでもモデル版が変われば識別子が変わる)。
    営業トレンドの重みは既存の earnings_trend_rules.yaml の値で、
    ここには持たない(重複した値を作らない)。
    """

    fv_confidence_factor: Mapping[ConfidenceLevel, Decimal]
    quality_deduction_max: Decimal
    quality_max_score: Decimal
    risk_deductions: Mapping[RiskFlag, Decimal]
    risk_deduction_cap: Decimal
    trend_deduction_scale: Decimal
    min_applied_axes: int
    required_axes: frozenset[AxisCode]

    def __post_init__(self) -> None:
        factors = {
            ConfidenceLevel(k): _require_decimal_range(f"fv_confidence_factor[{k}]", v, Decimal(0))
            for k, v in dict(self.fv_confidence_factor).items()
        }
        if set(factors) != set(ConfidenceLevel):
            raise ValueError("fv_confidence_factor は HIGH / MEDIUM / LOW のすべてが要る")
        high, medium, low = (
            factors[ConfidenceLevel.HIGH],
            factors[ConfidenceLevel.MEDIUM],
            factors[ConfidenceLevel.LOW],
        )
        if not (Decimal(0) < low <= medium <= high <= Decimal(1)):
            raise ValueError("fv_confidence_factor は 0 < LOW <= MEDIUM <= HIGH <= 1")
        deductions = {
            RiskFlag(k): _require_decimal_range(f"risk_deductions[{k}]", v, Decimal(0))
            for k, v in dict(self.risk_deductions).items()
        }
        if set(deductions) != set(RiskFlag):
            raise ValueError("risk_deductions は 6 語すべてが要る(欠落を 0 で補わない)")
        quality_max = _require_decimal_range("quality_deduction_max", self.quality_deduction_max)
        if quality_max < 0:
            raise ValueError("quality_deduction_max は 0 以上")
        score_max = _require_decimal_range("quality_max_score", self.quality_max_score)
        if score_max <= 0:
            raise ValueError("quality_max_score は正")
        try:
            with localcontext(exact_decimal_context()):
                Decimal(1) / score_max  # Q / 満点が有限小数で割り切れる値だけ(丸めを入れない)
        except ArithmeticError:
            raise ValueError(
                "quality_max_score は割り切れる値(2 と 5 の因数だけ)でなければならない"
            ) from None
        cap = _require_decimal_range("risk_deduction_cap", self.risk_deduction_cap, Decimal(0))
        scale = _require_decimal_range(
            "trend_deduction_scale", self.trend_deduction_scale, Decimal(0)
        )
        if isinstance(self.min_applied_axes, bool) or not isinstance(self.min_applied_axes, int):
            raise ValueError("min_applied_axes は整数")
        if not 1 <= self.min_applied_axes <= len(AXIS_ORDER):
            raise ValueError("min_applied_axes は 1〜5")
        required = frozenset(self.required_axes)
        if any(not isinstance(a, AxisCode) for a in required):
            raise ValueError("required_axes は AxisCode の語彙だけ")
        object.__setattr__(self, "fv_confidence_factor", MappingProxyType(factors))
        object.__setattr__(self, "risk_deductions", MappingProxyType(deductions))
        object.__setattr__(self, "quality_deduction_max", quality_max)
        object.__setattr__(self, "quality_max_score", score_max)
        object.__setattr__(self, "risk_deduction_cap", cap)
        object.__setattr__(self, "trend_deduction_scale", scale)
        object.__setattr__(self, "required_axes", required)

    @property
    def coefficient_set_id(self) -> str:
        """係数 + モデル版の正準な内容の sha256(先頭 16 桁)+ 識別子の版。"""
        content = {
            "model_version": COMBINED_MODEL_VERSION,
            "fv_confidence_factor": {
                k.value: _text(v)
                for k, v in sorted(self.fv_confidence_factor.items(), key=lambda kv: kv[0].value)
            },
            "quality_deduction_max": _text(self.quality_deduction_max),
            "quality_max_score": _text(self.quality_max_score),
            "risk_deductions": {
                k.value: _text(v)
                for k, v in sorted(self.risk_deductions.items(), key=lambda kv: kv[0].value)
            },
            "risk_deduction_cap": _text(self.risk_deduction_cap),
            "trend_deduction_scale": _text(self.trend_deduction_scale),
            "min_applied_axes": self.min_applied_axes,
            "required_axes": sorted(a.value for a in self.required_axes),
        }
        text = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        return f"{digest}-{COEFFICIENT_SET_ID_VERSION}"


# --- 入力 --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class QualityInput:
    """軸 2 の入力。**保有側**の企業品質(0〜50 点)と、その充足度・既存の下限(必須)。

    ★ BUY 側の `company_quality_score`(100 点満点)は別の尺度で、渡さない(範囲外は ValueError)。
    充足度(coverage_ratio)は『評価できた配点 / 利用可能な配点』。既存機構は未評価を 0 点として
    score に算入するため、`coverage_ratio < min_coverage` なら軸 2 を適用しない(『品質が低い』と
    『データが無い』を区別する)。min_coverage は既存の
    coverage_thresholds.company_quality_minimum を呼び出し側が渡す(新しい閾値を作らない)。
    """

    holding_quality_score_0_50: Decimal
    coverage_ratio: Decimal
    min_coverage: Decimal
    company_quality_model_version: str | None = None

    def __post_init__(self) -> None:
        score = _require_decimal_range(
            "holding_quality_score_0_50", self.holding_quality_score_0_50, Decimal(0), Decimal(50)
        )
        coverage = _require_decimal_range(
            "coverage_ratio", self.coverage_ratio, Decimal(0), Decimal(1)
        )
        minimum = _require_decimal_range("min_coverage", self.min_coverage, Decimal(0), Decimal(1))
        object.__setattr__(self, "holding_quality_score_0_50", score)
        object.__setattr__(self, "coverage_ratio", coverage)
        object.__setattr__(self, "min_coverage", minimum)


@dataclass(frozen=True)
class TrendWeights:
    """既存の earnings_trend_rules.yaml の 4 成分の重み(呼び出し側が既存の値を渡す。
    ここでは持たない)。"""

    operating_income: Decimal
    operating_cashflow: Decimal
    dividend_direction: Decimal
    acceleration: Decimal

    def __post_init__(self) -> None:
        for name in (
            "operating_income",
            "operating_cashflow",
            "dividend_direction",
            "acceleration",
        ):
            object.__setattr__(
                self,
                name,
                _require_decimal_range(f"weights.{name}", getattr(self, name), Decimal(0)),
            )
        if self.total <= 0:
            raise ValueError("重みの合計は正でなければならない")

    @property
    def total(self) -> Decimal:
        with localcontext(exact_decimal_context()):
            return (
                self.operating_income
                + self.operating_cashflow
                + self.dividend_direction
                + self.acceleration
            )


@dataclass(frozen=True)
class OperatingTrendScore:
    """営業トレンド点 s(軸 5 の入力)。

    ★ 既存の総合トレンド点(4 成分・配当方向と加速度を含む)とは別物 ★。営業利益・営業 CF の
    2 成分だけから作る。`components_used` と `partial_coverage` で、どの成分から作った s かを
    区別できる。value は小数 6 桁(偶数丸め)。
    """

    value: Decimal
    components_used: frozenset[TrendComponent]
    partial_coverage: Decimal
    min_coverage_required: Decimal
    weights: TrendWeights
    earnings_trend_model_version: str | None = None

    def __post_init__(self) -> None:
        value = _require_decimal_range(
            "operating_trend_score", self.value, Decimal(-100), Decimal(100)
        )
        if value != value.quantize(TREND_SCORE_QUANTUM):
            raise ValueError("営業トレンド点は小数 6 桁に丸めた値でなければならない")
        if not self.components_used:
            raise ValueError("s を作った成分が要る")
        if any(not isinstance(c, TrendComponent) for c in self.components_used):
            raise ValueError("components_used は TrendComponent の語彙だけ")


@dataclass(frozen=True)
class TrendUnavailable:
    """営業トレンド点を作れない理由(軸 5 の状態)。評価不能のデータから減点を作らない。"""

    status: AxisStatus
    reason: AxisReasonCode

    def __post_init__(self) -> None:
        if self.status is AxisStatus.APPLIED:
            raise ValueError("APPLIED の軸は TrendUnavailable ではない")


@dataclass(frozen=True)
class RaerInputs:
    """compose_raer の入力。

    Fair Value の信頼度は expected_return.fair_value_confidence から読む(H5)。

    risk_flags: None = 観測なし(軸 3 = UNAVAILABLE)/ 空 = 確認したがリスクなし。
    operating_trend: OperatingTrendScore(適用)/ TrendUnavailable(理由つき)/ None(提供なし)。
    """

    expected_return: ExpectedReturnResult
    quality: QualityInput | None
    risk_flags: Collection[RiskFlag] | None
    operating_trend: OperatingTrendScore | TrendUnavailable | None


# --- 営業トレンド点の組み立て(丸めはここの除算の 1 か所だけ)-------------------------------------


def trend_score_for_raer(
    *,
    evaluation_state: EarningsTrendEvaluationState,
    operating_income_component: object,
    operating_cashflow_component: object,
    weights: TrendWeights,
    min_coverage_required: object,
    earnings_trend_model_version: str | None = None,
) -> OperatingTrendScore | TrendUnavailable:
    """既存の EarningsTrendResult の成分から、営業トレンド点 s を作る。

    配当方向・加速度は受け取らない。

    ・evaluation_state が EVALUATED でなければ s を作らない(評価不能のデータから減点を作らない)。
      NOT_EVALUATED → UNAVAILABLE(COVERAGE_BELOW_MINIMUM)/ NOT_APPLICABLE → NOT_APPLIED
    ・営業利益・営業 CF の 2 成分だけで、既存と同じ規則を再適用する: 部分 coverage =
      利用可能な 2 成分の重みの和 / 4 成分の重みの合計(既存の分母)。`< min_coverage_required`
      なら作らない
    ・s = Σ(w_i x c_i) / Σ w_i(利用可能な成分のみ)。小数 6 桁・偶数丸め(丸めはこの除算だけ)
    """
    if not isinstance(evaluation_state, EarningsTrendEvaluationState):
        raise ValueError("evaluation_state は EarningsTrendEvaluationState でなければならない")
    minimum = _require_decimal_range(
        "min_coverage_required", min_coverage_required, Decimal(0), Decimal(1)
    )
    components: list[tuple[TrendComponent, Decimal, Decimal]] = []
    for kind, raw, weight in (
        (TrendComponent.OPERATING_INCOME, operating_income_component, weights.operating_income),
        (
            TrendComponent.OPERATING_CASHFLOW,
            operating_cashflow_component,
            weights.operating_cashflow,
        ),
    ):
        if raw is None:
            continue
        components.append(
            (
                kind,
                _require_decimal_range(f"{kind.value} の成分", raw, Decimal(-100), Decimal(100)),
                weight,
            )
        )
    if evaluation_state is EarningsTrendEvaluationState.NOT_APPLICABLE:
        return TrendUnavailable(AxisStatus.NOT_APPLIED, AxisReasonCode.NOT_APPLICABLE_TO_STOCK)
    if evaluation_state is EarningsTrendEvaluationState.NOT_EVALUATED:
        return TrendUnavailable(AxisStatus.UNAVAILABLE, AxisReasonCode.COVERAGE_BELOW_MINIMUM)
    available_weight = sum((w for _, _, w in components), start=Decimal(0))
    if not components or available_weight <= 0:
        return TrendUnavailable(AxisStatus.UNAVAILABLE, AxisReasonCode.INPUT_MISSING)
    with localcontext(exact_decimal_context()):
        # 部分 coverage < 下限 を、除算せずに厳密に判定する(available / total < minimum と同値)
        if available_weight < minimum * weights.total:
            return TrendUnavailable(AxisStatus.UNAVAILABLE, AxisReasonCode.COVERAGE_BELOW_MINIMUM)
        numerator = sum((c * w for _, c, w in components), start=Decimal(0))
    # ★ 丸めはここだけ(除算が有限にならない): s と、記録用の部分 coverage(判定には使わない)
    with localcontext(exact_decimal_context(allow_inexact=True)):
        score = (numerator / available_weight).quantize(
            TREND_SCORE_QUANTUM, rounding=ROUND_HALF_EVEN
        )
        partial_coverage = (available_weight / weights.total).quantize(
            TREND_SCORE_QUANTUM, rounding=ROUND_HALF_EVEN
        )
    return OperatingTrendScore(
        value=score,
        components_used=frozenset(kind for kind, _, _ in components),
        partial_coverage=partial_coverage,
        min_coverage_required=minimum,
        weights=weights,
        earnings_trend_model_version=earnings_trend_model_version,
    )


# --- 信頼度(X-2。1 関数に局所化)-----------------------------------------------------------------


def resolve_raer_confidence(
    fair_value_confidence: ConfidenceLevel, *, state: RaerState, applied_axes: int
) -> ConfidenceLevel:
    """RAER の信頼度のラベル。Fair Value の信頼度が基本。PROVISIONAL の間は最大 MEDIUM、
    適用軸が 3 以下なら LOW。

    RAER の値(%)には影響しない。#603 の配分の順位の第 2 キーに使われる。
    """
    if not isinstance(fair_value_confidence, ConfidenceLevel):
        raise ValueError("fair_value_confidence は ConfidenceLevel でなければならない")
    if state is RaerState.UNAVAILABLE:
        raise ValueError("算出不能の RAER は信頼度を持たない")
    if applied_axes <= LOW_CONFIDENCE_MAX_APPLIED_AXES:
        return ConfidenceLevel.LOW
    result = fair_value_confidence
    if state is RaerState.PROVISIONAL and _CONFIDENCE_ORDER.index(result) > _CONFIDENCE_ORDER.index(
        PROVISIONAL_CONFIDENCE_CEILING
    ):
        result = PROVISIONAL_CONFIDENCE_CEILING
    return result


# --- 軸 3 のリスク(重複の除外と上限)--------------------------------------------------------------


def _risk_items(
    flags: frozenset[RiskFlag], coefficients: RaerCoefficients
) -> tuple[tuple[RiskItem, ...], Decimal, Decimal]:
    """(記録, 数えた合計から上限で切った後の寄与度, 上限で切った量)。

    重複の除外(承認済みの暫定式。既存の MarginRiskCategory とは別の規則):
      ① 決算 3 営業日以内があれば 7 営業日以内は数えない
      ② 景気に左右される業種と一時的な増益は、引く量の大きい方だけ数える(同値なら RiskFlag
      の定義順で先)
    """
    excluded: set[RiskFlag] = set()
    if (
        RiskFlag.EARNINGS_WITHIN_3_BUSINESS_DAYS in flags
        and RiskFlag.EARNINGS_WITHIN_7_BUSINESS_DAYS in flags
    ):
        excluded.add(RiskFlag.EARNINGS_WITHIN_7_BUSINESS_DAYS)
    pair = [
        f
        for f in (RiskFlag.CYCLICAL_INDUSTRY, RiskFlag.TEMPORARY_EARNINGS_BOOST_RISK)
        if f in flags
    ]
    if len(pair) == 2:
        keep = max(pair, key=lambda f: (coefficients.risk_deductions[f], -_RISK_ORDER.index(f)))
        excluded.update(f for f in pair if f is not keep)
    items = tuple(
        RiskItem(
            flag,
            coefficients.risk_deductions[flag],
            RiskContributionStatus.EXCLUDED_DUPLICATE
            if flag in excluded
            else RiskContributionStatus.COUNTED,
        )
        for flag in _RISK_ORDER
        if flag in flags
    )
    with localcontext(exact_decimal_context()):
        counted = sum(
            (i.deduction_pct for i in items if i.status is RiskContributionStatus.COUNTED),
            start=Decimal(0),
        )
        capped = min(counted, coefficients.risk_deduction_cap)
        return items, capped, counted - capped


# --- 組合せ ------------------------------------------------------------------------------------


def _axis_adjustments(
    inputs: RaerInputs, fv_confidence: ConfidenceLevel | None
) -> list[AxisAdjustment]:
    """5 軸の状態を入力から決める(固定順)。欠測は UNAVAILABLE(理由つき)。0 や中立値で補完しない。"""
    quality = inputs.quality
    if quality is None:
        quality_adj = AxisAdjustment(
            AxisCode.COMPANY_QUALITY, AxisStatus.UNAVAILABLE, (AxisReasonCode.INPUT_MISSING,)
        )
    elif quality.coverage_ratio < quality.min_coverage:
        quality_adj = AxisAdjustment(
            AxisCode.COMPANY_QUALITY,
            AxisStatus.UNAVAILABLE,
            (AxisReasonCode.COVERAGE_BELOW_MINIMUM,),
        )
    else:
        quality_adj = AxisAdjustment(AxisCode.COMPANY_QUALITY, AxisStatus.APPLIED)
    trend = inputs.operating_trend
    if trend is None:
        trend_adj = AxisAdjustment(
            AxisCode.EARNINGS_TREND, AxisStatus.UNAVAILABLE, (AxisReasonCode.INPUT_MISSING,)
        )
    elif isinstance(trend, TrendUnavailable):
        trend_adj = AxisAdjustment(AxisCode.EARNINGS_TREND, trend.status, (trend.reason,))
    else:
        trend_adj = AxisAdjustment(AxisCode.EARNINGS_TREND, AxisStatus.APPLIED)
    if fv_confidence is None:
        fv_adj = AxisAdjustment(
            AxisCode.FAIR_VALUE_CONFIDENCE, AxisStatus.UNAVAILABLE, (AxisReasonCode.INPUT_MISSING,)
        )
    else:
        fv_adj = AxisAdjustment(AxisCode.FAIR_VALUE_CONFIDENCE, AxisStatus.APPLIED)
    if inputs.risk_flags is None:
        risk_adj = AxisAdjustment(
            AxisCode.BUSINESS_RISK,
            AxisStatus.UNAVAILABLE,
            (AxisReasonCode.RISK_FLAGS_NOT_OBSERVED,),
        )
    else:
        risk_adj = AxisAdjustment(AxisCode.BUSINESS_RISK, AxisStatus.APPLIED)
    data_quality_adj = AxisAdjustment(
        AxisCode.DATA_QUALITY, AxisStatus.UNAVAILABLE, (AxisReasonCode.AXIS_NOT_IMPLEMENTED,)
    )
    by_axis = {
        AxisCode.FAIR_VALUE_CONFIDENCE: fv_adj,
        AxisCode.COMPANY_QUALITY: quality_adj,
        AxisCode.BUSINESS_RISK: risk_adj,
        AxisCode.DATA_QUALITY: data_quality_adj,
        AxisCode.EARNINGS_TREND: trend_adj,
    }
    return [by_axis[axis] for axis in AXIS_ORDER]


def compose_raer(
    *, evaluation_date: date, inputs: RaerInputs, coefficients: RaerCoefficients
) -> RaerEvaluation:
    """承認済みの暫定式で RAER を作る(純粋関数)。値を作れないときは、理由つきの UNAVAILABLE。

    5 軸の状態と理由は、算出不能でも必ず記録する。範囲外・非有限・型の誤りは ValueError。
    """
    if not isinstance(inputs, RaerInputs) or not isinstance(coefficients, RaerCoefficients):
        raise ValueError(
            "inputs は RaerInputs、coefficients は RaerCoefficients でなければならない"
        )
    expected_return = inputs.expected_return
    if evaluation_date != expected_return.evaluation_date:
        raise ValueError("RAER の評価日は Expected Return の評価日と一致しなければならない")
    fv_confidence = expected_return.fair_value_confidence
    if fv_confidence is not None and not isinstance(fv_confidence, ConfidenceLevel):
        raise ValueError("fair_value_confidence は ConfidenceLevel でなければならない")
    flags: frozenset[RiskFlag] | None = None
    if inputs.risk_flags is not None:
        flags = frozenset(inputs.risk_flags)
        if any(not isinstance(f, RiskFlag) for f in flags):
            raise ValueError("risk_flags は RiskFlag の語彙だけ")
    if inputs.operating_trend is not None and not isinstance(
        inputs.operating_trend, (OperatingTrendScore, TrendUnavailable)
    ):
        raise ValueError("operating_trend は OperatingTrendScore / TrendUnavailable / None")
    if inputs.quality is not None and not isinstance(inputs.quality, QualityInput):
        raise ValueError("quality は QualityInput / None")

    adjustments = _axis_adjustments(inputs, fv_confidence)
    applied = {a.axis for a in adjustments if a.status is AxisStatus.APPLIED}
    risk_flags_out = (
        flags if (flags is not None and AxisCode.BUSINESS_RISK in applied) else frozenset()
    )

    # 土台(A-2): どちらかが不明なら算出不能。片方だけで合計しない
    upside_value = expected_return.upside.value
    income_value = expected_return.income.value
    reasons: list[RaerReasonCode] = []
    if upside_value is None:
        reasons.append(RaerReasonCode.EXPECTED_RETURN_UPSIDE_UNDETERMINED)
    if income_value is None:
        reasons.append(RaerReasonCode.EXPECTED_RETURN_INCOME_UNDETERMINED)
    if reasons:
        reasons.append(RaerReasonCode.BASE_UNDETERMINED)
    # 必須軸(A-3)と最低軸数(A-3)
    if not coefficients.required_axes <= applied:
        reasons.append(RaerReasonCode.REQUIRED_AXIS_MISSING)
    if len(applied) < coefficients.min_applied_axes:
        reasons.append(RaerReasonCode.AXES_BELOW_MINIMUM)

    def build(composition: RaerComposition | None) -> RaerEvaluation:
        return RaerEvaluation(
            model_version=COMBINED_MODEL_VERSION,
            evaluation_date=evaluation_date,
            expected_return=expected_return,
            axes=tuple(adjustments),
            risk_flags=risk_flags_out,
            risk_flags_available=AxisCode.BUSINESS_RISK in applied,
            reasons=() if composition is not None else tuple(reasons),
            composition=composition,
        )

    if reasons:
        return build(None)

    assert upside_value is not None and income_value is not None  # for type narrowing
    upside = to_decimal(upside_value, "upside")
    income = to_decimal(income_value, "income")
    contributions: list[AxisContribution] = []
    risk_items: tuple[RiskItem, ...] = ()
    cap_cut = Decimal(0)
    provenance: list[tuple[str, str]] = [("coefficient_set_id", coefficients.coefficient_set_id)]
    with localcontext(exact_decimal_context()):
        base = upside + income
        if AxisCode.FAIR_VALUE_CONFIDENCE in applied:
            assert fv_confidence is not None
            factor = coefficients.fv_confidence_factor[fv_confidence]
            discount = max(upside, Decimal(0)) * (Decimal(1) - factor)
            contributions.append(
                AxisContribution(
                    AxisCode.FAIR_VALUE_CONFIDENCE,
                    discount,
                    (("fv_confidence", fv_confidence.value), ("factor", _text(factor))),
                )
            )
        if AxisCode.COMPANY_QUALITY in applied:
            quality = inputs.quality
            assert quality is not None
            deduction = coefficients.quality_deduction_max * (
                Decimal(1) - quality.holding_quality_score_0_50 / coefficients.quality_max_score
            )
            contributions.append(
                AxisContribution(
                    AxisCode.COMPANY_QUALITY,
                    deduction,
                    (
                        ("quality_score_0_50", _text(quality.holding_quality_score_0_50)),
                        ("coverage_ratio", _text(quality.coverage_ratio)),
                        ("min_coverage", _text(quality.min_coverage)),
                        (
                            "company_quality_model_version",
                            quality.company_quality_model_version or "",
                        ),
                    ),
                )
            )
        if AxisCode.BUSINESS_RISK in applied:
            assert flags is not None
            risk_items, risk_total, cap_cut = _risk_items(flags, coefficients)
            contributions.append(
                AxisContribution(
                    AxisCode.BUSINESS_RISK,
                    risk_total,
                    (("risk_deduction_cap", _text(coefficients.risk_deduction_cap)),),
                )
            )
        if AxisCode.EARNINGS_TREND in applied:
            trend = inputs.operating_trend
            assert isinstance(trend, OperatingTrendScore)
            deduction = (
                coefficients.trend_deduction_scale * max(Decimal(0), -trend.value) / _HUNDRED
            )
            contributions.append(
                AxisContribution(
                    AxisCode.EARNINGS_TREND,
                    deduction,
                    (
                        ("operating_trend_score", _text(trend.value)),
                        (
                            "components_used",
                            ",".join(sorted(c.value for c in trend.components_used)),
                        ),
                        ("partial_coverage", _text(trend.partial_coverage)),
                        ("min_coverage_required", _text(trend.min_coverage_required)),
                        ("weight_operating_income", _text(trend.weights.operating_income)),
                        ("weight_operating_cashflow", _text(trend.weights.operating_cashflow)),
                        ("weight_dividend_direction", _text(trend.weights.dividend_direction)),
                        ("weight_acceleration", _text(trend.weights.acceleration)),
                        ("earnings_trend_model_version", trend.earnings_trend_model_version or ""),
                    ),
                )
            )
        total = sum((c.pct_points for c in contributions), start=Decimal(0))
        value = base - total
    state = RaerState.AVAILABLE if len(applied) == len(AXIS_ORDER) else RaerState.PROVISIONAL
    assert fv_confidence is not None
    composition = RaerComposition(
        upside_pct=upside,
        income_pct=income,
        base_pct=base,
        value=value,
        contributions=tuple(contributions),
        risk_items=risk_items,
        risk_cap_cut_pct=cap_cut,
        spec_flags=frozenset({SpecFlag.HORIZON_MISMATCH_PROVISIONAL}),
        coefficient_set_id=coefficients.coefficient_set_id,
        confidence=resolve_raer_confidence(fv_confidence, state=state, applied_axes=len(applied)),
        state=state,
        provenance=tuple(provenance),
    )
    return build(composition)
