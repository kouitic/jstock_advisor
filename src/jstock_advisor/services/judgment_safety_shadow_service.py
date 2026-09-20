"""判断の安全条件(G1〜G4)のshadow計測: 評価と監査記録(Issue #160 PR-3 / #457)。

shadow = 判定・通知・保存を**変えず**に、「安全条件を適用していたら何件がどうなったか」だけを
観測する機構である。本moduleは、handlerの合流点(Recommendationの保存が完了した後)から呼ばれ、
強い判定(BUY系 / 利確のFULL_PROFIT_TAKE)について純関数`evaluate_safety_conditions`を評価し、
結果を既存の`AuditLogTable`へ`decision_type="judgment_safety_shadow"`として1件記録する
(USER決定 U13 = OPTION_C: 新規Table・新規IAM・TTL/retention変更なし)。

## 本流を変えない構造(不変条件)

* **OFFなら何もしない**: `shadow_config.enabled`がFalseなら、評価関数も監査記録も呼ばない。
  設定が無い・読めない・不正な場合はOFFへ縮退する(`load_judgment_safety_shadow_config`)。
* **保存の後に置く**: 呼び出し元は、Recommendation・DecisionSnapshotの保存が完了した後にだけ呼ぶ。
  shadowは保存内容を変えられない。
* **失敗を隔離する**: 評価と記録の**両方**を`isolated_shadow_computation`(S-20)の中へ入れる。
  例外(評価・記録・AccessDenied等)は本流へ伝播せず、リトライもしない(shadowは欠落してよい。
  欠落は集計で件数の差として現れる)。
* **冪等**: `record_if_absent`と決定的な`audit_id`により、再試行・重複配信でも1件になる。
* **VALIDATIONでは記録しない**: 呼び出し元の既存のifが除外し、`AuditService`自身もVALIDATIONでは
  保存しない(二重の抑止)。
* **Recommendation・永続schemaへ載せない**: 事実(`SafetyFacts`)は各サービスの戻り値
  (`safety_facts`)から読むだけで、handlerの中で完結する。

## 記録する内容(allowlist)

下記の**列挙のみ**を記録する(スキーマに無いキーを足さない)。強い判定でない場合は記録しない
(書き込み量を強い判定の件数に限る)。強い判定なら、findingが0件でも記録する(分母のため)。

保存を禁止する項目: holding_id / owner / 保有数量 / 平均取得単価 / 価格(現在値・適正価格・売買価格)/
銘柄名 / `DataQualityIssue`本体・description・suppressed_values / 例外メッセージ /
Recommendationの理由等の文章。`stock_code`は既存の監査記録と同じ扱いで、監査記録のトップレベルの
項目として持つ(本moduleは`input_values`へ複製しない)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.shadow_observation import isolated_shadow_computation
from jstock_advisor.domain.signals.judgment_safety import (
    UNMEASURABLE_G3_INPUTS,
    SafetyEvaluation,
    SafetyFacts,
    evaluate_safety_conditions,
    is_strong_buy_side,
    is_strong_full_profit_take,
)
from jstock_advisor.domain.signals.judgment_safety_shadow_config import (
    JudgmentSafetyShadowConfig,
    load_judgment_safety_shadow_config,
)
from jstock_advisor.services.audit_service import AuditService

DECISION_TYPE: Final = "judgment_safety_shadow"
SCHEMA_VERSION: Final = 1
EVALUATOR_NAME: Final = "evaluate_safety_conditions"

ShadowEngine = Literal["BUY_CANDIDATES", "HOLDINGS_PROFIT_TAKING"]
ENGINE_BUY_CANDIDATES: Final[ShadowEngine] = "BUY_CANDIDATES"
ENGINE_HOLDINGS_PROFIT_TAKING: Final[ShadowEngine] = "HOLDINGS_PROFIT_TAKING"

#: `input_values`へ保存を許可するキー(これ以外を足さない)。
INPUT_VALUE_KEYS: Final[tuple[str, ...]] = (
    "schema_version",
    "engine",
    "recommendation_id",
    "recommendation_type",
    "buy_action",
    "earnings_date_status",
    "shadow_mode",
    "g3_required_inputs",
    "facts",
)
#: `facts`(SafetyFactsの転記)へ保存を許可するキー。
FACT_KEYS: Final[tuple[str, ...]] = (
    "financials_are_stale",
    "profit_taking_mitigation",
    "corporate_action",
)
#: `output_values`へ保存を許可するキー。
OUTPUT_VALUE_KEYS: Final[tuple[str, ...]] = (
    "findings",
    "not_evaluated",
    "unmeasurable_g3_inputs",
    "strong",
)


class _HasSafetyFacts(Protocol):
    """`BuyAnalysisOutcome` / `ProfitTakingOutcome`が満たす、事実の供給側の形(読み取り専用)。"""

    @property
    def safety_facts(self) -> SafetyFacts | None: ...


@dataclass(frozen=True)
class ShadowRecord:
    """監査へ書く内容(handlerの外へ出さない。テスト可能にするための値オブジェクト)。"""

    audit_id: str
    stock_code: str
    rule_version: str
    input_values: dict[str, Any]
    output_values: dict[str, Any]
    calculation_formulas: dict[str, str]


def shadow_audit_id(recommendation_id: str) -> str:
    """決定的な監査ID(再試行・重複配信でも1件になる)。所有者・holding_idを含まない。"""
    return f"{DECISION_TYPE}:{recommendation_id}"


def is_strong_judgment(recommendation: Recommendation) -> bool:
    """shadowの評価・記録の対象(強い判定 = BUY系 / 利確のFULL_PROFIT_TAKE)か。"""
    return is_strong_buy_side(recommendation) or is_strong_full_profit_take(recommendation)


def _enum_value(value: Any) -> Any:
    return None if value is None else value.value


def _facts_payload(facts: SafetyFacts) -> dict[str, Any]:
    mitigation = facts.profit_taking_mitigation
    corporate_action = facts.corporate_action
    return {
        "financials_are_stale": facts.financials_are_stale,
        "profit_taking_mitigation": (
            None
            if mitigation is None
            else {
                "continuous_dividend_increase_years": mitigation.continuous_dividend_increase_years,
                "is_progressive_or_doe_policy": mitigation.is_progressive_or_doe_policy,
            }
        ),
        "corporate_action": (
            None
            if corporate_action is None
            else {
                "state": corporate_action.state,
                "unresolved_checks": list(corporate_action.unresolved_checks),
            }
        ),
    }


def build_shadow_record(
    recommendation: Recommendation,
    facts: SafetyFacts,
    engine: ShadowEngine,
    shadow_config: JudgmentSafetyShadowConfig,
    evaluation: SafetyEvaluation,
) -> ShadowRecord:
    """評価結果を、許可した項目だけの監査内容へ変換する(純関数)。"""
    input_values: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        "recommendation_id": recommendation.recommendation_id,
        "recommendation_type": _enum_value(recommendation.recommendation_type),
        "buy_action": _enum_value(recommendation.buy_action),
        "earnings_date_status": _enum_value(recommendation.earnings_date_status),
        "shadow_mode": shadow_config.mode.value,
        "g3_required_inputs": list(shadow_config.g3_required_inputs),
        "facts": _facts_payload(facts),
    }
    output_values: dict[str, Any] = {
        "findings": [
            {
                "condition_id": finding.condition_id,
                "reason_code": finding.reason_code,
                "would_suppress": finding.would_suppress,
            }
            for finding in evaluation.findings
        ],
        "not_evaluated": list(evaluation.not_evaluated),
        # 測定できないG3の入力は、明示して件数へ含めない(FalseをUNKNOWNと推測しない)。
        "unmeasurable_g3_inputs": list(UNMEASURABLE_G3_INPUTS),
        "strong": True,
    }
    return ShadowRecord(
        audit_id=shadow_audit_id(recommendation.recommendation_id),
        stock_code=recommendation.stock_code,
        rule_version=recommendation.rule_version,
        input_values=input_values,
        output_values=output_values,
        calculation_formulas={
            "evaluator": EVALUATOR_NAME,
            "schema_version": str(SCHEMA_VERSION),
        },
    )


def record_shadow_observation(
    recommendation: Recommendation,
    facts: SafetyFacts,
    engine: ShadowEngine,
    shadow_config: JudgmentSafetyShadowConfig,
    audit_service: AuditService,
    now: dt.datetime,
) -> bool:
    """強い判定を評価し、既存の監査ログへ1件記録する。記録したらTrue。

    強い判定でなければ評価も記録もしない(False)。既に同じ監査IDの記録があれば何もしない(False)。
    """
    if not is_strong_judgment(recommendation):
        return False
    evaluation = evaluate_safety_conditions(recommendation, facts, shadow_config)
    record = build_shadow_record(recommendation, facts, engine, shadow_config, evaluation)
    entry = audit_service.record_if_absent(
        audit_id=record.audit_id,
        decision_type=DECISION_TYPE,
        stock_code=record.stock_code,
        input_values=record.input_values,
        calculation_formulas=record.calculation_formulas,
        output_values=record.output_values,
        data_sources=[],
        rule_version=record.rule_version,
        timestamp=now,
    )
    return entry is not None


def observe_judgment_safety_shadow(
    recommendation: Recommendation,
    outcome: _HasSafetyFacts,
    engine: ShadowEngine,
    now: dt.datetime,
    *,
    execution_context: ExecutionContext,
    audit_service: AuditService | None = None,
    shadow_config: JudgmentSafetyShadowConfig | None = None,
) -> bool:
    """handlerの合流点から呼ぶ入口。**例外を送出しない**。記録したらTrue。

    * shadowがOFF(既定・設定不備を含む)なら、事実の取得・評価・記録のいずれも行わない。
    * SHADOWなら、事実の取得・評価・記録の**すべてを**隔離の内側で行う(失敗しても本流へ
      伝播せず、Falseを返す。リトライしない)。
    """
    config = shadow_config if shadow_config is not None else load_judgment_safety_shadow_config()
    if not config.enabled:
        return False

    def _build() -> bool:
        service = (
            audit_service
            if audit_service is not None
            else AuditService(execution_context=execution_context)
        )
        # 事実が供給されていない(None)場合は、何も供給されていない扱い(全条件が not_evaluated)。
        facts = outcome.safety_facts
        return record_shadow_observation(
            recommendation,
            facts if facts is not None else SafetyFacts(),
            engine,
            config,
            service,
            now,
        )

    return isolated_shadow_computation(
        "judgment_safety_shadow", build=_build, on_failure=lambda _exc: False
    )
