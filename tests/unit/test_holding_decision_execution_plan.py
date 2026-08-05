"""HoldingDecisionExecutionPlanの4パターン・不変条件のテスト(実装プラン11節)。"""

import datetime as dt

import pytest
from pydantic import ValidationError

from jstock_advisor.config.loader import load_config
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
from jstock_advisor.domain.signals.holding_decision_execution_plan import (
    resolve_execution_plan,
    resolve_financial_deferred_policy,
)

_CFG = load_config()


def _runtime_config(override: FinancialPolicyOverride) -> HoldingDecisionRuntimeConfig:
    return HoldingDecisionRuntimeConfig(
        config_version=1,
        mode=RuntimeConfigMode.ACTIVE,
        notification_enabled=True,
        financial_policy_override=override,
        updated_at=dt.datetime.now(dt.UTC),
        updated_by="tester",
        change_reason="test",
    )


def test_legacy_mode_runs_only_legacy():
    policy = resolve_financial_deferred_policy(
        _runtime_config(FinancialPolicyOverride.DEFAULT),
        _CFG.industry_scoring_policy.financial_industry_policy,
    )
    plan = resolve_execution_plan(
        RuntimeConfigMode.LEGACY, IndustryClassification.GENERAL_CORPORATE, None, policy
    )
    assert plan.run_legacy_sell_evaluation is True
    assert plan.allow_legacy_sell_notification is True
    assert plan.run_holding_decision_evaluation is False
    assert plan.allow_holding_decision_notification is False
    assert plan.execution_reason == ExecutionPlanReason.NORMAL_LEGACY


def test_shadow_mode_runs_both_but_only_legacy_notifies():
    policy = resolve_financial_deferred_policy(
        _runtime_config(FinancialPolicyOverride.DEFAULT),
        _CFG.industry_scoring_policy.financial_industry_policy,
    )
    plan = resolve_execution_plan(
        RuntimeConfigMode.SHADOW, IndustryClassification.GENERAL_CORPORATE, None, policy
    )
    assert plan.run_legacy_sell_evaluation is True
    assert plan.allow_legacy_sell_notification is True
    assert plan.run_holding_decision_evaluation is True
    assert plan.allow_holding_decision_notification is False
    assert plan.execution_reason == ExecutionPlanReason.NORMAL_SHADOW


def test_active_mode_general_corporate_runs_only_new_engine():
    policy = resolve_financial_deferred_policy(
        _runtime_config(FinancialPolicyOverride.DEFAULT),
        _CFG.industry_scoring_policy.financial_industry_policy,
    )
    plan = resolve_execution_plan(
        RuntimeConfigMode.ACTIVE, IndustryClassification.GENERAL_CORPORATE, None, policy
    )
    assert plan.run_legacy_sell_evaluation is False
    assert plan.allow_legacy_sell_notification is False
    assert plan.run_holding_decision_evaluation is True
    assert plan.allow_holding_decision_notification is True
    assert plan.execution_reason == ExecutionPlanReason.NORMAL_ACTIVE


def test_active_mode_financial_deferred_falls_back_to_legacy_notification():
    policy = resolve_financial_deferred_policy(
        _runtime_config(FinancialPolicyOverride.DEFAULT),
        _CFG.industry_scoring_policy.financial_industry_policy,
    )
    plan = resolve_execution_plan(
        RuntimeConfigMode.ACTIVE,
        IndustryClassification.FINANCIAL,
        FinancialIndustryCategory.BANKING,
        policy,
    )
    assert plan.run_legacy_sell_evaluation is True
    assert plan.allow_legacy_sell_notification is True
    assert plan.run_holding_decision_evaluation is True
    assert plan.allow_holding_decision_notification is False
    assert plan.execution_reason == ExecutionPlanReason.FINANCIAL_MODEL_DEFERRED


def test_force_defer_all_overrides_yaml_supported_category():
    """YAML側でsupported_financial_categoriesに含まれるカテゴリがあっても、
    FORCE_DEFER_ALLは常に全カテゴリを退避させる。"""
    policy = resolve_financial_deferred_policy(
        _runtime_config(FinancialPolicyOverride.FORCE_DEFER_ALL),
        _CFG.industry_scoring_policy.financial_industry_policy,
    )
    plan = resolve_execution_plan(
        RuntimeConfigMode.ACTIVE,
        IndustryClassification.FINANCIAL,
        FinancialIndustryCategory.BANKING,
        policy,
    )
    assert plan.allow_holding_decision_notification is False
    assert plan.execution_reason == ExecutionPlanReason.FINANCIAL_MODEL_DEFERRED


def test_no_default_config_has_no_active_financial_category():
    """RuntimeConfigから金融業をActiveへ強制する経路が存在しないこと
    (FinancialPolicyOverrideの値がDEFAULT/FORCE_DEFER_ALLの2値しか無く、
    Active強制に相当する値自体が定義されていない)。"""
    assert set(FinancialPolicyOverride) == {
        FinancialPolicyOverride.DEFAULT,
        FinancialPolicyOverride.FORCE_DEFER_ALL,
    }


def test_execution_plan_invariant_rejects_dual_notification():
    with pytest.raises(ValidationError):
        HoldingDecisionExecutionPlan(
            run_legacy_sell_evaluation=True,
            allow_legacy_sell_notification=True,
            run_holding_decision_evaluation=True,
            allow_holding_decision_notification=True,
            execution_reason=ExecutionPlanReason.NORMAL_SHADOW,
        )


@pytest.mark.parametrize(
    ("mode", "financial_category"),
    [
        (RuntimeConfigMode.LEGACY, None),
        (RuntimeConfigMode.SHADOW, None),
        (RuntimeConfigMode.ACTIVE, None),
        (RuntimeConfigMode.ACTIVE, FinancialIndustryCategory.BANKING),
    ],
)
def test_kill_switch_off_forces_both_notification_flags_false(
    mode: RuntimeConfigMode, financial_category: FinancialIndustryCategory | None
):
    """kill switch(notification_enabled=False)はmode・業種に関わらず、新旧
    どちらの通知許可フラグも強制的にFalseへ落とす(実装プラン修正2)。
    run_*(判定・記録の実行有無)は変更しない。"""
    policy = resolve_financial_deferred_policy(
        _runtime_config(FinancialPolicyOverride.DEFAULT),
        _CFG.industry_scoring_policy.financial_industry_policy,
    )
    industry = (
        IndustryClassification.FINANCIAL
        if financial_category is not None
        else IndustryClassification.GENERAL_CORPORATE
    )
    plan_with_notification = resolve_execution_plan(
        mode, industry, financial_category, policy, notification_enabled=True
    )
    plan_without_notification = resolve_execution_plan(
        mode, industry, financial_category, policy, notification_enabled=False
    )

    assert plan_without_notification.allow_legacy_sell_notification is False
    assert plan_without_notification.allow_holding_decision_notification is False
    # 判定・記録の実行有無(run_*)自体はkill switchの影響を受けない。
    assert (
        plan_without_notification.run_legacy_sell_evaluation
        == plan_with_notification.run_legacy_sell_evaluation
    )
    assert (
        plan_without_notification.run_holding_decision_evaluation
        == plan_with_notification.run_holding_decision_evaluation
    )
    assert plan_without_notification.execution_reason == plan_with_notification.execution_reason


def test_notification_enabled_defaults_to_true_for_backward_compatibility():
    """notification_enabled引数を省略した既存呼び出しは、従来どおりmode・業種
    のみで通知可否が決まる(後方互換)。"""
    policy = resolve_financial_deferred_policy(
        _runtime_config(FinancialPolicyOverride.DEFAULT),
        _CFG.industry_scoring_policy.financial_industry_policy,
    )
    plan = resolve_execution_plan(
        RuntimeConfigMode.ACTIVE, IndustryClassification.GENERAL_CORPORATE, None, policy
    )
    assert plan.allow_holding_decision_notification is True


def test_run_profit_taking_when_no_sell_notification_defaults_true():
    plan = HoldingDecisionExecutionPlan(
        run_legacy_sell_evaluation=True,
        allow_legacy_sell_notification=True,
        run_holding_decision_evaluation=False,
        allow_holding_decision_notification=False,
        execution_reason=ExecutionPlanReason.NORMAL_LEGACY,
    )
    assert plan.run_profit_taking_when_no_sell_notification is True
