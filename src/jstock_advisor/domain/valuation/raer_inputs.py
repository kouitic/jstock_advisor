"""RAER 入力の組み立て(Issue #602 PR-G1。dormant)。

#128 R1(資金配分 Shadow)の入力の供給元。**永続済みの BUY 推奨(Recommendation)の事実**から、
`RaerInputs` と、その導出の記録(`InputProvenance`)を作る純粋関数。

USER 承認: #122 issuecomment-6138622792【3】(RAER 入力の供給元を最優先で解消する。既存の評価基盤を
再利用する。新規の同義スコアを作らない。導出不能なら代替値を作らず算出不能として記録する)。
設計 = #602 issuecomment-6138720448。

## この module が『しない』こと

  ・新しい採点・閾値・係数・重みを作らない(重み・下限は呼び出し側が既存の config から渡す)
  ・欠測を 0 や中立値で補完しない(導出できない軸は UNAVAILABLE。理由を provenance に残す)
  ・時計・営業日・I/O・永続化を持たない(`evaluation_date` と価格鮮度の判定結果は呼び出し側が渡す)
  ・Recommendation へ書き込まない(`extra="forbid"` のため、書き込みの追加は旧版への rollback を壊す)
  ・どこからも import されない(配線なし)。保有側の入力は作らない(永続されていない。#602 PR-C / D)

## 4 つの入力の出所(すべて既存の記録)

  ・リスクの印    = `margin_adjustments` のコードのうち RiskFlag の 6 語に一致するもの
                    (買い判定が検出済み。安全余裕率が算出されなかった推奨は『観測なし』= None。
                    空の集合 = リスクなし、とは別)
  ・企業品質      = `buy_score_input_facts["common_quality_shadow"]`
                    (保有判断と同じ `score_company_quality()`。
                    元から 0〜50 点・coverage_ratio つき。
                    100 点の v1 `company_quality_score` は使わない)
  ・営業トレンド点 = `earnings_trend_metrics` の営業利益・営業 CF の 2 成分
                    + 既存の earnings_trend_rules
  ・比較の鍵      = `RAER_FULL_COMPARISON_KEY`(全 4 軸が適用のときの鍵。軸 4 は未実装)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

from jstock_advisor.config.models import EarningsTrendRulesConfig
from jstock_advisor.domain.entities.enums import ConfidenceLevel, EarningsTrendEvaluationState
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.valuation import FairValueUnusableReasonCode
from jstock_advisor.domain.price_freshness import PriceFreshnessVerdict
from jstock_advisor.domain.valuation.expected_return import (
    ExpectedReturnResult,
    FairValueInput,
    IncomeInput,
    compute_expected_return,
)
from jstock_advisor.domain.valuation.raer import (
    COMBINED_MODEL_VERSION,
    AxisCode,
    RaerEvaluation,
    RaerResult,
    RiskFlag,
    make_comparison_key,
    to_raer_result,
)
from jstock_advisor.domain.valuation.raer_composition import (
    OperatingTrendScore,
    QualityInput,
    RaerCoefficients,
    RaerInputs,
    TrendUnavailable,
    TrendWeights,
    compose_raer,
    to_decimal,
    trend_score_for_raer,
)
from jstock_advisor.domain.valuation.yield_calc import BenefitProgramState

#: 全 4 軸(軸 4 = DATA_QUALITY は未実装)が適用されたときの比較の鍵。literal で書かない。
#: 配分の方針の comparison_key(allocation_policy.yaml。PR-G2 で設定)と一致することを
#: 契約テストで固定する。
RAER_FULL_COMPARISON_KEY: Final = make_comparison_key(
    COMBINED_MODEL_VERSION,
    (
        AxisCode.FAIR_VALUE_CONFIDENCE,
        AxisCode.COMPANY_QUALITY,
        AxisCode.BUSINESS_RISK,
        AxisCode.EARNINGS_TREND,
    ),
)

#: 推奨の `buy_score_input_facts` の中の、共有 Common Quality の観測のキー。
COMMON_QUALITY_FACTS_KEY: Final = "common_quality_shadow"
#: 観測が算出済みであることを表す状態値。生産側 `services.buy_signal_service.SHADOW_STATE_COMPUTED`
#: と同じ値(domain は services を import できないため、契約テストで一致を固定する)。
SHADOW_STATE_COMPUTED: Final = "COMPUTED"

_RISK_FLAG_VALUES: Final = frozenset(flag.value for flag in RiskFlag)


class ProvenanceItem(StrEnum):
    """導出の記録の対象(土台の 2 つと軸 4 つ。軸 4 = DATA_QUALITY は未実装で記録しない)。"""

    UPSIDE = "UPSIDE"
    INCOME = "INCOME"
    FAIR_VALUE_CONFIDENCE = "FAIR_VALUE_CONFIDENCE"
    COMPANY_QUALITY = "COMPANY_QUALITY"
    BUSINESS_RISK = "BUSINESS_RISK"
    EARNINGS_TREND = "EARNINGS_TREND"


class ProvenanceReason(StrEnum):
    """導出できなかった(または導出を弱めた)理由。数値を持たない。"""

    SOURCE_MISSING = "SOURCE_MISSING"  # 出所の記録が無い(キー欠測・None・空)
    SOURCE_COMPUTATION_FAILED = "SOURCE_COMPUTATION_FAILED"  # 生産側の観測が算出に失敗していた
    SOURCE_NOT_EVALUATED = "SOURCE_NOT_EVALUATED"  # 生産側が評価不能・対象外・充足度不足と記録した
    INVALID_VALUE = "INVALID_VALUE"  # 値が契約の範囲外・型の誤り・未知の語(補正せず軸を使わない)
    MARGIN_NOT_COMPUTED = "MARGIN_NOT_COMPUTED"  # 安全余裕率が算出されず、リスクを観測していない
    FV_USABILITY_UNKNOWN = "FV_USABILITY_UNKNOWN"  # Fair Value が使えるか不明(旧い記録)
    INCONSISTENT_YIELD_RECORD = "INCONSISTENT_YIELD_RECORD"  # 利回りの永続値が互いに矛盾する


class ProvenanceNote(StrEnum):
    """導出の注記(値は変えない)。"""

    BENEFIT_STATE_DERIVED_FROM_YIELDS = "BENEFIT_STATE_DERIVED_FROM_YIELDS"
    BENEFIT_STATE_NOT_DERIVABLE = "BENEFIT_STATE_NOT_DERIVABLE"


@dataclass(frozen=True)
class ProvenanceEntry:
    """1 項目の導出の記録。値は持たない(値は RaerInputs が持つ)。

    `derived` = 出所から入力を導けた。軸が適用されるか(充足度の下限など)は `compose_raer` が決める。
    """

    item: ProvenanceItem
    source: str  # 永続済みの出所(フィールド名 / 辞書のキー)
    derived: bool  # False = 導出できず、その項目は使われない(UNAVAILABLE / 観測なし)
    reason: ProvenanceReason | None = None
    notes: tuple[ProvenanceNote, ...] = ()

    def __post_init__(self) -> None:
        if self.derived and self.reason is not None:
            raise ValueError("導出できた項目は理由を持たない")
        if not self.derived and self.reason is None:
            raise ValueError("導出できない項目は理由が要る")


@dataclass(frozen=True)
class InputProvenance:
    """RaerInputs の各項目をどの永続済みの記録から導いたか、導けなかった理由は何か。"""

    entries: tuple[ProvenanceEntry, ...]

    def entry(self, item: ProvenanceItem) -> ProvenanceEntry:
        for candidate in self.entries:
            if candidate.item is item:
                return candidate
        raise KeyError(item)


@dataclass(frozen=True)
class AssembledRaerInputs:
    inputs: RaerInputs
    provenance: InputProvenance


@dataclass(frozen=True)
class CandidateRaerFacts:
    """推奨から読む事実だけを持つ最小の入力。

    フィールド名への依存は `facts_from_recommendation` に閉じる。
    """

    price: Decimal | None
    fair_value_neutral: Decimal | None
    fair_value_confidence: ConfidenceLevel | None
    fair_value_usable: bool | None
    fair_value_unusable_reason: str | None
    dividend_yield_pct: float | None
    benefit_yield_pct: float | None
    total_yield_pct: float | None
    margin_computed: bool
    margin_adjustment_codes: tuple[str, ...]
    common_quality: Mapping[str, object] | None
    earnings_trend_metrics: Mapping[str, object] | None


@dataclass(frozen=True)
class AssemblerPolicy:
    """既存の config の値(新しい閾値・重みを作らない)。呼び出し側が既存の config から作る。"""

    company_quality_minimum: Decimal  # coverage_thresholds.company_quality_minimum
    trend_weights: TrendWeights  # earnings_trend_rules の 4 つの重み
    trend_min_coverage_required: Decimal  # earnings_trend_rules.min_coverage_required
    trend_model_version: str | None = None  # earnings_trend_rules.model_version

    @classmethod
    def from_rules(
        cls, *, company_quality_minimum: float, trend_rules: EarningsTrendRulesConfig
    ) -> AssemblerPolicy:
        return cls(
            company_quality_minimum=to_decimal(company_quality_minimum, "company_quality_minimum"),
            trend_weights=TrendWeights(
                operating_income=to_decimal(trend_rules.operating_income_trend_weight),
                operating_cashflow=to_decimal(trend_rules.operating_cashflow_trend_weight),
                dividend_direction=to_decimal(trend_rules.dividend_direction_weight),
                acceleration=to_decimal(trend_rules.acceleration_weight),
            ),
            trend_min_coverage_required=to_decimal(
                trend_rules.min_coverage_required, "min_coverage_required"
            ),
            trend_model_version=trend_rules.model_version,
        )


def facts_from_recommendation(rec: Recommendation) -> CandidateRaerFacts:
    """永続済みの推奨から、RAER の入力の事実を取り出す。フィールド名への依存はここだけ。"""
    score_facts = rec.buy_score_input_facts
    common_quality = score_facts.get(COMMON_QUALITY_FACTS_KEY) if score_facts is not None else None
    trend = rec.earnings_trend_metrics
    return CandidateRaerFacts(
        price=rec.price_at_recommendation,
        fair_value_neutral=rec.fair_value_neutral,
        fair_value_confidence=rec.fair_value_overall_confidence,
        fair_value_usable=rec.fair_value_usable_for_trading_judgment,
        fair_value_unusable_reason=rec.fair_value_unusable_reason_code,
        dividend_yield_pct=rec.dividend_yield_pct_at_recommendation,
        benefit_yield_pct=rec.shareholder_benefit_yield_pct_at_recommendation,
        total_yield_pct=rec.total_yield_pct_at_recommendation,
        margin_computed=rec.required_margin_of_safety_entry is not None,
        margin_adjustment_codes=tuple(adjustment.code for adjustment in rec.margin_adjustments),
        common_quality=common_quality if isinstance(common_quality, Mapping) else None,
        earnings_trend_metrics=trend if trend else None,
    )


# --- 土台(U / I と軸 1)--------------------------------------------------------------------------


def _derive_benefit_state(
    dividend: float | None, benefit: float | None, total: float | None
) -> tuple[BenefitProgramState, float | None, tuple[ProvenanceNote, ...], ProvenanceReason | None]:
    """永続済みの 3 つの利回りから、#55 の真理値表を逆に読んで優待の状態を導く。

    `benefit_state` は永続されていない。総合利回りは `compute_total_yield_pct` の結果なので、
      優待利回りあり                          → VALUED
      優待利回りなし かつ 総合利回りあり       → NO_PROGRAM(A / G: 総合 = 配当のみ)
      優待利回りなし かつ 総合なし かつ 配当あり → UNVALUABLE(C)
      3 つとも無し                            → 導けない(配当が不明で I は値を持たない。結果は同じ)
    総合があるのに配当が無い記録は矛盾(総合が決まるには配当が要る)。配当を不明として扱い、理由を残す。
    """
    derived = (ProvenanceNote.BENEFIT_STATE_DERIVED_FROM_YIELDS,)
    if benefit is not None:
        return BenefitProgramState.VALUED, dividend, derived, None
    if total is not None:
        if dividend is None:
            return (
                BenefitProgramState.NO_PROGRAM,
                None,
                derived,
                ProvenanceReason.INCONSISTENT_YIELD_RECORD,
            )
        return BenefitProgramState.NO_PROGRAM, dividend, derived, None
    if dividend is not None:
        return BenefitProgramState.UNVALUABLE, dividend, derived, None
    return (
        BenefitProgramState.NO_PROGRAM,
        None,
        (ProvenanceNote.BENEFIT_STATE_NOT_DERIVABLE,),
        None,
    )


def _fair_value_input(facts: CandidateRaerFacts) -> tuple[FairValueInput, ProvenanceReason | None]:
    """Fair Value の入力。使えるか不明(None)なら、使えると仮定せず Fair Value 全体を欠測にする。"""
    if facts.fair_value_usable is None:
        return (
            FairValueInput(
                neutral=None,
                usable_for_trading_judgment=False,
                confidence=facts.fair_value_confidence,
            ),
            ProvenanceReason.FV_USABILITY_UNKNOWN,
        )
    reason: FairValueUnusableReasonCode | None = None
    if facts.fair_value_unusable_reason is not None:
        try:
            reason = FairValueUnusableReasonCode(facts.fair_value_unusable_reason)
        except ValueError:
            reason = None  # 未知の語は補正しない(使えないこと自体は fair_value_usable が表す)
    return (
        FairValueInput(
            neutral=facts.fair_value_neutral,
            usable_for_trading_judgment=facts.fair_value_usable,
            unusable_reason_code=reason,
            confidence=facts.fair_value_confidence,
        ),
        None,
    )


def _expected_return(
    facts: CandidateRaerFacts, evaluation_date: date, price_freshness: PriceFreshnessVerdict
) -> tuple[ExpectedReturnResult, tuple[ProvenanceEntry, ...]]:
    fair_value, fv_reason = _fair_value_input(facts)
    state, dividend, notes, yield_reason = _derive_benefit_state(
        facts.dividend_yield_pct, facts.benefit_yield_pct, facts.total_yield_pct
    )
    result = compute_expected_return(
        evaluation_date=evaluation_date,
        current_price=facts.price,
        price_freshness=price_freshness,
        fair_value=fair_value,
        income=IncomeInput(
            dividend_yield_pct=dividend,
            benefit_yield_pct=facts.benefit_yield_pct,
            benefit_state=state,
        ),
    )
    upside = result.upside.is_determined
    income = result.income.is_determined
    entries = (
        ProvenanceEntry(
            ProvenanceItem.UPSIDE,
            "price_at_recommendation / fair_value_neutral / fair_value_usable_for_trading_judgment",
            derived=upside,
            reason=None if upside else (fv_reason or ProvenanceReason.SOURCE_MISSING),
        ),
        ProvenanceEntry(
            ProvenanceItem.INCOME,
            "dividend_yield_pct_at_recommendation / shareholder_benefit_yield_pct_at_recommendation"
            " / total_yield_pct_at_recommendation",
            derived=income,
            reason=None if income else (yield_reason or ProvenanceReason.SOURCE_MISSING),
            notes=notes,
        ),
        ProvenanceEntry(
            ProvenanceItem.FAIR_VALUE_CONFIDENCE,
            "fair_value_overall_confidence",
            derived=result.fair_value_confidence is not None,
            reason=(
                None
                if result.fair_value_confidence is not None
                else ProvenanceReason.SOURCE_MISSING
            ),
        ),
    )
    return result, entries


# --- 軸 2 企業品質 --------------------------------------------------------------------------------

_QUALITY_SOURCE: Final = f"buy_score_input_facts.{COMMON_QUALITY_FACTS_KEY}"


def _quality(
    facts: CandidateRaerFacts, policy: AssemblerPolicy
) -> tuple[QualityInput | None, ProvenanceEntry]:
    shadow = facts.common_quality
    if shadow is None:
        return None, ProvenanceEntry(
            ProvenanceItem.COMPANY_QUALITY,
            _QUALITY_SOURCE,
            derived=False,
            reason=ProvenanceReason.SOURCE_MISSING,
        )
    if shadow.get("shadow_state") != SHADOW_STATE_COMPUTED:
        failed = shadow.get("shadow_state") is not None
        return None, ProvenanceEntry(
            ProvenanceItem.COMPANY_QUALITY,
            _QUALITY_SOURCE,
            derived=False,
            reason=(
                ProvenanceReason.SOURCE_COMPUTATION_FAILED
                if failed
                else ProvenanceReason.SOURCE_MISSING
            ),
        )
    try:
        quality = QualityInput(
            holding_quality_score_0_50=to_decimal(shadow.get("score"), "score"),
            coverage_ratio=to_decimal(shadow.get("coverage_ratio"), "coverage_ratio"),
            min_coverage=policy.company_quality_minimum,
        )
    except ValueError:
        return None, ProvenanceEntry(
            ProvenanceItem.COMPANY_QUALITY,
            _QUALITY_SOURCE,
            derived=False,
            reason=ProvenanceReason.INVALID_VALUE,
        )
    return quality, ProvenanceEntry(ProvenanceItem.COMPANY_QUALITY, _QUALITY_SOURCE, derived=True)


# --- 軸 3 事業・業種リスク ------------------------------------------------------------------------

_RISK_SOURCE: Final = "margin_adjustments / required_margin_of_safety_entry"


def _risk_flags(
    facts: CandidateRaerFacts,
) -> tuple[frozenset[RiskFlag] | None, ProvenanceEntry]:
    if not facts.margin_computed:
        return None, ProvenanceEntry(
            ProvenanceItem.BUSINESS_RISK,
            _RISK_SOURCE,
            derived=False,
            reason=ProvenanceReason.MARGIN_NOT_COMPUTED,
        )
    flags = frozenset(
        RiskFlag(code) for code in facts.margin_adjustment_codes if code in _RISK_FLAG_VALUES
    )
    return flags, ProvenanceEntry(ProvenanceItem.BUSINESS_RISK, _RISK_SOURCE, derived=True)


# --- 軸 5 営業トレンド ----------------------------------------------------------------------------

_TREND_SOURCE: Final = "earnings_trend_metrics"


def _trend(
    facts: CandidateRaerFacts, policy: AssemblerPolicy
) -> tuple[OperatingTrendScore | TrendUnavailable | None, ProvenanceEntry]:
    metrics = facts.earnings_trend_metrics
    if metrics is None:
        return None, ProvenanceEntry(
            ProvenanceItem.EARNINGS_TREND,
            _TREND_SOURCE,
            derived=False,
            reason=ProvenanceReason.SOURCE_MISSING,
        )
    state_raw = metrics.get("state")
    if state_raw is not None and not isinstance(state_raw, str):
        return None, ProvenanceEntry(
            ProvenanceItem.EARNINGS_TREND,
            _TREND_SOURCE,
            derived=False,
            reason=ProvenanceReason.INVALID_VALUE,
        )
    if state_raw is None:
        failed = metrics.get("shadow_state") is not None
        return None, ProvenanceEntry(
            ProvenanceItem.EARNINGS_TREND,
            _TREND_SOURCE,
            derived=False,
            reason=(
                ProvenanceReason.SOURCE_COMPUTATION_FAILED
                if failed
                else ProvenanceReason.SOURCE_MISSING
            ),
        )
    try:
        result = trend_score_for_raer(
            evaluation_state=EarningsTrendEvaluationState(state_raw),
            operating_income_component=metrics.get("operating_income_trend_component"),
            operating_cashflow_component=metrics.get("operating_cashflow_trend_component"),
            weights=policy.trend_weights,
            min_coverage_required=policy.trend_min_coverage_required,
            earnings_trend_model_version=policy.trend_model_version,
        )
    except ValueError:
        return None, ProvenanceEntry(
            ProvenanceItem.EARNINGS_TREND,
            _TREND_SOURCE,
            derived=False,
            reason=ProvenanceReason.INVALID_VALUE,
        )
    applied = isinstance(result, OperatingTrendScore)
    return result, ProvenanceEntry(
        ProvenanceItem.EARNINGS_TREND,
        _TREND_SOURCE,
        derived=applied,
        reason=None if applied else ProvenanceReason.SOURCE_NOT_EVALUATED,
    )


# --- 組み立て -------------------------------------------------------------------------------------


def assemble_raer_inputs(
    facts: CandidateRaerFacts,
    *,
    evaluation_date: date,
    price_freshness: PriceFreshnessVerdict,
    policy: AssemblerPolicy,
) -> AssembledRaerInputs:
    """推奨の事実から `RaerInputs` と導出の記録を作る。軸ごとの失敗は他の軸に影響しない。

    例外を投げない(入力の不正は該当の軸を UNAVAILABLE にし、provenance に理由を残す)。
    """
    expected_return, base_entries = _expected_return(facts, evaluation_date, price_freshness)
    quality, quality_entry = _quality(facts, policy)
    risk_flags, risk_entry = _risk_flags(facts)
    trend, trend_entry = _trend(facts, policy)
    provenance = InputProvenance(
        entries=(*base_entries, quality_entry, risk_entry, trend_entry),
    )
    return AssembledRaerInputs(
        inputs=RaerInputs(
            expected_return=expected_return,
            quality=quality,
            risk_flags=risk_flags,
            operating_trend=trend,
        ),
        provenance=provenance,
    )


def raer_result_for_candidate(
    facts: CandidateRaerFacts,
    *,
    evaluation_date: date,
    price_freshness: PriceFreshnessVerdict,
    policy: AssemblerPolicy,
    coefficients: RaerCoefficients,
) -> tuple[RaerResult, RaerEvaluation, InputProvenance]:
    """assemble → compose_raer → to_raer_result。#603 PR-F1 へは RaerResult を渡す。"""
    assembled = assemble_raer_inputs(
        facts, evaluation_date=evaluation_date, price_freshness=price_freshness, policy=policy
    )
    evaluation = compose_raer(
        evaluation_date=evaluation_date, inputs=assembled.inputs, coefficients=coefficients
    )
    return to_raer_result(evaluation), evaluation, assembled.provenance


__all__ = [
    "RAER_FULL_COMPARISON_KEY",
    "AssembledRaerInputs",
    "AssemblerPolicy",
    "CandidateRaerFacts",
    "InputProvenance",
    "ProvenanceEntry",
    "ProvenanceItem",
    "ProvenanceNote",
    "ProvenanceReason",
    "assemble_raer_inputs",
    "facts_from_recommendation",
    "raer_result_for_candidate",
]
