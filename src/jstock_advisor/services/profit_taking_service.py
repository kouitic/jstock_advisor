"""利確判定サービス(要求仕様3節 profit_taking_service)。

保有銘柄についてstock_snapshot_serviceでデータを取得し、profit_takingドメイン
ロジックで判定したうえでRecommendationスナップショットを生成する。
"""

from __future__ import annotations

import calendar
import datetime as dt
import uuid
from dataclasses import dataclass

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.enums import AccountType, ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.profit_taking import (
    MitigatingFactorInputs,
    evaluate_profit_taking,
)
from jstock_advisor.interfaces.types import ShareholderBenefit
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.buy_signal_service import RULE_VERSION_PLACEHOLDER
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot


@dataclass(frozen=True)
class ProfitTakingOutcome:
    stock_code: str
    recommendation: Recommendation | None
    data_error: str | None


class ProfitTakingService:
    def __init__(
        self,
        providers: ProviderBundle,
        config: AppConfig,
        audit_service: AuditService | None = None,
    ) -> None:
        self._providers = providers
        self._config = config
        self._audit = audit_service or AuditService()

    def analyze(self, holding: Holding, now: dt.datetime) -> ProfitTakingOutcome:
        snapshot, error = build_stock_snapshot(
            self._providers, holding.stock_code, now, self._config
        )
        if snapshot is None:
            self._audit.record(
                decision_type="profit_taking",
                stock_code=holding.stock_code,
                input_values={},
                calculation_formulas={},
                output_values={"data_error": error},
                data_sources=[],
                rule_version=RULE_VERSION_PLACEHOLDER,
                timestamp=now,
            )
            return ProfitTakingOutcome(holding.stock_code, None, error)

        mitigating_inputs = MitigatingFactorInputs(
            fair_value_rising_with_earnings_growth=(
                snapshot.fair_value is not None
                and not snapshot.severe_earnings_decline
                and all(
                    snapshot.quarterly_operating_incomes[i]
                    >= snapshot.quarterly_operating_incomes[i - 1]
                    for i in range(1, len(snapshot.quarterly_operating_incomes))
                )
                if len(snapshot.quarterly_operating_incomes) >= 2
                else False
            ),
            continuous_dividend_increase_years=(
                snapshot.dividend.consecutive_dividend_increase_years or 0
            ),
            is_progressive_or_doe_policy=snapshot.dividend.is_progressive_or_doe_policy,
            long_term_holding_benefit_imminent=_is_long_term_benefit_imminent(
                holding, snapshot.benefit, now, self._config
            ),
            few_reinvestment_alternatives=False,  # 将来: 買い候補件数から動的算出する拡張ポイント
            is_nisa_account=holding.account_type == AccountType.NISA,
        )

        result = evaluate_profit_taking(
            current_price=snapshot.current_price,
            average_purchase_price=holding.average_purchase_price,
            shares=holding.shares,
            total_purchase_amount=holding.total_purchase_amount,
            cumulative_dividend_received=holding.cumulative_dividend_received,
            cumulative_benefit_value_received=holding.cumulative_benefit_value_received,
            fair_value=snapshot.fair_value,
            current_total_yield_pct=snapshot.total_yield_pct,
            forecast_annual_dividend_per_share=snapshot.dividend.forecast_annual_dividend_per_share,
            mitigating_inputs=mitigating_inputs,
            config=self._config.profit_taking,
        )

        self._audit.record(
            decision_type="profit_taking",
            stock_code=holding.stock_code,
            input_values={
                "current_price": str(snapshot.current_price),
                "average_purchase_price": str(holding.average_purchase_price),
                "shares": holding.shares,
                "fair_value": (
                    str(snapshot.fair_value) if snapshot.fair_value is not None else None
                ),
                "current_total_yield_pct": snapshot.total_yield_pct,
                "consecutive_dividend_increase_years": (
                    mitigating_inputs.continuous_dividend_increase_years
                ),
                "is_progressive_or_doe_policy": mitigating_inputs.is_progressive_or_doe_policy,
                "is_nisa_account": mitigating_inputs.is_nisa_account,
            },
            calculation_formulas={
                "unrealized_pnl_pct": "(current_price / average_purchase_price - 1) * 100",
                "total_return_pct": (
                    "(unrealized_pnl + cumulative_dividend + cumulative_benefit) "
                    "/ total_purchase_amount * 100"
                ),
            },
            output_values={
                "recommendation_type": result.recommendation_type.value,
                "triggered_reasons": result.triggered_reasons,
                "mitigating_factors_applied": result.mitigating_factors_applied,
                "unrealized_pnl_pct": result.pnl.unrealized_pnl_pct,
                "total_return_pct": result.pnl.total_return_pct,
            },
            data_sources=list(snapshot.data_sources),
            rule_version=RULE_VERSION_PLACEHOLDER,
            timestamp=now,
        )

        if result.recommendation_type == RecommendationType.HOLD:
            return ProfitTakingOutcome(holding.stock_code, None, None)

        recommendation = Recommendation(
            recommendation_id=str(uuid.uuid4()),
            stock_code=holding.stock_code,
            stock_name=holding.stock_name,
            recommended_at=now,
            recommendation_type=result.recommendation_type,
            sell_prices=result.sell_prices,
            price_at_recommendation=snapshot.current_price,
            average_purchase_price_at_recommendation=holding.average_purchase_price,
            shares_at_recommendation=holding.shares,
            dividend_yield_pct_at_recommendation=snapshot.dividend_yield_pct,
            shareholder_benefit_yield_pct_at_recommendation=snapshot.benefit_yield_pct,
            total_yield_pct_at_recommendation=snapshot.total_yield_pct,
            fair_value_at_recommendation=snapshot.fair_value,
            reasons=result.triggered_reasons,
            counter_factors=result.mitigating_factors_applied,
            key_risks=[
                f"含み損益率{result.pnl.unrealized_pnl_pct:.1f}%",
                f"配当・優待込み累計利益率{result.pnl.total_return_pct:.1f}%",
            ],
            confidence=_confidence_for(snapshot.fair_value_methods_used_count),
            next_earnings_date=snapshot.next_earnings_date,
            dividend_record_date=snapshot.dividend.dividend_record_dates[0]
            if snapshot.dividend.dividend_record_dates
            else None,
            benefit_record_date=snapshot.benefit.benefit_record_dates[0]
            if snapshot.benefit is not None and snapshot.benefit.benefit_record_dates
            else None,
            rule_version=RULE_VERSION_PLACEHOLDER,
            config_values_used={
                "unrealized_gain_full_pct": (
                    self._config.profit_taking.thresholds.unrealized_gain_full_pct
                ),
                "total_yield_strong_caution_pct": (
                    self._config.profit_taking.thresholds.total_yield_strong_caution_pct
                ),
            },
            data_sources=list(snapshot.data_sources),
        )
        return ProfitTakingOutcome(holding.stock_code, recommendation, None)


def _confidence_for(fair_value_methods_used_count: int) -> ConfidenceLevel:
    if fair_value_methods_used_count >= 3:
        return ConfidenceLevel.HIGH
    if fair_value_methods_used_count >= 1:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


def _is_long_term_benefit_imminent(
    holding: Holding,
    benefit: ShareholderBenefit | None,
    now: dt.datetime,
    config: AppConfig,
) -> bool:
    """保有銘柄が優待の長期保有条件をまもなく満たすかどうかを判定する。

    ShareholderBenefit.benefits内のlong_term_holding_condition_monthsと
    Holding.first_purchase_dateから、条件達成日が設定のwithin_business_days以内かを見る。
    """
    if benefit is None:
        return False

    mitigating = config.profit_taking.mitigating_factors.long_term_holding_benefit_imminent
    within_days = mitigating.within_business_days
    if within_days is None:
        return False

    for detail in benefit.benefits:
        months = detail.long_term_holding_condition_months
        if months is None:
            continue
        qualify_date = _add_months(holding.first_purchase_date, months)
        days_remaining = (qualify_date - now.date()).days
        if 0 <= days_remaining <= within_days * 2:  # 営業日ベースの概算(週末考慮の簡易マージン)
            return True
    return False


def _add_months(date: dt.date, months: int) -> dt.date:
    month_index = date.month - 1 + months
    year = date.year + month_index // 12
    month = month_index % 12 + 1
    day = min(date.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)
