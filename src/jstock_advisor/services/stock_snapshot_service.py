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
from jstock_advisor.domain.classification.stock_type import classify_stock_type
from jstock_advisor.domain.entities.classification import StockTypeClassification
from jstock_advisor.domain.entities.common import (
    BenefitUtilityCoefficients,
    BuyPriceLevels,
    DataSourceReference,
)
from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.entities.momentum import MomentumSnapshot
from jstock_advisor.domain.entities.valuation import FairValueMethodResult, FairValueRange
from jstock_advisor.domain.financial_series import to_seasonally_adjusted_series
from jstock_advisor.domain.screening.rules import detect_disclosure_risk_keywords
from jstock_advisor.domain.signals.buy_signal import has_severe_earnings_decline
from jstock_advisor.domain.signals.momentum import compute_momentum_snapshot
from jstock_advisor.domain.valuation.buy_price import compute_recommended_buy_prices
from jstock_advisor.domain.valuation.fair_value import (
    aggregate_fair_value,
    compute_dcf_price,
    compute_historical_range_price,
    compute_pbr_price,
    compute_per_price,
    compute_target_yield_price,
    median_historical_pbr,
    median_historical_per,
)
from jstock_advisor.domain.valuation.fair_value_usability import build_fair_value_range
from jstock_advisor.domain.valuation.yield_calc import (
    compute_annual_benefit_value,
    compute_benefit_yield_pct,
    compute_dividend_yield_pct,
    compute_total_yield_pct,
)
from jstock_advisor.interfaces.types import (
    CashflowDecomposition,
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
    cashflow_decomposition: CashflowDecomposition | None
    stock_type_classification: StockTypeClassification
    fair_value_range: FairValueRange
    momentum: MomentumSnapshot


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

    topix_history = providers.market_data.get_benchmark_price_history(
        "TOPIX", history_start, now.date()
    )
    topix_bars = topix_history.bars if topix_history is not None else []
    sector_etf = config.momentum.sector_etf_map.get(financial.industry or "")
    sector_history = (
        providers.market_data.get_benchmark_price_history(sector_etf, history_start, now.date())
        if sector_etf
        else None
    )
    sector_bars = sector_history.bars if sector_history is not None else []

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
    dcf_price = compute_dcf_price(
        financial.operating_cashflow,
        financial.capital_expenditure,
        financial.shares_outstanding,
        config.valuation.dcf_method.discount_rate_pct,
        config.valuation.dcf_method.terminal_growth_rate_pct,
        config.valuation.dcf_method.projection_years,
    )
    fair_value_candidates = {
        "target_yield": target_price,
        "per": per_price,
        "pbr": pbr_price,
        "historical_range": range_price,
        "dcf": dcf_price,
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

    method_confidence = {
        "target_yield": ConfidenceLevel.HIGH,
        "per": ConfidenceLevel.MEDIUM,
        "pbr": ConfidenceLevel.MEDIUM,
        "historical_range": ConfidenceLevel.MEDIUM,
        "dcf": ConfidenceLevel.MEDIUM,  # 固定割引率のためHIGHにはしない(要求仕様8節)
    }
    method_exclusion_reason = {
        "target_yield": "予想配当が取得できないため算出不可",
        "per": "予想EPSまたは過去PER中央値が取得できないため算出不可",
        "pbr": "予想BPSまたは過去PBR中央値が取得できないため算出不可",
        "historical_range": "過去株価データが取得できないため算出不可",
        "dcf": "営業CF・設備投資・発行済株式数のいずれかが取得できない、"
        "またはFCFが負のため算出不可",
    }
    fair_value_method_results = [
        FairValueMethodResult(
            method=name,
            fair_value=value,
            confidence=method_confidence[name],
            exclusion_reason=None if value is not None else method_exclusion_reason[name],
        )
        for name, value in fair_value_candidates.items()
    ]
    fair_value_range = build_fair_value_range(
        fair_value_method_results,
        config.valuation.fair_value_methods.aggregation_method,
        config.valuation.fair_value_methods.method_weights,
        config.valuation.fair_value_usability,
    )

    data_sources = [snap.source, financial.source, dividend.source]
    if benefit is not None:
        data_sources.append(benefit.source)
    data_fetched_at = min(s.fetched_at for s in data_sources)

    keywords_found = detect_disclosure_risk_keywords(
        disclosures, config.sell.disclosure_risk_keywords
    )

    period_ends = [q.quarter_end for q in financial.recent_quarters]
    raw_operating_incomes = [q.operating_income for q in financial.recent_quarters]
    raw_operating_cashflows = [q.operating_cashflow for q in financial.recent_quarters]

    # 四半期粒度のデータは直近12ヶ月移動合計(TTM)に変換し、季節性(業種特有の
    # 繁閑差)による誤検知を防ぐ。年次粒度はそのまま(恒等変換)。
    adjusted_operating_incomes = to_seasonally_adjusted_series(raw_operating_incomes, period_ends)
    adjusted_operating_cashflows = to_seasonally_adjusted_series(
        raw_operating_cashflows, period_ends
    )

    quarterly_operating_incomes = [v for v in adjusted_operating_incomes if v is not None]
    quarterly_operating_cashflows = [v for v in adjusted_operating_cashflows if v is not None]
    severe_earnings_decline = has_severe_earnings_decline(quarterly_operating_incomes)
    cashflow_decomposition = providers.financial_data.get_cashflow_decomposition(stock_code)
    stock_type_classification = classify_stock_type(
        financial=financial,
        dividend_yield_pct=dividend_yield_pct,
        current_price=current_price,
        quarterly_operating_incomes=quarterly_operating_incomes,
        disclosures=disclosures,
        now=now,
        config=config.stock_classification,
        data_sources=data_sources,
    )
    momentum_snapshot = compute_momentum_snapshot(
        bars,
        current_price,
        now.date(),
        config.momentum,
        benchmark_bars=topix_bars or None,
        sector_bars=sector_bars or None,
    )

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
        cashflow_decomposition=cashflow_decomposition,
        stock_type_classification=stock_type_classification,
        fair_value_range=fair_value_range,
        momentum=momentum_snapshot,
    )
    return snapshot, None
