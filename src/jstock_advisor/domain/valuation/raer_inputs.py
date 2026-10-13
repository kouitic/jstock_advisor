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
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

from jstock_advisor.config.models import EarningsTrendRulesConfig
from jstock_advisor.domain.entities.enums import (
    BuyIndustrySector,
    ConfidenceLevel,
    EarningsTrendEvaluationState,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.valuation import FairValueUnusableReasonCode
from jstock_advisor.domain.price_freshness import PriceFreshnessVerdict
from jstock_advisor.domain.valuation.expected_return import (
    ComponentResult,
    ExpectedReturnResult,
    FairValueInput,
    IncomeInput,
    ReasonCode,
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
#: ★ この鍵は『軸 4 が未実装であること』に依存する。軸 4 を実装すると適用軸の集合が変わり、
#: 鍵も変わる(旧い記録と比較できなくなる)。軸 4 の追加はモデル版の更新として扱うこと。
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

#: 推奨の `buy_score_input_facts` の中の、リスクの印の『検出の記録』のキー(#602 H2。買い判定が
#: 検出済みの語と、語ごとの観測の有無を写す)。生産側(buy_signal_service)は domain の raer 系
#: module を import できない(#606 の到達性の契約)ため、同じ literal を持ち、契約テストで
#: 一致を固定する。
RISK_DETECTION_FACTS_KEY: Final = "raer_risk_detection"
RISK_DETECTION_VERSION: Final = 1
_OBSERVED_KEYS: Final = ("earnings_date", "liquidity", "industry", "eps")

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
    TOTAL_YIELD_MISMATCH = "TOTAL_YIELD_MISMATCH"  # 再計算した総合利回りが永続値と一致しない


class ProvenanceNote(StrEnum):
    """導出の注記(値は変えない)。"""

    BENEFIT_STATE_DERIVED_FROM_YIELDS = "BENEFIT_STATE_DERIVED_FROM_YIELDS"
    BENEFIT_STATE_NOT_DERIVABLE = "BENEFIT_STATE_NOT_DERIVABLE"
    # 買い側の安全余裕率の category dedupe で superseded になった code(RAER は別の除外規則を持つ
    # ので、検出された全部を渡す)を含めた。景気業種 + 大口顧客依存は RAER では両方数える(承認済み)
    SUPERSEDED_CODES_INCLUDED = "SUPERSEDED_CODES_INCLUDED"
    # 買い側の検出は『不明 → 該当しない』(買い判定の既存の挙動)なので、候補の『リスクなし』には
    # 『観測できなかった語を該当なしとして扱った』ものが含まれうる。R1 の既知の限界で、候補側の
    # 未観測を区別するには買い判定の記録へ観測の有無を足す別の変更が要る(R2 以降)。
    # 保有側(PR-G3)は未観測を区別する(非対称)。観測できる形で必ず残す
    CANDIDATE_UNOBSERVED_TREATED_AS_NOT_FLAGGED = "CANDIDATE_UNOBSERVED_TREATED_AS_NOT_FLAGGED"
    # 検出の記録(H2)があり、6 語すべてを観測できた(『リスクなし』は確認したうえでの値)
    RISK_ALL_WORDS_OBSERVED = "RISK_ALL_WORDS_OBSERVED"
    # 軸 2 の充足度の下限は、買い側が意図的に保留している(shadow calibration で決める)ため、
    # 保有側の既存の値(coverage_thresholds.company_quality_minimum)を借用している
    COVERAGE_MINIMUM_BORROWED_FROM_HOLDING_SIDE = "COVERAGE_MINIMUM_BORROWED_FROM_HOLDING_SIDE"


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
    price_as_of_date: date | None
    buy_industry_sector: BuyIndustrySector | None
    fair_value_neutral: Decimal | None
    fair_value_confidence: ConfidenceLevel | None
    fair_value_usable: bool | None
    fair_value_unusable_reason: str | None
    dividend_yield_pct: float | None
    benefit_yield_pct: float | None
    total_yield_pct: float | None
    margin_computed: bool
    margin_adjustment_codes: tuple[str, ...]
    margin_superseded_codes: tuple[str, ...]
    risk_detection: Mapping[str, object] | None
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
    risk_detection = score_facts.get(RISK_DETECTION_FACTS_KEY) if score_facts is not None else None
    trend = rec.earnings_trend_metrics
    return CandidateRaerFacts(
        price=rec.price_at_recommendation,
        price_as_of_date=rec.price_as_of_date,
        buy_industry_sector=rec.buy_industry_sector,
        fair_value_neutral=rec.fair_value_neutral,
        fair_value_confidence=rec.fair_value_overall_confidence,
        fair_value_usable=rec.fair_value_usable_for_trading_judgment,
        fair_value_unusable_reason=rec.fair_value_unusable_reason_code,
        dividend_yield_pct=rec.dividend_yield_pct_at_recommendation,
        benefit_yield_pct=rec.shareholder_benefit_yield_pct_at_recommendation,
        total_yield_pct=rec.total_yield_pct_at_recommendation,
        margin_computed=rec.required_margin_of_safety_entry is not None,
        margin_adjustment_codes=tuple(adjustment.code for adjustment in rec.margin_adjustments),
        margin_superseded_codes=tuple(
            adjustment.code
            for adjustment in rec.margin_adjustments
            if adjustment.superseded_by is not None
        ),
        risk_detection=risk_detection if isinstance(risk_detection, Mapping) else None,
        common_quality=common_quality if isinstance(common_quality, Mapping) else None,
        earnings_trend_metrics=trend if trend else None,
    )


# --- 土台(U / I と軸 1)--------------------------------------------------------------------------


def _derive_benefit_state(
    dividend: float | None, benefit: float | None, total: float | None
) -> tuple[BenefitProgramState, tuple[ProvenanceNote, ...]]:
    """永続済みの 3 つの利回りから、#55 の真理値表を逆に読んで優待の状態を『出力同値』で導く。

    ★ これは優待の真の状態の復元ではない。`benefit_state` は永続されていないため、INCOME の出力
    (値と理由コード)が同じになる状態のうち 1 つを選ぶだけである。特に UNVALUABLE と
    『VALUED だが優待利回りが欠測』は同じ ComponentResult を返す(契約テストで固定。将来この 2 つの
    扱いが分かれたら、テストが落ちて逆算の見直しに気づける)。

      優待利回りあり                          → VALUED
      優待利回りなし かつ 総合利回りあり       → NO_PROGRAM(A / G: 総合 = 配当のみ)
      優待利回りなし かつ 総合なし かつ 配当あり → UNVALUABLE(C。VALUED + 欠測と出力同値)
      3 つとも無し                            → 導けない(配当が不明で I は値を持たない。結果は同じ)

    逆算した状態は、必ず永続された総合利回りと突き合わせる(`_check_total_yield`)。
    """
    derived = (ProvenanceNote.BENEFIT_STATE_DERIVED_FROM_YIELDS,)
    if benefit is not None:
        return BenefitProgramState.VALUED, derived
    if total is not None:
        return BenefitProgramState.NO_PROGRAM, derived
    if dividend is not None:
        return BenefitProgramState.UNVALUABLE, derived
    return BenefitProgramState.NO_PROGRAM, (ProvenanceNote.BENEFIT_STATE_NOT_DERIVABLE,)


#: 再計算した総合利回りと永続値を突き合わせる許容(絶対値・% ポイント)。1 か所だけ。
#: 永続値は float で、同じ関数・同じ入力から作られるため通常は完全一致する。この値は
#: float の表現の揺れを吸収するだけで、投資判断の閾値ではない。
YIELD_RECORD_TOLERANCE: Final = 1e-9


def _total_yield_matches(recomputed: float | None, persisted: float | None) -> bool:
    """再計算した総合利回りが永続値と一致するか。『値が無い』どうしは一致、片方だけ無いのは不一致。"""
    if recomputed is None or persisted is None:
        return recomputed is None and persisted is None
    return abs(recomputed - persisted) <= YIELD_RECORD_TOLERANCE


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
    state, notes = _derive_benefit_state(
        facts.dividend_yield_pct, facts.benefit_yield_pct, facts.total_yield_pct
    )
    result = compute_expected_return(
        evaluation_date=evaluation_date,
        current_price=facts.price,
        price_freshness=price_freshness,
        fair_value=fair_value,
        income=IncomeInput(
            dividend_yield_pct=facts.dividend_yield_pct,
            benefit_yield_pct=facts.benefit_yield_pct,
            benefit_state=state,
        ),
    )
    # ★ 常に突き合わせる(分岐に限らない): 逆算した状態で再計算した INCOME が永続された総合利回りと
    # 一致しなければ、値を作らない(優待が総合に入っていたのに優待利回りが永続されていない記録で、
    # 黙って過小な INCOME が RAER の土台に入る経路を断つ)
    mismatch = not _total_yield_matches(result.income.value, facts.total_yield_pct)
    if mismatch:
        result = replace(
            result,
            income=ComponentResult(
                reasons=(ReasonCode.DIVIDEND_UNKNOWN,), detail="yield_record_mismatch"
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
            reason=(
                None
                if income
                else (
                    ProvenanceReason.TOTAL_YIELD_MISMATCH
                    if mismatch
                    else ProvenanceReason.SOURCE_MISSING
                )
            ),
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
    return quality, ProvenanceEntry(
        ProvenanceItem.COMPANY_QUALITY,
        _QUALITY_SOURCE,
        derived=True,
        notes=(ProvenanceNote.COVERAGE_MINIMUM_BORROWED_FROM_HOLDING_SIDE,),
    )


# --- 軸 3 事業・業種リスク ------------------------------------------------------------------------

_RISK_SOURCE: Final = "margin_adjustments / required_margin_of_safety_entry"
_DETECTION_SOURCE: Final = f"buy_score_input_facts.{RISK_DETECTION_FACTS_KEY}"


class RiskFlagSource(StrEnum):
    """RiskFlagView の出所。"""

    DETECTION_RECORD = "DETECTION_RECORD"  # 検出の記録(H2)。語ごとの観測の有無が分かる
    MARGIN_ADJUSTMENTS = "MARGIN_ADJUSTMENTS"  # 旧い記録。『不明 → 該当しない』を区別できない
    NOT_OBSERVED = "NOT_OBSERVED"  # 観測できていない(安全余裕率が算出されず、検出の記録も無い)


@dataclass(frozen=True)
class RiskFlagView:
    """推奨から読んだ、リスクの印 6 語の状態。候補にも保有銘柄にも同じ関数で作る。

    flags      = 観測できて該当した語
    unobserved = 観測できなかった語(『リスクなし』にしない)
    『観測できて該当しない語』= 6 語 − flags − unobserved

    ★ `source = MARGIN_ADJUSTMENTS` は旧い記録で、観測できなかった語を区別できない(unobserved は
    空に見えるが、それは『確認できた』ではない)。保有側のように代替値を置かない用途は、
    `observed_all` を使う。
    """

    flags: frozenset[RiskFlag]
    unobserved: frozenset[RiskFlag]
    source: RiskFlagSource

    def __post_init__(self) -> None:
        if self.flags & self.unobserved:
            raise ValueError("該当した語が観測できなかった語に含まれている")

    def observed_all(self, words: frozenset[RiskFlag] | None = None) -> bool:
        """指定した語(省略時は 6 語)をすべて観測できたと言えるか。検出の記録があるときだけ True。"""
        if self.source is not RiskFlagSource.DETECTION_RECORD:
            return False
        return not (self.unobserved & (words if words is not None else frozenset(RiskFlag)))


def _parse_detection(
    record: Mapping[str, object] | None,
) -> tuple[frozenset[RiskFlag], frozenset[RiskFlag]] | None:
    """検出の記録を (該当した語, 観測できなかった語) へ。契約に合わない形は None(補正しない)。"""
    version = None if record is None else record.get("version")
    if isinstance(version, bool) or version != RISK_DETECTION_VERSION:
        return None
    assert record is not None
    raw_flags = record.get("flags")
    observed = record.get("observed")
    if not isinstance(raw_flags, list | tuple) or not isinstance(observed, Mapping):
        return None
    if set(observed) != set(_OBSERVED_KEYS) or any(
        not isinstance(value, bool) for value in observed.values()
    ):
        return None
    if any(not isinstance(word, str) or word not in _RISK_FLAG_VALUES for word in raw_flags):
        return None
    flags = frozenset(RiskFlag(word) for word in raw_flags)
    unobserved: set[RiskFlag] = set()
    if not observed["earnings_date"]:
        unobserved |= {
            RiskFlag.EARNINGS_WITHIN_3_BUSINESS_DAYS,
            RiskFlag.EARNINGS_WITHIN_7_BUSINESS_DAYS,
        }
    if not observed["liquidity"]:
        unobserved.add(RiskFlag.SMALL_CAP_OR_LOW_LIQUIDITY)
    if not observed["industry"]:
        unobserved |= {RiskFlag.CYCLICAL_INDUSTRY, RiskFlag.MAJOR_CUSTOMER_DEPENDENCY}
    # 一時的な増益のリスクは『景気業種』のときだけ判定される。景気業種でないと確定していれば
    # 該当なしを確認できており、景気業種なら EPS の観測が要る。業種が分からなければ観測できない
    cyclical_known_false = bool(observed["industry"]) and RiskFlag.CYCLICAL_INDUSTRY not in flags
    if not (cyclical_known_false or (bool(observed["industry"]) and bool(observed["eps"]))):
        unobserved.add(RiskFlag.TEMPORARY_EARNINGS_BOOST_RISK)
    if flags & unobserved:
        return None  # 観測できなかったのに該当した語がある = 記録の矛盾。使わない
    return flags, frozenset(unobserved)


def risk_flag_view(facts: CandidateRaerFacts) -> RiskFlagView:
    """リスクの印 6 語の状態を、検出の記録(優先)または旧い記録(安全余裕率の調整)から読む。"""
    parsed = _parse_detection(facts.risk_detection)
    if parsed is not None:
        flags, unobserved = parsed
        return RiskFlagView(flags, unobserved, RiskFlagSource.DETECTION_RECORD)
    if not facts.margin_computed:
        return RiskFlagView(frozenset(), frozenset(RiskFlag), RiskFlagSource.NOT_OBSERVED)
    flags = frozenset(
        RiskFlag(code) for code in facts.margin_adjustment_codes if code in _RISK_FLAG_VALUES
    )
    return RiskFlagView(flags, frozenset(), RiskFlagSource.MARGIN_ADJUSTMENTS)


def risk_flag_view_from_recommendation(rec: Recommendation) -> RiskFlagView:
    """永続済みの推奨(候補・保有銘柄どちらも)から、リスクの印 6 語の状態を読む。"""
    return risk_flag_view(facts_from_recommendation(rec))


@dataclass(frozen=True)
class HoldingMarketView:
    """保有銘柄の市場の事実(評価額の算出に使う価格・価格時点・業種)。推奨から読むだけ。

    ★ 株数・取得単価・損益・保有 ID は持たない(推奨は銘柄単位の事実。株数は owner 別の
    保有から取る)。
    値が取れないときは None のまま(0・前回値・推定値で補わない)。価格が正でない(0 以下)ものは
    使えない価格として None にする。価格の鮮度の判定は、呼び出し側(殻)が既存の保有用の関数で行う。
    """

    price: Decimal | None
    price_as_of_date: date | None
    buy_industry_sector: BuyIndustrySector | None


def holding_market_view(facts: CandidateRaerFacts) -> HoldingMarketView:
    price = facts.price if facts.price is not None and facts.price > 0 else None
    return HoldingMarketView(
        price=price,
        price_as_of_date=facts.price_as_of_date,
        buy_industry_sector=facts.buy_industry_sector,
    )


def holding_market_view_from_recommendation(rec: Recommendation) -> HoldingMarketView:
    """永続済みの推奨から、保有銘柄の価格・価格時点・業種を読む(#603 PR-F1 / F2 へ渡す)。"""
    return holding_market_view(facts_from_recommendation(rec))


def _risk_flags(
    facts: CandidateRaerFacts,
) -> tuple[frozenset[RiskFlag] | None, ProvenanceEntry]:
    view = risk_flag_view(facts)
    if view.source is RiskFlagSource.NOT_OBSERVED:
        return None, ProvenanceEntry(
            ProvenanceItem.BUSINESS_RISK,
            _RISK_SOURCE,
            derived=False,
            reason=ProvenanceReason.MARGIN_NOT_COMPUTED,
        )
    superseded_included = any(code in _RISK_FLAG_VALUES for code in facts.margin_superseded_codes)
    if view.source is RiskFlagSource.DETECTION_RECORD:
        # 検出の記録がある: 観測できた / できなかった語を区別できる。候補側は R1 では、
        # 観測できなかった語を『該当なし』として扱う(買い判定の既存の挙動。HANAKO の判断)。
        # その事実を印として残す
        coverage_note = (
            ProvenanceNote.CANDIDATE_UNOBSERVED_TREATED_AS_NOT_FLAGGED
            if view.unobserved
            else ProvenanceNote.RISK_ALL_WORDS_OBSERVED
        )
        source = _DETECTION_SOURCE
    else:
        coverage_note = ProvenanceNote.CANDIDATE_UNOBSERVED_TREATED_AS_NOT_FLAGGED
        source = _RISK_SOURCE
    return view.flags, ProvenanceEntry(
        ProvenanceItem.BUSINESS_RISK,
        source,
        derived=True,
        notes=(coverage_note,)
        + ((ProvenanceNote.SUPERSEDED_CODES_INCLUDED,) if superseded_included else ()),
    )


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
    "RISK_DETECTION_FACTS_KEY",
    "AssembledRaerInputs",
    "AssemblerPolicy",
    "CandidateRaerFacts",
    "HoldingMarketView",
    "InputProvenance",
    "ProvenanceEntry",
    "ProvenanceItem",
    "ProvenanceNote",
    "ProvenanceReason",
    "RiskFlagSource",
    "RiskFlagView",
    "assemble_raer_inputs",
    "facts_from_recommendation",
    "holding_market_view",
    "holding_market_view_from_recommendation",
    "raer_result_for_candidate",
    "risk_flag_view",
    "risk_flag_view_from_recommendation",
]
