"""適正価格信頼度(valuation_confidence)のHIGH tier到達可否shadow計測(Issue #582)。

shadow = 判定・通知・保存を**変えず**に、「`determine_valuation_confidence()`が
`require_industry_model=False`で判定していたら何が変わるか」だけを観測する
機構である。`industry_model_applied`は#208の調査により恒久的にFalseと判明して
おり、本番のHIGH tierは一度も到達していない。本moduleは、handlerの合流点
(Recommendationの保存が完了した後)から呼ばれ、candidate側の判定を
`determine_valuation_confidence` → `compute_valuation_anchor` →
`compute_margin_of_safety` → `determine_buy_price_reliability` →
`compute_buy_price_levels` → `decide_buy_action` → `validate_buy_recommendation`
(buy_signal_service.pyのstep9〜18相当。すべて既存の純関数の再利用)で再実行し、
結果を既存の`AuditLogTable`へ`decision_type=
"valuation_confidence_shadow"`として1件記録する(judgment_safety_shadow_service.py
〔#160 PR-3〕と同型)。

サブちゃんレビュー対応(PR #704): candidate側にも`validate_buy_recommendation`
(step18の整合性検証)を適用する。production側の`actual_buy_action`は
この検証**後**の値であり、candidate側を検証前のままにすると
「検証後のactual」と「検証前のcandidate」という不揃いな比較になり、
実際には何も変わっていないケースでもbuy_action_changedが見かけ上発火する。

## 本流を変えない構造(不変条件)

* **OFFなら何もしない**: `shadow_config.enabled`がFalseなら、candidateの再計算も
  監査記録も呼ばない。設定が無い・読めない・不正な場合はOFFへ縮退する
  (`load_valuation_confidence_shadow_config`)。
* **保存の後に置く**: 呼び出し元は、Recommendationの保存が完了した後にだけ呼ぶ。
  shadowは保存内容を変えられない。
* **失敗を隔離する**: candidateの再計算と記録の**両方**を
  `isolated_shadow_computation`(S-20)の中へ入れる。例外は本流へ伝播せず、
  リトライもしない(shadowは欠落してよい)。
* **冪等**: `record_if_absent`と決定的な`audit_id`により、再試行・重複配信でも
  1件になる。
* **VALIDATIONでは記録しない**: 呼び出し元が既存のifで除外する(judgment_safety_
  shadowと同じ抑止点)。
* **Recommendation・永続schemaへ載せない**: candidateの入力(`ValuationConfidence
  ShadowInputs`)は`BuyAnalysisOutcome`の非比較・非repr fieldから読むだけで、
  handlerの中で完結する。

## 記録する内容(allowlist、USER指定6項目に対応)

保存を禁止する項目: holding_id / owner / 保有数量 / 平均取得単価 / 銘柄名 /
例外メッセージ。`stock_code`は既存の監査記録と同じ扱いで、監査記録のトップレベル
の項目として持つ。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Protocol

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.enums import BuyAction, ConfidenceLevel, EarningsDateStatus
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.valuation import FairValueRange
from jstock_advisor.domain.shadow_observation import isolated_shadow_computation
from jstock_advisor.domain.signals.buy_consistency import validate_buy_recommendation
from jstock_advisor.domain.signals.buy_decision import decide_buy_action
from jstock_advisor.domain.signals.valuation_confidence_shadow_config import (
    ValuationConfidenceShadowConfig,
    load_valuation_confidence_shadow_config,
)
from jstock_advisor.domain.valuation.buy_price_levels import compute_buy_price_levels
from jstock_advisor.domain.valuation.buy_price_reliability import determine_buy_price_reliability
from jstock_advisor.domain.valuation.margin_of_safety import compute_margin_of_safety
from jstock_advisor.domain.valuation.valuation_confidence import determine_valuation_confidence
from jstock_advisor.domain.valuation.valuation_methods import (
    DispersionBand,
    compute_valuation_anchor,
)
from jstock_advisor.services.audit_service import AuditService

DECISION_TYPE: Final = "valuation_confidence_shadow"
SCHEMA_VERSION: Final = 1


@dataclass(frozen=True)
class ValuationConfidenceShadowInputs:
    """candidate再計算に必要な、判定時点の実測値のスナップショット(#582)。

    confidenceの影響を受けない値(methods_used_count・dispersion_ratio・
    excluded_outlier_count・borderline_interpolated_count・outlier_filter_
    blocking_reason等)は`valuation_summary`から導出できるため、重複して
    保持しない。config由来の閾値・method_weightsも`config`から導出する
    (analyze()時点のself._configをそのまま渡す。挙動に影響する値の
    二重保守を避けるため)。
    """

    valuation_summary: FairValueRange
    dispersion_band: DispersionBand | None
    industry_model_applied: bool
    uses_simplified_dcf: bool
    normalized_eps_confidence: ConfidenceLevel | None
    adjustment_codes: tuple[str, ...]
    data_quality_warning: bool
    earnings_date_status: EarningsDateStatus | None
    current_price: Decimal
    company_quality_score: float
    business_days_to_earnings: int | None
    config: AppConfig
    # --- 実際(v1)の結果。candidateとのdiff計算に使う ---
    actual_confidence: ConfidenceLevel
    actual_reasons_not_high: tuple[str, ...]
    actual_entry_price: Decimal | None
    actual_standard_price: Decimal | None
    actual_strong_price: Decimal | None
    actual_buy_action: BuyAction | None
    actual_raw_buy_action: BuyAction | None


@dataclass(frozen=True)
class _CandidateResult:
    """`_run_candidate_chain`の戻り値(6手順の再実行結果)。"""

    confidence: ConfidenceLevel
    reasons_not_high: tuple[str, ...]
    entry_price: Decimal | None
    standard_price: Decimal | None
    strong_price: Decimal | None
    buy_action: BuyAction
    raw_buy_action: BuyAction


class _HasValuationConfidenceShadowInputs(Protocol):
    """`BuyAnalysisOutcome`が満たす、shadow入力の供給側の形(読み取り専用)。"""

    @property
    def valuation_confidence_shadow_inputs(self) -> ValuationConfidenceShadowInputs | None: ...


def shadow_audit_id(recommendation_id: str) -> str:
    """決定的な監査ID(再試行・重複配信でも1件になる)。所有者・holding_idを含まない。"""
    return f"{DECISION_TYPE}:{recommendation_id}"


def _price_diff_pct(actual: Decimal | None, candidate: Decimal | None) -> float | None:
    """(candidate - actual) / actual を%で返す。どちらかがNone・actual=0なら比較不能でNone。"""
    if actual is None or candidate is None or actual == 0:
        return None
    return float((candidate - actual) / actual * 100)


def _margin_tier(confidence: ConfidenceLevel) -> str:
    # margin_of_safety.compute_margin_of_safety()自身のtier選択(HIGH/MEDIUM。
    # LOWはallowed=Falseで安全余裕率を生成しない)をそのまま表す。
    #
    # サブちゃんレビュー対応(PR #704 SHOULD): margin_tier(本関数の戻り値)は
    # confidenceそのものの写像であり、margin_tier_changedはconfidence_changedと
    # 定義上常に同値になる(compute_margin_of_safety()が信頼度以外でtierを
    # 切り替えないため)。これは意図した設計であり不具合ではない
    # (USER指定6項目の「margin tier変化件数」はconfidence tierの変化を指す)。
    # ただし、entry/standard/strong価格側のmaximum_marginキャップにより、
    # tierが変わっても実際の必要安全余裕率(%)が変わらない(=価格差分が0に近い)
    # ケースはentry_price_diff_pct等の価格差分側で別途観測できる。tierの変化と
    # 価格の変化は本output_valuesでは別のキーとして記録しており混同していない。
    return confidence.value


def _run_candidate_chain(inputs: ValuationConfidenceShadowInputs) -> _CandidateResult:
    """require_industry_model=Falseでcandidateの判定を再実行する(6手順)。"""
    config = inputs.config
    candidate_confidence_result = determine_valuation_confidence(
        methods_used_count=inputs.valuation_summary.methods_used_count or 0,
        dispersion_ratio=inputs.valuation_summary.valuation_dispersion_ratio,
        dispersion_medium_max=config.buy_decision.valuation_dispersion.medium_max,
        dispersion_anchor_block=config.buy_decision.valuation_dispersion.anchor_block,
        industry_model_applied=inputs.industry_model_applied,
        uses_simplified_dcf=inputs.uses_simplified_dcf,
        normalized_eps_confidence=inputs.normalized_eps_confidence,
        require_industry_model=False,
    )
    candidate_confidence = candidate_confidence_result.level

    anchor_result = compute_valuation_anchor(
        inputs.valuation_summary,
        candidate_confidence,
        inputs.dispersion_band,
        config.valuation.fair_value_methods.method_weights,
    )

    margin_result = compute_margin_of_safety(
        candidate_confidence, list(inputs.adjustment_codes), config.buy_decision.margin_of_safety
    )

    excluded_outlier_count = sum(
        1 for m in inputs.valuation_summary.methods_excluded if m.exclusion_detail is not None
    )
    borderline_interpolated_count = sum(
        1 for m in inputs.valuation_summary.methods_used if m.transition_detail is not None
    )
    reliability_result = determine_buy_price_reliability(
        margin_result=margin_result,
        maximum_entry_margin=config.buy_decision.margin_of_safety.maximum_margin.entry,
        valuation_dispersion_ratio=inputs.valuation_summary.valuation_dispersion_ratio,
        dispersion_medium_max=config.buy_decision.valuation_dispersion.medium_max,
        methods_used_count=inputs.valuation_summary.methods_used_count,
        data_quality_warning=inputs.data_quality_warning,
        earnings_date_status=inputs.earnings_date_status,
        excluded_outlier_count=excluded_outlier_count,
        outlier_filter_blocking_reason=inputs.valuation_summary.outlier_filter_blocking_reason,
        borderline_interpolated_count=borderline_interpolated_count,
    )

    buy_price_levels = compute_buy_price_levels(anchor_result.anchor, margin_result)

    decision = decide_buy_action(
        current_price=inputs.current_price,
        buy_price_levels=buy_price_levels,
        company_quality_score=inputs.company_quality_score,
        business_days_to_earnings=inputs.business_days_to_earnings,
        valuation_dispersion_ratio=inputs.valuation_summary.valuation_dispersion_ratio,
        buy_price_reliability=reliability_result.reliability,
        config=config.buy_decision,
    )
    candidate_entry_price = buy_price_levels.entry.price if buy_price_levels.entry else None
    candidate_standard_price = (
        buy_price_levels.standard.price if buy_price_levels.standard else None
    )
    candidate_strong_price = buy_price_levels.strong.price if buy_price_levels.strong else None
    candidate_buy_action = decision.action

    # --- サブちゃんレビュー対応(PR #704 MUST1): production側のstep18
    # (整合性検証。buy_signal_service.py:1489-1501)をcandidate側にも適用する。
    # これを省くと「step18適用後のactual」と「step18適用前のcandidate」を
    # 比較する形になり、実際には何も変わっていない(raw同士は同一)のに
    # buy_action_changed/action_transitionが見かけ上発火してしまう。
    #
    # F8(サブちゃんレビュー対応、記録のみ): この検証は通常の入力では発火しない
    # (サブちゃん実測: 5通りの入力すべてで不発火)。entry/standard/strongの
    # 価格順序・confidence起因の矛盾は、同じ`compute_buy_price_levels`/
    # `decide_buy_action`から導出されたcandidate_entry_price等を渡している限り
    # 構造的に生じにくいためである。**candidate側でMANUAL_REVIEWが一度も
    # 観測されないこと自体は実装漏れではない**(production側のstep18も同じ
    # 理由で通常は不発火であり、稀な不整合入力に対する二重の安全策として
    # 存在する)。対称性(actual/candidate双方に同じ検証を適用する)を保つ目的で
    # 追加しており、発火頻度を上げる目的の変更ではない。
    violations = validate_buy_recommendation(
        action=candidate_buy_action,
        current_price=inputs.current_price,
        entry_price=candidate_entry_price,
        standard_price=candidate_standard_price,
        strong_price=candidate_strong_price,
        confidence=candidate_confidence,
        business_days_to_earnings=inputs.business_days_to_earnings,
        valuation_dispersion_ratio=inputs.valuation_summary.valuation_dispersion_ratio,
        config=config.buy_decision,
    )
    if violations:
        candidate_buy_action = BuyAction.MANUAL_REVIEW

    return _CandidateResult(
        confidence=candidate_confidence,
        reasons_not_high=tuple(candidate_confidence_result.reasons_not_high),
        entry_price=candidate_entry_price,
        standard_price=candidate_standard_price,
        strong_price=candidate_strong_price,
        buy_action=candidate_buy_action,
        raw_buy_action=decision.raw_action,
    )


def build_shadow_record(
    recommendation: Recommendation, inputs: ValuationConfidenceShadowInputs
) -> tuple[dict[str, Any], dict[str, Any]]:
    """candidateを再実行し、監査記録用のinput_values/output_valuesへ変換する(純関数)。"""
    candidate = _run_candidate_chain(inputs)
    candidate_confidence = candidate.confidence
    candidate_reasons_not_high = candidate.reasons_not_high
    candidate_entry_price = candidate.entry_price
    candidate_standard_price = candidate.standard_price
    candidate_strong_price = candidate.strong_price
    candidate_buy_action = candidate.buy_action
    candidate_raw_buy_action = candidate.raw_buy_action

    input_values: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "recommendation_id": recommendation.recommendation_id,
        "require_industry_model": False,
    }
    output_values: dict[str, Any] = {
        "actual_confidence": inputs.actual_confidence.value,
        "candidate_confidence": candidate_confidence.value,
        "confidence_changed": inputs.actual_confidence != candidate_confidence,
        "actual_margin_tier": _margin_tier(inputs.actual_confidence),
        "candidate_margin_tier": _margin_tier(candidate_confidence),
        "margin_tier_changed": inputs.actual_confidence != candidate_confidence,
        "entry_price_diff_pct": _price_diff_pct(inputs.actual_entry_price, candidate_entry_price),
        "standard_price_diff_pct": _price_diff_pct(
            inputs.actual_standard_price, candidate_standard_price
        ),
        "strong_price_diff_pct": _price_diff_pct(
            inputs.actual_strong_price, candidate_strong_price
        ),
        "actual_buy_action": (
            inputs.actual_buy_action.value if inputs.actual_buy_action is not None else None
        ),
        "candidate_buy_action": candidate_buy_action.value,
        "buy_action_changed": inputs.actual_buy_action != candidate_buy_action,
        "actual_raw_buy_action": (
            inputs.actual_raw_buy_action.value if inputs.actual_raw_buy_action is not None else None
        ),
        "candidate_raw_buy_action": candidate_raw_buy_action.value,
        "raw_buy_action_changed": inputs.actual_raw_buy_action != candidate_raw_buy_action,
        "action_transition": (
            f"{inputs.actual_buy_action.value if inputs.actual_buy_action else 'NONE'}"
            f"->{candidate_buy_action.value}"
        ),
        "actual_reasons_not_high": list(inputs.actual_reasons_not_high),
        "candidate_reasons_not_high": list(candidate_reasons_not_high),
        "reasons_not_high_diff": {
            "removed": [
                r for r in inputs.actual_reasons_not_high if r not in candidate_reasons_not_high
            ],
            "added": [
                r for r in candidate_reasons_not_high if r not in inputs.actual_reasons_not_high
            ],
        },
    }
    return input_values, output_values


def record_shadow_observation(
    recommendation: Recommendation,
    inputs: ValuationConfidenceShadowInputs,
    audit_service: AuditService,
    now: dt.datetime,
) -> bool:
    """candidateを再実行し、既存の監査ログへ1件記録する。記録したらTrue。

    既に同じ監査IDの記録があれば何もしない(False)。
    """
    input_values, output_values = build_shadow_record(recommendation, inputs)
    entry = audit_service.record_if_absent(
        audit_id=shadow_audit_id(recommendation.recommendation_id),
        decision_type=DECISION_TYPE,
        stock_code=recommendation.stock_code,
        input_values=input_values,
        calculation_formulas={
            "evaluator": "determine_valuation_confidence(require_industry_model=False)",
            "schema_version": str(SCHEMA_VERSION),
        },
        output_values=output_values,
        data_sources=[],
        rule_version=recommendation.rule_version,
        timestamp=now,
    )
    return entry is not None


def observe_valuation_confidence_shadow(
    recommendation: Recommendation,
    outcome: _HasValuationConfidenceShadowInputs,
    now: dt.datetime,
    *,
    execution_context: ExecutionContext,
    audit_service: AuditService | None = None,
    shadow_config: ValuationConfidenceShadowConfig | None = None,
) -> bool:
    """handlerの合流点から呼ぶ入口。**例外を送出しない**。記録したらTrue。

    * shadowがOFF(既定・設定不備を含む)なら、candidateの再計算・記録のいずれも
      行わない。
    * SHADOWなら、candidateの再計算・記録の**すべてを**隔離の内側で行う(失敗
      しても本流へ伝播せず、Falseを返す。リトライしない)。
    """
    config = (
        shadow_config if shadow_config is not None else load_valuation_confidence_shadow_config()
    )
    if not config.enabled:
        return False

    inputs = outcome.valuation_confidence_shadow_inputs
    if inputs is None:
        return False

    def _build() -> bool:
        service = (
            audit_service
            if audit_service is not None
            else AuditService(execution_context=execution_context)
        )
        return record_shadow_observation(recommendation, inputs, service, now)

    return isolated_shadow_computation(
        "valuation_confidence_shadow", build=_build, on_failure=lambda _exc: False
    )
