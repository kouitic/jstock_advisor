"""買い判定サービス(要求仕様3節 buy_signal_service)。

stock_snapshot_serviceで取得したデータをもとに、screening/scoring/buy_signalの
各ドメインロジックを組み合わせてRecommendationスナップショットを生成する。
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.scoring.score import compute_score
from jstock_advisor.domain.screening.rules import evaluate_screening
from jstock_advisor.domain.signals.buy_signal import (
    compute_drawdown_from_52w_high_pct,
    compute_recent_price_change_pct,
    compute_undervaluation_signals,
    estimate_historical_average_dividend_yield_pct,
    evaluate_buy_signal,
    is_earnings_trend_non_decreasing,
)
from jstock_advisor.domain.valuation.fair_value import median_historical_pbr, median_historical_per
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

RULE_VERSION_PLACEHOLDER = "v1-mvp"  # rule_version_service(要求仕様43節)実装までの暫定値


@dataclass(frozen=True)
class BuyAnalysisOutcome:
    stock_code: str
    recommendation: Recommendation | None
    screening_passed: bool
    exclusion_reasons: list[str]
    data_error: str | None


class BuySignalService:
    def __init__(
        self,
        providers: ProviderBundle,
        config: AppConfig,
        business_calendar: BusinessCalendar,
    ) -> None:
        self._providers = providers
        self._config = config
        self._calendar = business_calendar

    def analyze(
        self,
        stock_code: str,
        now: dt.datetime,
        recommendation_type: RecommendationType = RecommendationType.BUY,
    ) -> BuyAnalysisOutcome:
        snapshot, error = build_stock_snapshot(self._providers, stock_code, now, self._config)
        if snapshot is None:
            return BuyAnalysisOutcome(stock_code, None, False, [], error)

        screening_result = evaluate_screening(
            financial=snapshot.financial,
            dividend=snapshot.dividend,
            total_yield_pct=snapshot.total_yield_pct,
            average_trading_value_yen=snapshot.avg_trading_value,
            disclosure_risk_keywords_found=snapshot.disclosure_risk_keywords_found,
            data_fetched_at=snapshot.data_fetched_at,
            now=now,
            business_calendar=self._calendar,
            config=self._config.screening,
        )

        earnings_trend_non_decreasing = is_earnings_trend_non_decreasing(
            snapshot.quarterly_operating_incomes
        )
        financial = snapshot.financial
        current_price = snapshot.current_price
        current_per = (
            current_price / financial.forecast_eps
            if financial.forecast_eps is not None and financial.forecast_eps > 0
            else None
        )
        current_pbr = (
            current_price / financial.forecast_bps
            if financial.forecast_bps is not None and financial.forecast_bps > 0
            else None
        )
        per_median = median_historical_per(snapshot.historical_valuations)
        pbr_median = median_historical_pbr(snapshot.historical_valuations)
        historical_avg_dividend_yield_pct = estimate_historical_average_dividend_yield_pct(
            snapshot.dividend.previous_fiscal_year_dividend_per_share, snapshot.bars
        )
        drawdown_pct = compute_drawdown_from_52w_high_pct(current_price, snapshot.bars, now.date())
        recent_price_change_pct = compute_recent_price_change_pct(snapshot.bars, now.date(), 60)

        undervaluation_signals = compute_undervaluation_signals(
            current_price=current_price,
            current_per=current_per,
            historical_per_median=per_median,
            current_pbr=current_pbr,
            historical_pbr_median=pbr_median,
            current_dividend_yield_pct=snapshot.dividend_yield_pct,
            historical_average_dividend_yield_pct=historical_avg_dividend_yield_pct,
            drawdown_from_52w_high_pct=drawdown_pct,
            buy_prices=snapshot.buy_prices,
            recent_price_change_pct=recent_price_change_pct,
            earnings_trend_non_decreasing=earnings_trend_non_decreasing,
            severe_earnings_decline=snapshot.severe_earnings_decline,
        )

        score_result = compute_score(
            total_yield_pct=snapshot.total_yield_pct,
            dividend=snapshot.dividend,
            financial=financial,
            undervaluation_signals=undervaluation_signals,
            benefit_yield_pct=snapshot.benefit_yield_pct,
            quarterly_operating_incomes=snapshot.quarterly_operating_incomes,
            price_bars=snapshot.bars,
            min_equity_ratio_pct=self._config.screening.financial_health.min_equity_ratio_pct,
            max_payout_ratio_pct=self._config.screening.financial_health.max_payout_ratio_pct,
            config=self._config.scoring,
        )

        data_age_days = self._calendar.business_days_between(
            snapshot.data_fetched_at.date(), now.date()
        )
        has_stale_data_warning = data_age_days > 1

        buy_result = evaluate_buy_signal(
            screening_result=screening_result,
            severe_earnings_decline=snapshot.severe_earnings_decline,
            benefit=snapshot.benefit,
            score_result=score_result,
            scoring_config=self._config.scoring,
            fair_value=snapshot.fair_value,
            buy_prices=snapshot.buy_prices,
            fair_value_methods_used_count=snapshot.fair_value_methods_used_count,
            data_sources_count=len(snapshot.data_sources),
            has_stale_data_warning=has_stale_data_warning,
        )

        if not buy_result.recommended:
            return BuyAnalysisOutcome(
                stock_code, None, screening_result.passed, buy_result.exclusion_reasons, None
            )

        if snapshot.fair_value is None or snapshot.buy_prices is None:
            raise AssertionError("recommended=Trueのときfair_value/buy_pricesはNoneにならない想定")

        dividend = snapshot.dividend
        benefit = snapshot.benefit
        recommendation = Recommendation(
            recommendation_id=str(uuid.uuid4()),
            stock_code=stock_code,
            stock_name=financial.stock_name or stock_code,
            recommended_at=now,
            recommendation_type=recommendation_type,
            buy_prices=snapshot.buy_prices,
            price_at_recommendation=current_price,
            dividend_yield_pct_at_recommendation=snapshot.dividend_yield_pct,
            shareholder_benefit_yield_pct_at_recommendation=snapshot.benefit_yield_pct,
            total_yield_pct_at_recommendation=snapshot.total_yield_pct,
            fair_value_at_recommendation=snapshot.fair_value,
            total_score=score_result.breakdown.total,
            score_breakdown=score_result.breakdown,
            reasons=buy_result.positive_reasons,
            counter_factors=buy_result.counter_factors,
            key_risks=buy_result.key_risks,
            confidence=buy_result.confidence,
            next_earnings_date=snapshot.next_earnings_date,
            dividend_record_date=dividend.dividend_record_dates[0]
            if dividend.dividend_record_dates
            else None,
            benefit_record_date=benefit.benefit_record_dates[0]
            if benefit is not None and benefit.benefit_record_dates
            else None,
            rule_version=RULE_VERSION_PLACEHOLDER,
            config_values_used={
                "min_total_yield_pct": self._config.screening.total_yield.min_total_yield_pct,
                "aggregation_method": self._config.valuation.fair_value_methods.aggregation_method,
            },
            data_sources=list(snapshot.data_sources),
        )

        return BuyAnalysisOutcome(stock_code, recommendation, True, [], None)
