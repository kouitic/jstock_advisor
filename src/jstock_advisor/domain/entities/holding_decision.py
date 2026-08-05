"""保有判断スコア方式(2026-08仕様)のドメインエンティティ。

企業品質スコア(0-50)+投資ストーリー維持スコア(0-50)-リスク控除スコア(0-100)を
統合した単一の保有判断スコアで、保有銘柄の売却推奨を判定する。詳細は実装プラン
(保有銘柄「保有判断スコア」方式への移行)を参照。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.base import Entity, ImmutableSnapshot
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import (
    BaselineOrigin,
    BaselineStatus,
    EvidenceCoverageStatus,
    ExecutionPlanReason,
    FinancialPolicyOverride,
    HoldingDecisionCategory,
    HoldingDecisionConfidenceLevel,
    RuntimeConfigMode,
    ThesisConditionAttestationStatus,
)
from pydantic import model_validator

# ============================================================================
# 比率指標・スコア項目の詳細内訳
# ============================================================================


class RatioMetricDetail(ImmutableSnapshot):
    """比率指標(営業CF/営業利益比率・簡易予想ROE等)1件の算出詳細。

    必須メタデータ(missing_required_metadata)が1件でもあればcalculation_status=
    NOT_EVALUATEDとなり採点しない。参考メタデータ(missing_optional_metadata)の
    欠損は採点を継続しつつconfidenceをMEDIUM以下へ制限する。
    """

    metric_name: str
    calculation_status: EvidenceCoverageStatus
    confidence: HoldingDecisionConfidenceLevel | None = None
    missing_required_metadata: tuple[str, ...] = ()
    missing_optional_metadata: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    raw_input_value: float | None = None
    clamped_input_value: float | None = None


class ScoreItemDetail(ImmutableSnapshot):
    """企業品質・投資ストーリー維持スコアの評価軸1項目分の内訳。

    1.5節のコンポーネント正規化式(available_points/raw_points)の入力になる。
    """

    item_code: str
    axis: str
    weight: float
    status: EvidenceCoverageStatus
    points_earned: float = 0.0
    reason: str | None = None


class RiskDeductionCategoryDetail(ImmutableSnapshot):
    """リスク控除カテゴリ1件分の内訳(4節のカテゴリ別上限に対応)。"""

    category: str
    cap: float
    points: float
    status: EvidenceCoverageStatus = EvidenceCoverageStatus.EVALUATED
    signal_reason_codes: tuple[str, ...] = ()


# ============================================================================
# コンポーネントスコア(企業品質/投資ストーリー維持/リスク控除)
# ============================================================================


class CompanyQualityScore(ImmutableSnapshot):
    """企業品質スコア(0-50点)。1.5節の正規化式で算出する。"""

    score: float
    coverage_ratio: float
    items: tuple[ScoreItemDetail, ...] = ()
    ratio_metric_details: tuple[RatioMetricDetail, ...] = ()


class InvestmentThesisScore(ImmutableSnapshot):
    """投資ストーリー維持スコア(0-50点)。"""

    score: float
    coverage_ratio: float
    items: tuple[ScoreItemDetail, ...] = ()
    baseline_id: str | None = None
    baseline_version: int | None = None
    baseline_origin: BaselineOrigin | None = None


class RiskDeductionScore(ImmutableSnapshot):
    """リスク控除スコア(0-100点)。ハードゲート該当イベントは含まない(7節・4節)。"""

    score: float
    coverage_ratio: float
    categories: tuple[RiskDeductionCategoryDetail, ...] = ()


class HoldingDecisionHardGate(ImmutableSnapshot):
    """ハードゲート判定結果(7節)。"""

    triggered: bool
    reason_codes: tuple[str, ...] = ()
    score_cap: float | None = None
    adjustment_applied: bool = False


class ComponentCoverage(ImmutableSnapshot):
    """component別coverage_ratio(8節)。"""

    overall: float
    company_quality: float
    investment_thesis: float
    risk_deduction: float


class ReasonImpact(ImmutableSnapshot):
    """主な加点・減点要因1件分の構造化データ(15節)。表示専用文字列ではなく、
    LINE通知・CSV・将来のWeb画面・AI分析で共通利用できる構造として保持する。
    """

    reason_code: str
    category: str
    score_impact: float


# ============================================================================
# 保有判断スコアの結果(監査・Shadow比較・通知判定に使う唯一のレコード)
# ============================================================================


class HoldingDecisionResult(ImmutableSnapshot):
    """1銘柄1回の保有判断スコア評価結果(不変スナップショット)。

    ExecutionPlan.run_holding_decision_evaluation=trueであれば、通知の有無に
    関わらず必ず1レコード保存する(11節)。
    """

    holding_decision_result_id: str
    holding_id: str
    stock_code: str
    evaluated_at: dt.datetime

    company_quality: CompanyQualityScore
    investment_thesis: InvestmentThesisScore
    risk_deduction: RiskDeductionScore

    base_score: float
    hard_gate: HoldingDecisionHardGate
    final_score: float
    display_value: int

    category: HoldingDecisionCategory
    coverage: ComponentCoverage
    confidence: HoldingDecisionConfidenceLevel

    should_notify: bool
    recommendation_id: str | None = None

    baseline_id: str | None = None
    baseline_version: int | None = None
    baseline_origin: BaselineOrigin | None = None

    scoring_model_version: int
    # RuntimeConfig取得失敗によりフォールバック既定値を使った場合は-1を保存する
    # (12節。正常取得時は必ず1以上の実際のレコードバージョンが入る)。
    runtime_config_version: int
    financial_model_version_used: int | None = None

    execution_plan_reason: ExecutionPlanReason
    evaluation_duration_ms: int | None = None

    # Shadow比較用(mode=shadow等で新旧両エンジンが実行された場合のみ両方埋まる)。
    legacy_reason_codes: tuple[str, ...] = ()
    new_reason_codes: tuple[str, ...] = ()

    # 主な加点・減点要因(15節)。score_impactの絶対値が大きい順、上限は
    # config化(top_positive_reasons_count/top_negative_reasons_count)。
    positive_reasons: tuple[ReasonImpact, ...] = ()
    negative_reasons: tuple[ReasonImpact, ...] = ()

    data_sources: tuple[DataSourceReference, ...] = ()


# ============================================================================
# Baseline(投資ストーリー維持スコアの基準時点)
# ============================================================================


class BaselineValueSnapshot(ImmutableSnapshot):
    """baseline比較対象の実値スナップショット(投資ストーリー維持スコアの
    標準5軸: 配当方針/総合利回り/優待条件/利益CF前提/財務健全性)。
    """

    dividend_policy_note: str | None = None
    total_yield_pct: float | None = None
    has_shareholder_benefit: bool | None = None
    benefit_condition_note: str | None = None
    operating_income_trend_note: str | None = None
    operating_cashflow_trend_note: str | None = None
    equity_ratio_pct: float | None = None


class InvestmentThesisBaseline(ImmutableSnapshot):
    """baseline本体(完全な不変スナップショット)。「現在有効か」はこの
    エンティティでは表現しない(InvestmentThesisBaselinePointerが担う)。
    """

    baseline_id: str  # "{holding_id}:v{version}"
    holding_id: str  # 現状stock_codeの1:1エイリアス
    stock_code: str
    version: int
    origin: BaselineOrigin
    status: BaselineStatus  # 人間承認の進行状態のみ(DRAFT/PROPOSED/APPROVED/REJECTED)
    created_at: dt.datetime
    approved_at: dt.datetime | None = None
    approved_by: str | None = None
    supersedes_baseline_id: str | None = None
    baseline_values: BaselineValueSnapshot


class InvestmentThesisBaselinePointer(Entity):
    """「現在有効なbaseline」を指す唯一の情報源(1 holding_id = 1行)。

    条件付きUpdateItem(pointer_versionによる楽観ロック)で更新する。
    baseline本体は書き換えない。
    """

    holding_id: str
    active_baseline_id: str
    active_baseline_version: int
    pointer_version: int
    updated_at: dt.datetime
    updated_by: str | None = None


# ============================================================================
# 個別購入理由(InvestmentThesis / CustomThesisCondition)
# ============================================================================


class ThesisConditionAttestation(ImmutableSnapshot):
    """CustomThesisConditionに対する人間の定期申告(自由記述の解釈は行わない)。"""

    status: ThesisConditionAttestationStatus
    attested_at: dt.datetime
    attested_by: str


class CustomThesisCondition(ImmutableSnapshot):
    """人間が構造化登録した銘柄固有の投資理由1件(投資ストーリー維持スコアの
    個別購入理由軸、5点)。共通テンプレートの標準5軸とは独立。
    """

    condition_id: str
    description: str
    registered_at: dt.datetime
    last_attestation: ThesisConditionAttestation | None = None


class InvestmentThesis(Entity):
    """銘柄固有の個別購入理由(構造化条件の集合)。標準5軸のbaseline比較とは別物。"""

    investment_thesis_id: str
    holding_id: str
    stock_code: str
    conditions: list[CustomThesisCondition] = []
    updated_at: dt.datetime


# ============================================================================
# ランタイムConfig(mode / kill switch)
# ============================================================================


class HoldingDecisionRuntimeConfig(Entity):
    """再デプロイ不要で切り替える運用パラメータ(専用DynamoDBテーブル)。

    config/*.yaml(Layer同梱、スコア配点等)とは別物。
    """

    config_id: str = "holding_decision"
    config_version: int
    mode: RuntimeConfigMode
    notification_enabled: bool
    financial_policy_override: FinancialPolicyOverride
    updated_at: dt.datetime
    updated_by: str
    change_reason: str


# ============================================================================
# 新旧エンジンの排他制御
# ============================================================================


class HoldingDecisionExecutionPlan(ImmutableSnapshot):
    """新旧エンジンの実行可否・通知許可を独立したbooleanで表現する(11節)。

    allow_legacy_sell_notificationとallow_holding_decision_notificationが
    同時にTrueになることは、この不変条件により構造的に発生し得ない。
    """

    run_legacy_sell_evaluation: bool
    allow_legacy_sell_notification: bool
    run_holding_decision_evaluation: bool
    allow_holding_decision_notification: bool
    run_profit_taking_when_no_sell_notification: bool = True
    execution_reason: ExecutionPlanReason

    @model_validator(mode="after")
    def _check_notification_exclusivity(self) -> HoldingDecisionExecutionPlan:
        if self.allow_legacy_sell_notification and self.allow_holding_decision_notification:
            raise ValueError(
                "HoldingDecisionExecutionPlan: allow_legacy_sell_notificationと"
                "allow_holding_decision_notificationを同時にTrueにはできません"
                "(新旧同時通知の禁止)"
            )
        return self
