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
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.classification.stock_type import classify_stock_type
from jstock_advisor.domain.entities.classification import StockTypeClassification
from jstock_advisor.domain.entities.common import (
    BenefitUtilityCoefficients,
    DataSourceReference,
)
from jstock_advisor.domain.entities.enums import ConfidenceLevel, EarningsDateStatus, ValuationBasis
from jstock_advisor.domain.entities.historical_valuation import HistoricalValuationResult
from jstock_advisor.domain.entities.momentum import MomentumSnapshot
from jstock_advisor.domain.entities.timing_score import TimingScoreResult
from jstock_advisor.domain.entities.valuation import FairValueMethodResult, FairValueRange
from jstock_advisor.domain.financial_series import (
    FinancialPeriodValue,
    build_financial_period_series,
    to_seasonally_adjusted_series,
)
from jstock_advisor.domain.jst import evaluation_date_jst, require_timezone_aware
from jstock_advisor.domain.screening.rules import (
    detect_disclosure_risk_keywords,
    detect_material_event_keywords,
)
from jstock_advisor.domain.signals.buy_signal import has_severe_earnings_decline
from jstock_advisor.domain.signals.historical_valuation import evaluate_historical_valuation
from jstock_advisor.domain.signals.momentum import compute_momentum_snapshot
from jstock_advisor.domain.signals.timing_score import evaluate_timing_score
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
    # next_earnings_dateは検証済みの値(過去日・取得不能時はNone)。生値は
    # earnings_date_rawで別途保持する(コードレビュー対応: 明治HD事例、
    # データ提供元の更新遅延により過去日がそのまま返ってくることがあるため、
    # buy/sell/profit_takingの3消費者すべてで一元的に検証する)。
    next_earnings_date: dt.date | None
    earnings_date_status: EarningsDateStatus
    earnings_date_raw: dt.date | None
    # 次回決算までの営業日数(JST暦日基準、決算日修正デプロイ前対応で新設)。
    # buy/sell/profit_takingの3消費者が個別に再計算していたのをここへ一元化する
    # (計算元を1か所にすることで、UTC/JST境界の誤判定を1箇所の修正で解消できる)。
    business_days_to_earnings: int | None
    dividend_yield_pct: float | None
    benefit_yield_pct: float | None
    annual_benefit_value: Decimal | None
    total_yield_pct: float
    fair_value: Decimal | None
    fair_value_methods_used_count: int
    data_sources: list[DataSourceReference]
    data_fetched_at: dt.datetime
    quarterly_operating_incomes: list[Decimal]
    quarterly_operating_cashflows: list[Decimal]
    # --- 財務期間の構造化(2026-07仕様レビュー対応)。sell_signal専用に、
    # period_type(QUARTER/YTD/TTM/ANNUAL)を明示したうえで継続悪化判定を行う ---
    quarterly_operating_income_periods: list[FinancialPeriodValue]
    quarterly_operating_cashflow_periods: list[FinancialPeriodValue]
    severe_earnings_decline: bool
    disclosure_risk_keywords_found: list[str]
    material_event_keywords_found: list[str]
    cashflow_decomposition: CashflowDecomposition | None
    stock_type_classification: StockTypeClassification
    fair_value_range: FairValueRange
    momentum: MomentumSnapshot
    # --- 判定精度向上機能Phase B: Historical Valuation Score(2026-08追加) ---
    # 銘柄自身の過去PER/PBR水準に対する現在値のランクベース評価結果
    # (score/confidence/coverage/内訳を含む構造化Result)。DecisionSnapshot
    # 記録専用のShadow計測であり、BUY候補判定・保有判断スコア・旧売却判定・
    # ProfitTaking判定・LINE通知など既存の判定ロジックからは一切参照されない。
    historical_valuation: HistoricalValuationResult
    # --- 判定精度向上機能Phase B第二弾: Timing Score(2026-08追加) ---
    # momentum(上記)を基にしたモメンタムベースの技術的タイミング評価結果。
    # 同じくDecisionSnapshot記録専用のShadow計測であり、既存の判定ロジックには
    # 一切影響しない。
    timing: TimingScoreResult


def build_stock_snapshot(
    providers: ProviderBundle,
    stock_code: str,
    now: dt.datetime,
    config: AppConfig,
    business_calendar: BusinessCalendar | None = None,
) -> tuple[StockSnapshot | None, str | None]:
    # 決算日修正デプロイ前対応: nowはtimezone-aware必須(naiveを暗黙にUTC扱いしない)。
    require_timezone_aware(now)
    calendar = business_calendar or BusinessCalendar.from_config(config.holiday_calendar)
    # 決算日関連の暦日比較は必ずJST基準で行う(UTC-awareなnowに.date()を直接呼ぶと、
    # JST 00:00-09:00の間は前日のUTC日付になり誤判定する)。
    evaluation_date = evaluation_date_jst(now)

    snap = providers.market_data.get_latest_price(stock_code)
    if snap is None:
        return None, "株価データを取得できません"

    financial = providers.financial_data.get_financial_summary(stock_code)
    if financial is None:
        return None, "財務データを取得できません"

    dividend = providers.dividend_data.get_dividend_info(stock_code)
    if dividend is None:
        return None, (
            "配当データを取得できません"
            "(データ提供元(yfinance)から取得できなかったか、yfinanceとEDINETの配当額が"
            "株式分割等で説明できない水準まで乖離しており自動判定できないため除外しています。"
            "後者の場合、詳細はCloudWatch Logsの該当銘柄のwarningログをご確認ください)"
        )

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
    # 決算日の妥当性検証(コードレビュー対応: 明治HD事例)。データ提供元(yfinance等)の
    # 更新遅延により、評価日より過去の日付が「次回決算予定日」として返ってくることが
    # ある。過去日をそのまま次回決算日として使わず、検証済みの値のみをnext_earnings_date
    # へ格納する(buy/sell/profit_takingの3消費者すべてがここで一元的に検証される)。
    earnings_date_raw = providers.disclosure.get_next_earnings_date(stock_code)
    if earnings_date_raw is None:
        earnings_date_status = EarningsDateStatus.UNAVAILABLE
        next_earnings_date = None
    elif earnings_date_raw < evaluation_date:
        earnings_date_status = EarningsDateStatus.STALE_PAST_DATE
        next_earnings_date = None
    else:
        earnings_date_status = EarningsDateStatus.CONFIRMED
        next_earnings_date = earnings_date_raw
    # 次回決算までの営業日数(JST暦日基準)。ここで1回だけ計算し、buy/sell/
    # profit_takingは全てsnapshot.business_days_to_earningsを読むだけにする
    # (デプロイ前対応: 計算元の分散によるUTC/JST境界の誤判定を防止)。
    business_days_to_earnings = (
        calendar.business_days_between(evaluation_date, next_earnings_date)
        if next_earnings_date is not None
        else None
    )

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
    material_event_keywords_found = detect_material_event_keywords(disclosures)

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
    quarterly_operating_income_periods = build_financial_period_series(
        raw_operating_incomes, period_ends, source=financial.source.provider
    )
    quarterly_operating_cashflow_periods = build_financial_period_series(
        raw_operating_cashflows, period_ends, source=financial.source.provider
    )
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

    # 判定精度向上機能Phase B: Historical Valuation Score(Shadow計測)。
    # buy_signal_service.py等が使う既存のcurrent_per/current_pbr計算式
    # (current_price / forecast_eps・forecast_bps)とは独立に、ここでのみ
    # 再計算する(既存のBUYスクリーニング・銘柄分類ロジックには一切触れない)。
    # コードレビュー対応(basis整合性): get_historical_valuation()が返す過去PER
    # 系列はTRAILING(実績)basisのため、現在PERも同一basisのtrailing_eps
    # (forecast_eps=forwardEpsとは別物)から算出する。PBRはforecast_bpsの実体が
    # 既にtrailing bookValueであるため、そのままTRAILING basisとして扱う。
    current_per = (
        current_price / financial.trailing_eps
        if financial.trailing_eps is not None and financial.trailing_eps > 0
        else None
    )
    current_per_basis = (
        ValuationBasis.TRAILING if current_per is not None else ValuationBasis.UNKNOWN
    )
    current_pbr = (
        current_price / financial.forecast_bps
        if financial.forecast_bps is not None and financial.forecast_bps > 0
        else None
    )
    current_pbr_basis = (
        ValuationBasis.TRAILING if current_pbr is not None else ValuationBasis.UNKNOWN
    )
    historical_valuation = evaluate_historical_valuation(
        historical_valuations,
        stock_code,
        current_per,
        current_per_basis,
        current_pbr,
        current_pbr_basis,
        now,
        config.historical_valuation,
    )

    # 判定精度向上機能Phase B第二弾: Timing Score(Shadow計測)。既に計算済みの
    # momentum_snapshotを基に算出する派生値であり、既存のBUY/保有/売却/
    # ProfitTaking判定・LINE通知には一切影響しない。
    timing = evaluate_timing_score(momentum_snapshot, now, config.timing_score)

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
        earnings_date_status=earnings_date_status,
        earnings_date_raw=earnings_date_raw,
        business_days_to_earnings=business_days_to_earnings,
        dividend_yield_pct=dividend_yield_pct,
        benefit_yield_pct=benefit_yield_pct,
        annual_benefit_value=annual_benefit_value,
        total_yield_pct=total_yield_pct,
        fair_value=fair_value,
        fair_value_methods_used_count=fair_value_methods_used_count,
        data_sources=data_sources,
        data_fetched_at=data_fetched_at,
        quarterly_operating_incomes=quarterly_operating_incomes,
        quarterly_operating_cashflows=quarterly_operating_cashflows,
        quarterly_operating_income_periods=quarterly_operating_income_periods,
        quarterly_operating_cashflow_periods=quarterly_operating_cashflow_periods,
        severe_earnings_decline=severe_earnings_decline,
        disclosure_risk_keywords_found=keywords_found,
        material_event_keywords_found=material_event_keywords_found,
        cashflow_decomposition=cashflow_decomposition,
        stock_type_classification=stock_type_classification,
        fair_value_range=fair_value_range,
        momentum=momentum_snapshot,
        historical_valuation=historical_valuation,
        timing=timing,
    )
    return snapshot, None
