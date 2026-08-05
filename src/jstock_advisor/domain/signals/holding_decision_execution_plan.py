"""新旧エンジンの実行権限を決定する純粋関数(実装プラン11節・3節)。

HoldingDecisionExecutionPlan自体のPydantic不変条件(通知許可2フラグの同時True禁止)
と合わせて、「同一銘柄で新旧双方から通知が出る」ことを構造的に排除する。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jstock_advisor.config.models import FinancialIndustryPolicy
from jstock_advisor.domain.entities.enums import (
    ExecutionPlanReason,
    FinancialIndustryCategory,
    FinancialPolicyOverride,
    IndustryClassification,
    RuntimeConfigMode,
)
from jstock_advisor.domain.entities.holding_decision import (
    HoldingDecisionExecutionPlan,
    HoldingDecisionRuntimeConfig,
)

_ALL_FINANCIAL_CATEGORIES = frozenset(
    {
        FinancialIndustryCategory.BANKING,
        FinancialIndustryCategory.INSURANCE,
        FinancialIndustryCategory.SECURITIES,
        FinancialIndustryCategory.OTHER_FINANCIAL,
    }
)


@dataclass(frozen=True)
class FinancialDeferredPolicy:
    """金融業カテゴリごとの「新方式が通知を担当してよいか」の解決結果。

    RuntimeConfig(退避方向のみの緊急オーバーライド)とYAML(正の設定元、
    カテゴリ別deferred)を一本化した結果であり、これ自体からActive化を
    強制する経路は存在しない。
    """

    all_categories_deferred: bool
    deferred_categories: frozenset[FinancialIndustryCategory] = field(default_factory=frozenset)
    financial_model_version: int | None = None

    def is_deferred(self, category: FinancialIndustryCategory | None) -> bool:
        if category is None:
            return False
        if self.all_categories_deferred:
            return True
        return category in self.deferred_categories


def resolve_financial_deferred_policy(
    runtime_config: HoldingDecisionRuntimeConfig,
    yaml_industry_policy: FinancialIndustryPolicy,
) -> FinancialDeferredPolicy:
    """RuntimeConfig.financial_policy_overrideとYAMLのカテゴリ別deferredを一本化する。

    DEFAULT: YAML設定(カテゴリ別deferred)をそのまま使う。
    FORCE_DEFER_ALL: YAML側の解除状況に関わらず全金融業カテゴリを即座に退避させる
    (緊急退避専用。この関数からActiveへ強制する経路は存在しない)。
    """
    if runtime_config.financial_policy_override == FinancialPolicyOverride.FORCE_DEFER_ALL:
        return FinancialDeferredPolicy(all_categories_deferred=True)

    deferred = frozenset(
        category for category, policy in yaml_industry_policy.categories.items() if policy.deferred
    )
    return FinancialDeferredPolicy(
        all_categories_deferred=False,
        deferred_categories=frozenset(FinancialIndustryCategory(c) for c in deferred),
        financial_model_version=yaml_industry_policy.financial_model_version,
    )


def resolve_execution_plan(
    mode: RuntimeConfigMode,
    industry_classification: IndustryClassification,
    financial_category: FinancialIndustryCategory | None,
    financial_deferred_policy: FinancialDeferredPolicy,
    notification_enabled: bool = True,
) -> HoldingDecisionExecutionPlan:
    """global mode・業種分類・金融業deferredポリシーから実行計画を一意に決定する。

    notification_enabled(kill switch)がFalseの場合、mode・業種に関わらず
    新旧どちらの通知許可フラグも強制的にFalseへ落とす(12節: kill switchは
    modeとは独立した緊急停止スイッチ。判定・記録自体は継続し、通知だけを
    止める)。呼び出し側はこの値をキャッシュを経由せず毎回取得すること
    (services/holding_decision_runtime_config_service.pyの
    get_notification_enabled()参照)。
    """
    is_financial = industry_classification == IndustryClassification.FINANCIAL
    financial_deferred = is_financial and financial_deferred_policy.is_deferred(financial_category)

    if mode == RuntimeConfigMode.LEGACY:
        plan = HoldingDecisionExecutionPlan(
            run_legacy_sell_evaluation=True,
            allow_legacy_sell_notification=True,
            run_holding_decision_evaluation=False,
            allow_holding_decision_notification=False,
            execution_reason=ExecutionPlanReason.NORMAL_LEGACY,
        )
    elif mode == RuntimeConfigMode.SHADOW:
        plan = HoldingDecisionExecutionPlan(
            run_legacy_sell_evaluation=True,
            allow_legacy_sell_notification=True,
            run_holding_decision_evaluation=True,
            allow_holding_decision_notification=False,
            execution_reason=ExecutionPlanReason.NORMAL_SHADOW,
        )
    elif financial_deferred:  # mode == ACTIVE
        plan = HoldingDecisionExecutionPlan(
            run_legacy_sell_evaluation=True,
            allow_legacy_sell_notification=True,
            run_holding_decision_evaluation=True,
            allow_holding_decision_notification=False,
            execution_reason=ExecutionPlanReason.FINANCIAL_MODEL_DEFERRED,
        )
    else:  # mode == ACTIVE, general corporate
        plan = HoldingDecisionExecutionPlan(
            run_legacy_sell_evaluation=False,
            allow_legacy_sell_notification=False,
            run_holding_decision_evaluation=True,
            allow_holding_decision_notification=True,
            execution_reason=ExecutionPlanReason.NORMAL_ACTIVE,
        )

    if notification_enabled:
        return plan
    return plan.model_copy(
        update={
            "allow_legacy_sell_notification": False,
            "allow_holding_decision_notification": False,
        }
    )
