"""保有判断スコアのオーケストレーション(実装プラン全体)。

HoldingDecisionInputBuilder → CompanyQualityScoringService →
InvestmentThesisScoringService → RiskDeductionScoringService →
HoldingDecisionScoreCalculator → HoldingDecisionHardGateEvaluator →
HoldingDecisionPolicy(should_notify判定)という一連の流れを1つのサービスへ
まとめる(各段階のロジック自体は対応するdomain/signals/*.pyへ委譲する)。
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from dataclasses import dataclass

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.classification.financial_industry import classify_industry
from jstock_advisor.domain.entities.enums import (
    BaselineOrigin,
    BaselineStatus,
    EvidenceCoverageStatus,
    ExecutionPlanReason,
    PeriodType,
    TriggerStatus,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holding_decision import (
    BaselineValueSnapshot,
    CompanyQualityScore,
    HoldingDecisionResult,
    InvestmentThesisScore,
    ReasonImpact,
    RiskDeductionScore,
)
from jstock_advisor.domain.financial_series import FinancialPeriodValue
from jstock_advisor.domain.signals.company_quality_scoring import (
    CompanyQualityInputs,
    score_company_quality,
)
from jstock_advisor.domain.signals.holding_decision_hard_gate import (
    HardGateInputs,
    evaluate_hard_gate,
)
from jstock_advisor.domain.signals.holding_decision_score import combine_holding_decision
from jstock_advisor.domain.signals.investment_thesis_scoring import (
    InvestmentThesisInputs,
    score_investment_thesis,
)
from jstock_advisor.domain.signals.risk_deduction_scoring import (
    RiskDeductionInputs,
    score_risk_deduction,
)
from jstock_advisor.domain.signals.sell_signal import build_sell_rule_inputs_from_data
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.holding_decision_runtime_config_service import (
    HoldingDecisionRuntimeConfigService,
)
from jstock_advisor.services.investment_thesis_service import InvestmentThesisService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.stock_snapshot_service import StockSnapshot, build_stock_snapshot

_HARD_GATE_RULE_NAMES = (
    "balance_sheet_insolvency",
    "major_scandal",
    "accounting_problem",
    "listing_maintenance_risk",
)


def _build_reason_impacts(
    company_quality: CompanyQualityScore,
    investment_thesis: InvestmentThesisScore,
    risk_deduction: RiskDeductionScore,
    top_positive_count: int,
    top_negative_count: int,
) -> tuple[tuple[ReasonImpact, ...], tuple[ReasonImpact, ...]]:
    """主な加点・減点要因を構造化データとして抽出する(15節)。

    企業品質・投資ストーリーの各評価項目は「満点=支持要因」「満点未満=減点要因」
    として扱い、リスク控除の各カテゴリは控除点そのものを減点要因として扱う。
    """
    impacts: list[ReasonImpact] = []
    for item in (*company_quality.items, *investment_thesis.items):
        if item.status != EvidenceCoverageStatus.EVALUATED or item.weight <= 0:
            continue
        gap = item.points_earned - item.weight
        if gap < 0:
            impacts.append(
                ReasonImpact(reason_code=item.item_code, category=item.axis, score_impact=gap)
            )
        elif item.points_earned >= item.weight:
            impacts.append(
                ReasonImpact(
                    reason_code=item.item_code, category=item.axis, score_impact=item.weight
                )
            )
    for category in risk_deduction.categories:
        if category.points > 0:
            impacts.append(
                ReasonImpact(
                    reason_code=category.category,
                    category="risk_deduction",
                    score_impact=-category.points,
                )
            )

    positive = sorted((i for i in impacts if i.score_impact > 0), key=lambda i: -i.score_impact)
    negative = sorted((i for i in impacts if i.score_impact < 0), key=lambda i: i.score_impact)
    return tuple(positive[:top_positive_count]), tuple(negative[:top_negative_count])


@dataclass(frozen=True)
class HoldingDecisionEvaluationOutcome:
    stock_code: str
    result: HoldingDecisionResult | None
    data_error: str | None = None
    integrity_error: bool = False


class HoldingDecisionService:
    def __init__(
        self,
        providers: ProviderBundle,
        config: AppConfig,
        investment_thesis_service: InvestmentThesisService | None = None,
        runtime_config_service: HoldingDecisionRuntimeConfigService | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self._providers = providers
        self._config = config
        self._thesis_service = investment_thesis_service or InvestmentThesisService()
        self._runtime_config_service = (
            runtime_config_service
            or HoldingDecisionRuntimeConfigService(
                cache_ttl_seconds=config.holding_decision.runtime_config_cache_ttl_seconds
            )
        )
        self._audit = audit_service or AuditService()

    def evaluate(
        self,
        holding: Holding,
        now: dt.datetime,
        execution_plan_reason: ExecutionPlanReason,
        snapshot: StockSnapshot | None = None,
        runtime_config_version: int | None = None,
        financial_model_version_used: int | None = None,
        legacy_reason_codes: tuple[str, ...] = (),
    ) -> HoldingDecisionEvaluationOutcome:
        """保有判断スコアを算出する。

        現時点の実装では、`holding`引数から読むのは`holding.stock_code`のみであり、
        `shares`/`average_purchase_price`/`total_purchase_amount`/`first_purchase_date`/
        `last_purchase_date`/`account_type`等の保有固有情報は一切参照しない
        (コードレビュー対応: これにより非保有銘柄でもダミー保有データを渡して
        安全に評価できる。backtest/compareの`placeholder_holding`が前提とする
        性質であり、`test_holding_decision_service_ignores_holding_specific_fields`
        で回帰確認している)。ただし、これは現時点の実装事実であり永続的な仕様として
        固定するものではない(将来、保有固有情報を使う設計変更があり得る)。
        """
        start = time.monotonic()
        rules = self._config.holding_decision

        error: str | None = None
        if snapshot is None:
            snapshot, error = build_stock_snapshot(
                self._providers, holding.stock_code, now, self._config
            )
        if snapshot is None:
            self._audit.record(
                decision_type="holding_decision",
                stock_code=holding.stock_code,
                input_values={},
                calculation_formulas={},
                output_values={"data_error": error},
                data_sources=[],
                rule_version=str(rules.scoring_model_version),
                timestamp=now,
            )
            return HoldingDecisionEvaluationOutcome(holding.stock_code, None, data_error=error)

        holding_id = holding.stock_code  # 現状stock_codeの1:1エイリアス(3節)
        industry = classify_industry(snapshot.financial.sector, snapshot.financial.industry)

        # --- 企業品質スコア -------------------------------------------------
        eps_period_values = [
            FinancialPeriodValue(value=hv.eps, period_end=hv.date, period_type=PeriodType.ANNUAL)
            for hv in snapshot.historical_valuations
            if hv.eps is not None
        ]
        listing_risk_keyword_confirmed = bool(snapshot.material_event_keywords_found)
        company_quality = score_company_quality(
            CompanyQualityInputs(
                financial=snapshot.financial,
                quarterly_operating_income_periods=snapshot.quarterly_operating_income_periods,
                quarterly_operating_cashflow_periods=snapshot.quarterly_operating_cashflow_periods,
                eps_period_values=eps_period_values,
                cashflow_decomposition=snapshot.cashflow_decomposition,
                industry_classification=industry,
                listing_risk_keyword_confirmed=listing_risk_keyword_confirmed,
            ),
            rules.company_quality_weights,
            rules.company_quality_score_thresholds,
            self._config.holding_decision_ratio,
        )

        # --- リスク控除スコア(既存sell_signal.pyのルール抽出部を再利用) --------
        sell_rule_inputs = build_sell_rule_inputs_from_data(
            dividend=snapshot.dividend,
            financial=snapshot.financial,
            benefit=snapshot.benefit,
            quarterly_operating_income_periods=snapshot.quarterly_operating_income_periods,
            quarterly_operating_cashflow_periods=snapshot.quarterly_operating_cashflow_periods,
            disclosure_risk_keywords_found=snapshot.disclosure_risk_keywords_found,
            config=self._config.sell,
            cashflow_decomposition=snapshot.cashflow_decomposition,
            material_event_keywords_found=snapshot.material_event_keywords_found,
        )
        risk_deduction = score_risk_deduction(
            RiskDeductionInputs(sell_rule_inputs=sell_rule_inputs),
            self._config.holding_decision_risk,
        )
        new_reason_codes = tuple(
            name
            for name, ev in sell_rule_inputs.evaluations.items()
            if ev.status == TriggerStatus.TRIGGERED
        )

        # --- Baseline確定 ----------------------------------------------------
        lookup = self._thesis_service.get_active_baseline(holding_id)
        if lookup.integrity_error:
            self._audit.record(
                decision_type="holding_decision",
                stock_code=holding.stock_code,
                input_values={"holding_id": holding_id},
                calculation_formulas={},
                output_values={"error": "DATA_INTEGRITY_ERROR"},
                data_sources=list(snapshot.data_sources),
                rule_version=str(rules.scoring_model_version),
                timestamp=now,
            )
            return HoldingDecisionEvaluationOutcome(holding.stock_code, None, integrity_error=True)

        has_benefit = snapshot.benefit is not None and not snapshot.benefit.is_abolished
        is_first_evaluation = lookup.baseline is None
        if lookup.baseline is None:
            baseline_values = BaselineValueSnapshot(
                total_yield_pct=snapshot.total_yield_pct,
                has_shareholder_benefit=has_benefit,
                equity_ratio_pct=snapshot.financial.equity_ratio_pct,
            )
            baseline = self._thesis_service.activate_baseline(
                holding_id,
                holding.stock_code,
                BaselineOrigin.SYSTEM_INITIALIZED,
                baseline_values,
                status=BaselineStatus.APPROVED,
                approved_by="system",
                max_retries=rules.baseline_activation_max_retries,
                now=now,
            )
        else:
            baseline = lookup.baseline

        if is_first_evaluation:
            benefit_abolished_or_downgraded = None
            profit_cf_premise_broken = None
            financial_premise_broken = None
        else:
            benefit_abolished_or_downgraded = snapshot.benefit is not None and (
                snapshot.benefit.is_abolished or snapshot.benefit.is_major_downgrade
            )
            profit_cf_premise_broken = any(
                sell_rule_inputs.evaluations.get(name, None) is not None
                and sell_rule_inputs.evaluations[name].status == TriggerStatus.TRIGGERED
                for name in (
                    "continuous_operating_income_decline",
                    "continuous_operating_cashflow_decline",
                )
            )
            financial_premise_broken = (
                sell_rule_inputs.evaluations.get("financial_health_severe_deterioration")
                is not None
                and sell_rule_inputs.evaluations["financial_health_severe_deterioration"].status
                == TriggerStatus.TRIGGERED
            )

        thesis = self._thesis_service.get_or_create_thesis(holding_id, holding.stock_code, now)

        dividend_cut_or_omission_confirmed = (
            snapshot.dividend.is_dividend_cut_announced
            or snapshot.dividend.is_dividend_omission_announced
        )

        investment_thesis = score_investment_thesis(
            InvestmentThesisInputs(
                current_total_yield_pct=snapshot.total_yield_pct,
                has_shareholder_benefit=has_benefit,
                benefit_abolished_or_downgraded=benefit_abolished_or_downgraded,
                dividend_cut_or_omission_confirmed=dividend_cut_or_omission_confirmed,
                profit_cf_premise_broken=profit_cf_premise_broken,
                financial_premise_broken=financial_premise_broken,
                thesis=thesis,
            ),
            rules.investment_thesis_weights,
            self._config.investment_thesis_template,
            rules.fresh_within_days,
            rules.stale_after_days,
            now,
            baseline_id=baseline.baseline_id,
            baseline_version=baseline.version,
            baseline_origin=baseline.origin,
        )

        # --- ハードゲート ------------------------------------------------------
        def _triggered_and_confirmed(rule_name: str) -> bool:
            ev = sell_rule_inputs.evaluations.get(rule_name)
            return (
                ev is not None
                and ev.status == TriggerStatus.TRIGGERED
                and ev.primary_source_confirmed
            )

        accounting_problem_confirmed = _triggered_and_confirmed("accounting_problem")
        listing_risk_confirmed = _triggered_and_confirmed("listing_maintenance_risk")
        major_scandal_confirmed = _triggered_and_confirmed("major_scandal")
        dividend_omission_confirmed = _triggered_and_confirmed("dividend_omission")
        financial_crisis_points = next(
            (c.points for c in risk_deduction.categories if c.category == "financial_crisis"), 0.0
        )

        investment_thesis_collapse_confirmed = (
            baseline.origin == BaselineOrigin.HUMAN_APPROVED
            and investment_thesis.score < rules.investment_thesis_collapse_threshold
        )

        hard_gate = evaluate_hard_gate(
            HardGateInputs(
                debt_excess_confirmed=(
                    snapshot.financial.is_debt_excess
                    and _triggered_and_confirmed("balance_sheet_insolvency")
                ),
                going_concern_doubt_confirmed=snapshot.financial.is_going_concern_doubt,
                bankruptcy_filing_confirmed=major_scandal_confirmed,
                delisting_or_kanri_confirmed=listing_risk_confirmed,
                accounting_fraud_confirmed=accounting_problem_confirmed,
                dividend_omission_and_cashflow_crisis_confirmed=(
                    dividend_omission_confirmed and financial_crisis_points > 0
                ),
                investment_thesis_collapse_confirmed=investment_thesis_collapse_confirmed,
            )
        )

        outcome = combine_holding_decision(
            company_quality, investment_thesis, risk_deduction, hard_gate, rules
        )

        positive_reasons, negative_reasons = _build_reason_impacts(
            company_quality,
            investment_thesis,
            risk_deduction,
            rules.top_positive_reasons_count,
            rules.top_negative_reasons_count,
        )

        duration_ms = int((time.monotonic() - start) * 1000)
        effective_runtime_config_version = (
            runtime_config_version
            if runtime_config_version is not None
            else self._runtime_config_service.get_config(now).effective_runtime_config_version
        )

        result = HoldingDecisionResult(
            holding_decision_result_id=str(uuid.uuid4()),
            holding_id=holding_id,
            stock_code=holding.stock_code,
            evaluated_at=now,
            company_quality=company_quality,
            investment_thesis=investment_thesis,
            risk_deduction=risk_deduction,
            base_score=outcome.base_score,
            hard_gate=outcome.hard_gate,
            final_score=outcome.final_score,
            display_value=outcome.display_value,
            category=outcome.category,
            coverage=outcome.coverage,
            confidence=outcome.confidence,
            should_notify=outcome.should_notify,
            baseline_id=baseline.baseline_id,
            baseline_version=baseline.version,
            baseline_origin=baseline.origin,
            scoring_model_version=rules.scoring_model_version,
            runtime_config_version=effective_runtime_config_version,
            financial_model_version_used=financial_model_version_used,
            execution_plan_reason=execution_plan_reason,
            evaluation_duration_ms=duration_ms,
            legacy_reason_codes=legacy_reason_codes,
            new_reason_codes=new_reason_codes,
            positive_reasons=positive_reasons,
            negative_reasons=negative_reasons,
            data_sources=tuple(snapshot.data_sources),
        )

        self._audit.record(
            decision_type="holding_decision",
            stock_code=holding.stock_code,
            input_values={"industry": industry.classification.value},
            calculation_formulas={
                "base_score": "company_quality + investment_thesis - risk_deduction",
                "final_score": (
                    "min(base_score, hard_gate_score_cap) if hard_gate_triggered else base_score"
                ),
            },
            output_values={
                "base_score": outcome.base_score,
                "final_score": outcome.final_score,
                "category": outcome.category.value,
                "should_notify": outcome.should_notify,
                "hard_gate_triggered": hard_gate.triggered,
                "hard_gate_reason_codes": list(hard_gate.reason_codes),
            },
            data_sources=list(snapshot.data_sources),
            rule_version=str(rules.scoring_model_version),
            timestamp=now,
        )

        return HoldingDecisionEvaluationOutcome(holding.stock_code, result)
