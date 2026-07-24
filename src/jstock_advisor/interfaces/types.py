"""Providerが返すデータの型定義。

すべて要求仕様12節・13節の原則("取得できない場合は推測で補完しない")に従い、
値が取得できない場合は Provider メソッドが None または空リストを返す。
どのフィールドも創作・推測値を許容しない(実データが無ければNoneのままとする)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import BenefitUtilityCategory


class PriceBar(ImmutableSnapshot):
    date: dt.date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class PriceSnapshot(ImmutableSnapshot):
    stock_code: str
    as_of_date: dt.date
    close_price: Decimal
    high_price: Decimal | None = None
    low_price: Decimal | None = None
    volume: int | None = None
    source: DataSourceReference


class PriceHistory(ImmutableSnapshot):
    symbol: str  # 銘柄コード、またはベンチマーク指数シンボル(例: "TOPIX")
    bars: list[PriceBar]
    source: DataSourceReference


class QuarterlyFinancials(ImmutableSnapshot):
    stock_code: str
    quarter_end: dt.date
    operating_income: Decimal | None = None
    ordinary_income: Decimal | None = None
    operating_cashflow: Decimal | None = None
    source: DataSourceReference


class FinancialSummary(ImmutableSnapshot):
    stock_code: str
    stock_name: str | None = None
    fiscal_period_end: dt.date
    security_type: str = "STOCK"  # "STOCK" / "REIT" / "ETF"
    market_segment: str | None = None
    industry: str | None = None
    equity_ratio_pct: float | None = None
    payout_ratio_pct: float | None = None
    operating_cashflow: Decimal | None = None
    net_income: Decimal | None = None
    operating_income: Decimal | None = None
    ordinary_income: Decimal | None = None
    interest_bearing_debt: Decimal | None = None
    forecast_eps: Decimal | None = None
    forecast_bps: Decimal | None = None
    is_going_concern_doubt: bool = False
    is_deficit: bool = False
    is_debt_excess: bool = False
    recent_quarters: list[QuarterlyFinancials] = []
    source: DataSourceReference


class HistoricalValuation(ImmutableSnapshot):
    """過去のPER/PBR算出に必要な時系列データ(1時点分)。"""

    stock_code: str
    date: dt.date
    eps: Decimal | None = None
    bps: Decimal | None = None
    price: Decimal | None = None
    per: Decimal | None = None
    pbr: Decimal | None = None
    source: DataSourceReference


class DividendInfo(ImmutableSnapshot):
    stock_code: str
    fiscal_year: str
    forecast_annual_dividend_per_share: Decimal | None = None
    actual_annual_dividend_per_share: Decimal | None = None
    previous_fiscal_year_dividend_per_share: Decimal | None = None
    is_dividend_cut_announced: bool = False
    is_dividend_omission_announced: bool = False
    is_progressive_or_doe_policy: bool = False
    dividend_policy_note: str | None = None
    dividend_record_dates: list[dt.date] = []
    consecutive_dividend_increase_years: int | None = None
    source: DataSourceReference


class BenefitDetail(ImmutableSnapshot):
    category: BenefitUtilityCategory
    description: str
    estimated_value: Decimal | None = None
    min_shares_for_tier: int
    long_term_holding_condition_months: int | None = None


class ShareholderBenefit(ImmutableSnapshot):
    stock_code: str
    min_shares_required: int
    benefits: list[BenefitDetail]
    frequency_per_year: int
    benefit_record_dates: list[dt.date] = []
    is_abolished: bool = False
    is_major_downgrade: bool = False
    change_note: str | None = None
    source: DataSourceReference


class CorporateActionEvent(ImmutableSnapshot):
    stock_code: str
    event_type: str  # 例: "SPLIT", "REVERSE_SPLIT", "BUYBACK"
    announced_date: dt.date
    effective_date: dt.date | None = None
    ratio: Decimal | None = None  # 例: 2:1分割なら2.0
    detail: str | None = None
    source: DataSourceReference


class Disclosure(ImmutableSnapshot):
    stock_code: str
    published_at: dt.datetime
    title: str
    category: str | None = None  # 例: "決算短信", "業績予想の修正", "配当予想の修正"
    summary: str | None = None
    url: str | None = None
    source: DataSourceReference


class NewsItem(ImmutableSnapshot):
    stock_code: str | None = None
    published_at: dt.datetime
    title: str
    summary: str | None = None
    url: str | None = None
    source: DataSourceReference
