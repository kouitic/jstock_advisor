"""投資前提悪化による売却判定サービス(要求仕様3節 sell_signal_service)。

保有銘柄についてstock_snapshot_serviceでデータを取得し、sell_signalドメイン
ロジックで判定したうえでRecommendationスナップショットを生成する。
株価の下落そのものは判定材料に含めない。
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.common import SellPriceLevels
from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.sell_signal import (
    build_sell_rule_inputs_from_data,
    evaluate_sell_signal,
)
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.buy_signal_service import RULE_VERSION_PLACEHOLDER
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.rule_version_service import RuleVersionService
from jstock_advisor.services.stock_snapshot_service import StockSnapshot, build_stock_snapshot


@dataclass(frozen=True)
class SellSignalOutcome:
    stock_code: str
    recommendation: Recommendation | None
    data_error: str | None


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

        self._audit.record(
            decision_type="sell_signal",
            stock_code=holding.stock_code,
            input_values=inputs.as_dict(),
            calculation_formulas={
                "judgment": (
                    "critical_count>=critical_to_urgent_review_min_count or "
                    "major_count>=major_to_urgent_review_min_count -> URGENT_REVIEW; "
                    "major_count>=major_to_sell_min_count or critical_count>=1 -> SELL; "
                    "それ以外 -> HOLD"
                ),
            },
            output_values={
                "recommendation_type": result.recommendation_type.value,
                "triggered_rules": result.triggered_rules,
                "reasons": result.reasons,
            },
            data_sources=list(snapshot.data_sources),
            rule_version=self._active_rule_version(),
            timestamp=now,
        )

        if result.recommendation_type == RecommendationType.HOLD:
            return SellSignalOutcome(holding.stock_code, None, None)

        sell_prices = SellPriceLevels(stop_review_price=result.stop_review_price)

        recommendation = Recommendation(
            recommendation_id=str(uuid.uuid4()),
            stock_code=holding.stock_code,
            stock_name=snapshot.financial.stock_name or holding.stock_name,
            recommended_at=now,
            recommendation_type=result.recommendation_type,
            sell_prices=sell_prices,
            price_at_recommendation=snapshot.current_price,
            average_purchase_price_at_recommendation=holding.average_purchase_price,
            shares_at_recommendation=holding.shares,
            dividend_yield_pct_at_recommendation=snapshot.dividend_yield_pct,
            shareholder_benefit_yield_pct_at_recommendation=snapshot.benefit_yield_pct,
            total_yield_pct_at_recommendation=snapshot.total_yield_pct,
            fair_value_at_recommendation=snapshot.fair_value,
            reasons=result.reasons,
            counter_factors=[],
            key_risks=[f"該当ルール: {', '.join(result.triggered_rules)}"],
            confidence=ConfidenceLevel.HIGH,
            next_earnings_date=snapshot.next_earnings_date,
            dividend_record_date=snapshot.dividend.dividend_record_dates[0]
            if snapshot.dividend.dividend_record_dates
            else None,
            benefit_record_date=snapshot.benefit.benefit_record_dates[0]
            if snapshot.benefit is not None and snapshot.benefit.benefit_record_dates
            else None,
            rule_version=self._active_rule_version(),
            config_values_used={
                "major_to_sell_min_count": self._config.sell.judgment.major_to_sell_min_count,
                "critical_to_urgent_review_min_count": (
                    self._config.sell.judgment.critical_to_urgent_review_min_count
                ),
                "triggered_rules": result.triggered_rules,
            },
            data_sources=list(snapshot.data_sources),
        )
        return SellSignalOutcome(holding.stock_code, recommendation, None)
