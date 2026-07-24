"""複数サービスで共有する銘柄分析スナップショット構築処理。

buy_signal_service / profit_taking_service / sell_signal_service はいずれも
現在株価・財務・配当・優待・適正価格といった同じ基礎データを必要とするため、
取得と適正価格算出をここに集約する。データが取得できない場合はエラーメッセージを
返し、推測で補完しない。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.common import (
    BenefitUtilityCoefficients,
    BuyPriceLevels,
    DataSourceReference,
)
from jstock_advisor.domain.screening.rules import detect_disclosure_risk_keywords
from jstock_advisor.domain.signals.buy_signal import has_severe_earnings_decline
from jstock_advisor.domain.valuation.buy_price import compute_recommended_buy_prices
from jstock_advisor.domain.valuation.fair_value import (
    aggregate_fair_value,
    compute_historical_range_price,
    compute_pbr_price,
    compute_per_price,
    compute_target_yield_price,
    median_historical_pbr,
    median_historical_per,
)
from jstock_advisor.domain.valuation.yield_calc import (
    compute_annual_benefit_value,
    compute_benefit_yield_pct,
    compute_dividend_yield_pct,
    compute_total_yield_pct,
)
from jstock_advisor.interfaces.types import (
    Disclosure,
    DividendInfo,
    FinancialSummary,
    HistoricalValuation,
    PriceBar,
    ShareholderBenefit,
)
from jstock_advisor.services.provider_bundle import ProviderBundle


@dataclass(frozen=True)
class StockSnapshot:
    stock_code: str
    current_price: Decimal
    financial: FinancialSummary
    dividend: DividendInfo
    benefit: ShareholderBenefit | None
    bars: list[PriceBar]
    historical_valuations: list[HistoricalValuation]
    avg_trading_value: Decimal | None
    disclosures: list[Disclosure]
    next_earnings_date: dt.date | None
    dividend_yield_pct: float | None
    benefit_yield_pct: float | None
    total_yield_pct: float
    fair_value: Decimal | None
    buy_prices: BuyPriceLevels | None
    fair_value_methods_used_count: int
    data_sources: list[DataSourceReference]
    data_fetched_at: dt.datetime
    quarterly_operating_incomes: list[Decimal]
    quarterly_operating_cashflows: list[Decimal]
    severe_earnings_decline: bool
    disclosure_risk_keywords_found: list[str]


def build_stock_snapshot(
    providers: ProviderBundle,
    stock_code: str,
    now: dt.datetime,
    config: AppConfig,
) -> tuple[StockSnapshot | None, str | None]:
    snap = providers.market_data.get_latest_price(stock_code)
    if snap is None:
        return None, "株価データを取得できません"

    financial = providers.financial_data.get_financial_summary(stock_code)
    if financial is None:
        return None, "財務データを取得できません"

    dividend = providers.dividend_data.get_dividend_info(stock_code)
    if dividend is None:
        return None, "配当データを取得できません"

    benefit = providers.shareholder_benefit.get_shareholder_benefit(stock_code)
    current_price = snap.close_price

    history_start = now.date() - dt.timedelta(
        days=365 * config.valuation.historical_range_method.lookback_years
    )
    history = providers.market_data.get_price_history(stock_code, history_start, now.date())
    bars = history.bars if history is not None else []

    historical_valuations = providers.financial_data.get_historical_valuation(
        stock_code, config.valuation.per_method.lookback_years_primary
    )
    avg_trading_value = providers.market_data.get_average_trading_value(stock_code, 20)
    disclosures = providers.disclosure.get_disclosures(
        stock_code, now.date() - dt.timedelta(days=30)
    )
    next_earnings_date = providers.disclosure.get_next_earnings_date(stock_code)

    coefficients = BenefitUtilityCoefficients(
        **config.scoring.shareholder_benefit_value.utility_coefficients_default.model_dump()
    )
    dividend_yield_pct = compute_dividend_yield_pct(
        dividend.forecast_annual_dividend_per_share, current_price
    )
    annual_benefit_value = compute_annual_benefit_value(benefit, coefficients)
    min_shares_required = benefit.min_shares_required if benefit is not None else 100
    benefit_yield_pct = compute_benefit_yield_pct(
        annual_benefit_value, min_shares_required, current_price
    )
    total_yield_pct = compute_total_yield_pct(dividend_yield_pct, benefit_yield_pct)

    target_price = compute_target_yield_price(
        dividend.forecast_annual_dividend_per_share,
        config.valuation.target_yield_method.target_dividend_yield_pct,
    )
    per_median = median_historical_per(historical_valuations)
    pbr_median = median_historical_pbr(historical_valuations)
    per_price = compute_per_price(financial.forecast_eps, per_median)
    pbr_price = compute_pbr_price(financial.forecast_bps, pbr_median)
    range_price = compute_historical_range_price(
        bars,
        now.date(),
        config.valuation.historical_range_method.lookback_years,
        config.valuation.historical_range_method.use_52_week_low,
    )
    fair_value_candidates = {
        "target_yield": target_price,
        "per": per_price,
        "pbr": pbr_price,
        "historical_range": range_price,
    }
    fair_value_methods_used_count = sum(1 for v in fair_value_candidates.values() if v is not None)
    fair_value = aggregate_fair_value(
        fair_value_candidates,
        config.valuation.fair_value_methods.aggregation_method,
        config.valuation.fair_value_methods.method_weights,
    )
    buy_prices = (
        compute_recommended_buy_prices(fair_value, config.valuation.recommended_buy_price)
        if fair_value is not None
        else None
    )

    data_sources = [snap.source, financial.source, dividend.source]
    if benefit is not None:
        data_sources.append(benefit.source)
    data_fetched_at = min(s.fetched_at for s in data_sources)

    keywords_found = detect_disclosure_risk_keywords(
        disclosures, config.sell.disclosure_risk_keywords
    )

    quarterly_operating_incomes = [
        q.operating_income for q in financial.recent_quarters if q.operating_income is not None
    ]
    quarterly_operating_cashflows = [
        q.operating_cashflow for q in financial.recent_quarters if q.operating_cashflow is not None
    ]
    severe_earnings_decline = has_severe_earnings_decline(quarterly_operating_incomes)

    snapshot = StockSnapshot(
        stock_code=stock_code,
        current_price=current_price,
        financial=financial,
        dividend=dividend,
        benefit=benefit,
        bars=bars,
        historical_valuations=historical_valuations,
        avg_trading_value=avg_trading_value,
        disclosures=disclosures,
        next_earnings_date=next_earnings_date,
        dividend_yield_pct=dividend_yield_pct,
        benefit_yield_pct=benefit_yield_pct,
        total_yield_pct=total_yield_pct,
        fair_value=fair_value,
        buy_prices=buy_prices,
        fair_value_methods_used_count=fair_value_methods_used_count,
        data_sources=data_sources,
        data_fetched_at=data_fetched_at,
        quarterly_operating_incomes=quarterly_operating_incomes,
        quarterly_operating_cashflows=quarterly_operating_cashflows,
        severe_earnings_decline=severe_earnings_decline,
        disclosure_risk_keywords_found=keywords_found,
    )
    return snapshot, None
