"""投資前提悪化による売却判定サービス(2026-07仕様: 判定エンジンの再設計)。

保有銘柄についてstock_snapshot_serviceでデータを取得し、sell_signalドメイン
ロジックで判定したうえでRecommendationスナップショットを生成する。
株価の下落そのものは判定材料に含めない。

信頼度はConfidenceLevel.HIGHを決め打ちせず、confidence_scoringで実際に算出する。
根拠がすべてyfinance等の二次情報のみの場合、SELL/URGENT_REVIEWをREVIEWへ
自動的に格下げする(要求仕様§12: yfinance単独で強い売却判定を出さない)。
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.common import SellPriceLevels
from jstock_advisor.domain.entities.enums import RecommendationType, TriggerStatus
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.confidence_scoring import (
    ConfidenceFactors,
    ConfidenceScoreResult,
    compute_confidence,
)
from jstock_advisor.domain.signals.sell_signal import (
    SellRuleEvaluation,
    SellSignalResult,
    build_sell_rule_inputs_from_data,
    evaluate_sell_signal,
)
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.buy_signal_service import RULE_VERSION_PLACEHOLDER
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.rule_version_service import RuleVersionService
from jstock_advisor.services.stock_snapshot_service import StockSnapshot, build_stock_snapshot

_STRONG_TYPES = (RecommendationType.SELL, RecommendationType.URGENT_REVIEW)


@dataclass(frozen=True)
class SellSignalOutcome:
    stock_code: str
    recommendation: Recommendation | None
    data_error: str | None


def _evidence_detail_dict(e: SellRuleEvaluation) -> dict[str, object]:
    return {
        "rule_name": e.rule_name,
        "status": e.status.value,
        "severity": e.severity,
        "evidence_group": e.evidence_group.value,
        "is_immediate_critical": e.is_immediate_critical,
        "metric_name": e.metric_name,
        "current_value": e.current_value,
        "previous_value": e.previous_value,
        "threshold": e.threshold,
        "comparison_period": e.comparison_period,
        "primary_source_confirmed": e.primary_source_confirmed,
        "source": e.source,
        "explanation": e.explanation,
    }


def _build_action_summary(recommendation_type: RecommendationType) -> str:
    if recommendation_type == RecommendationType.URGENT_REVIEW:
        return "即時性の高い重大な悪化事象が検出されました。速やかに内容を確認してください。"
    if recommendation_type == RecommendationType.SELL:
        return "複数の独立した根拠に基づき投資前提の悪化が疑われます。売却を検討してください。"
    return (
        "投資前提の悪化を示唆する事象が検出されましたが、根拠が単一のため自動的な"
        "売却判断は行いません。内容を確認し、必要に応じて追加情報を収集してください。"
    )


def _build_counter_factors(dividend: object, benefit: object) -> list[str]:
    factors: list[str] = []
    increase_years = getattr(dividend, "consecutive_dividend_increase_years", None)
    if increase_years is not None and increase_years > 0:
        factors.append(f"配当は{increase_years}期連続増配中")
    if (
        benefit is not None
        and not getattr(benefit, "is_abolished", False)
        and not getattr(benefit, "is_major_downgrade", False)
    ):
        factors.append("株主優待は継続しており、廃止・大幅改悪は確認されていない")
    return factors


def _build_next_review_conditions(
    evidence_details: list[SellRuleEvaluation], next_earnings_date: dt.date | None
) -> list[str]:
    conditions: list[str] = []
    if next_earnings_date is not None:
        conditions.append(f"次回決算発表({next_earnings_date})後に本判定を再評価する")
    if any(
        e.rule_name in ("financial_health_severe_deterioration", "regulatory_capital_breach")
        and e.status == TriggerStatus.NOT_EVALUATED
        for e in evidence_details
    ):
        conditions.append("銀行・保険等専用の財務健全性指標が取得可能になり次第、再評価する")
    return conditions


def _build_holding_risks(evidence_details: list[SellRuleEvaluation]) -> list[str]:
    return [
        e.explanation
        for e in evidence_details
        if e.status == TriggerStatus.TRIGGERED
        and e.severity in ("critical", "major")
        and e.explanation
    ]


class SellSignalService:
    def __init__(
        self,
        providers: ProviderBundle,
        config: AppConfig,
        audit_service: AuditService | None = None,
        rule_version_service: RuleVersionService | None = None,
    ) -> None:
        self._providers = providers
        self._config = config
        self._audit = audit_service or AuditService()
        self._rule_version_service = rule_version_service or RuleVersionService()

    def _active_rule_version(self) -> str:
        return self._rule_version_service.get_active_version_or(RULE_VERSION_PLACEHOLDER)

    def _compute_confidence_level(
        self,
        result: SellSignalResult,
        snapshot: StockSnapshot,
        now: dt.datetime,
    ) -> ConfidenceScoreResult:
        industry_unevaluated = any(
            e.rule_name in ("financial_health_severe_deterioration", "regulatory_capital_breach")
            and e.status == TriggerStatus.NOT_EVALUATED
            for e in result.evidence_details
        )
        data_freshness_days = (now - snapshot.data_fetched_at).days
        days_to_earnings = None
        if snapshot.next_earnings_date is not None:
            days_to_earnings = (snapshot.next_earnings_date - now.date()).days

        factors = ConfidenceFactors(
            data_freshness_days=data_freshness_days,
            primary_source_fetch_rate=(
                sum(1 for e in result.evidence_details if e.primary_source_confirmed)
                / len(result.evidence_details)
                if result.evidence_details
                else None
            ),
            days_to_next_earnings_business_days=days_to_earnings,
            latest_quarter_fetched=bool(snapshot.quarterly_operating_incomes),
            record_date_known=snapshot.dividend.dividend_record_date is not None,
            key_metric_missing=snapshot.financial.equity_ratio_pct is None,
            independent_evidence_group_count=result.independent_evidence_group_count,
            industry_specific_model_unavailable=industry_unevaluated,
            evidence_sourced_from_yfinance_only=result.all_evidence_yfinance_only,
            dividend_breakdown_confirmed=snapshot.dividend.dividend_breakdown_confirmed,
            counter_factors_evaluated=True,
        )
        return compute_confidence(factors, self._config.confidence)

    def analyze(
        self, holding: Holding, now: dt.datetime, snapshot: StockSnapshot | None = None
    ) -> SellSignalOutcome:
        """snapshotを渡すと再取得を省略する(profit_takingと同一銘柄を二重に取得する
        無駄を避けるため、呼び出し側で一度だけ取得して両方に渡すことを想定)。"""
        error: str | None = None
        if snapshot is None:
            snapshot, error = build_stock_snapshot(
                self._providers, holding.stock_code, now, self._config
            )
        if snapshot is None:
            self._audit.record(
                decision_type="sell_signal",
                stock_code=holding.stock_code,
                input_values={},
                calculation_formulas={},
                output_values={"data_error": error},
                data_sources=[],
                rule_version=self._active_rule_version(),
                timestamp=now,
            )
            return SellSignalOutcome(holding.stock_code, None, error)

        inputs = build_sell_rule_inputs_from_data(
            dividend=snapshot.dividend,
            financial=snapshot.financial,
            benefit=snapshot.benefit,
            quarterly_operating_incomes=snapshot.quarterly_operating_incomes,
            quarterly_operating_cashflows=snapshot.quarterly_operating_cashflows,
            disclosure_risk_keywords_found=snapshot.disclosure_risk_keywords_found,
            config=self._config.sell,
            cashflow_decomposition=snapshot.cashflow_decomposition,
        )

        result = evaluate_sell_signal(inputs, snapshot.current_price, self._config.sell)

        recommendation_type = result.recommendation_type
        downgraded_reason: str | None = None
        if recommendation_type in _STRONG_TYPES and result.all_evidence_yfinance_only:
            # 要求仕様§12: 根拠がすべてyfinance等の二次情報のみの場合、SELL/URGENT_REVIEWを
            # 出さずREVIEWへ格下げする。
            downgraded_reason = (
                f"{recommendation_type.value}の根拠がすべて一次情報未確認のためREVIEWへ格下げ"
            )
            recommendation_type = RecommendationType.REVIEW

        confidence_result = self._compute_confidence_level(result, snapshot, now)

        self._audit.record(
            decision_type="sell_signal",
            stock_code=holding.stock_code,
            input_values=inputs.as_dict(),
            calculation_formulas={
                "judgment": (
                    "即時性criticalが1件以上 -> URGENT_REVIEW; "
                    "独立major2件以上 or 独立critical2件以上 or "
                    "(critical+独立major1件以上) -> SELL; "
                    "major/criticalいずれか1件以上 -> REVIEW; それ以外 -> HOLD; "
                    "根拠が全てyfinance等の二次情報のみの場合はSELL/URGENT_REVIEWをREVIEWへ格下げ"
                ),
            },
            output_values={
                "recommendation_type": recommendation_type.value,
                "raw_recommendation_type": result.recommendation_type.value,
                "downgraded_reason": downgraded_reason,
                "triggered_rules": result.triggered_rules,
                "reasons": result.reasons,
                "independent_evidence_group_count": result.independent_evidence_group_count,
                "confidence": confidence_result.level.value,
                "confidence_score": confidence_result.score,
                "confidence_reasons": confidence_result.reasons_not_high,
            },
            data_sources=list(snapshot.data_sources),
            rule_version=self._active_rule_version(),
            timestamp=now,
        )

        if recommendation_type == RecommendationType.HOLD:
            return SellSignalOutcome(holding.stock_code, None, None)

        sell_prices = SellPriceLevels(
            immediate_execution_price=result.immediate_execution_price,
            stop_review_price=result.stop_review_price,
        )

        counter_factors = _build_counter_factors(snapshot.dividend, snapshot.benefit)
        evidence_details = result.evidence_details

        recommendation = Recommendation(
            recommendation_id=str(uuid.uuid4()),
            stock_code=holding.stock_code,
            stock_name=snapshot.financial.stock_name or holding.stock_name,
            recommended_at=now,
            recommendation_type=recommendation_type,
            sell_prices=sell_prices,
            price_at_recommendation=snapshot.current_price,
            average_purchase_price_at_recommendation=holding.average_purchase_price,
            shares_at_recommendation=holding.shares,
            dividend_yield_pct_at_recommendation=snapshot.dividend_yield_pct,
            shareholder_benefit_yield_pct_at_recommendation=snapshot.benefit_yield_pct,
            total_yield_pct_at_recommendation=snapshot.total_yield_pct,
            fair_value_at_recommendation=snapshot.fair_value,
            reasons=result.reasons,
            counter_factors=counter_factors,
            key_risks=[f"該当ルール: {', '.join(result.triggered_rules)}"],
            confidence=confidence_result.level,
            next_earnings_date=snapshot.next_earnings_date,
            dividend_record_date=snapshot.dividend.dividend_record_dates[0]
            if snapshot.dividend.dividend_record_dates
            else None,
            benefit_record_date=snapshot.benefit.benefit_record_dates[0]
            if snapshot.benefit is not None and snapshot.benefit.benefit_record_dates
            else None,
            rule_version=self._active_rule_version(),
            config_values_used={
                "triggered_rules": result.triggered_rules,
                "independent_evidence_group_count": result.independent_evidence_group_count,
                "downgraded_reason": downgraded_reason,
            },
            data_sources=list(snapshot.data_sources),
            recommended_action_summary=_build_action_summary(recommendation_type),
            next_review_conditions=_build_next_review_conditions(
                evidence_details, snapshot.next_earnings_date
            ),
            holding_risks=_build_holding_risks(evidence_details),
            evidence_details=[_evidence_detail_dict(e) for e in evidence_details],
            independent_evidence_group_count=result.independent_evidence_group_count,
        )
        return SellSignalOutcome(holding.stock_code, recommendation, None)
