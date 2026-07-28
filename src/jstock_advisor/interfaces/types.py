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
from jstock_advisor.domain.entities.enums import (
    BenefitUtilityCategory,
    CorporateActionType,
    DividendComparisonOutcome,
    RecordDateUnknownReason,
)


class BankRegulatoryMetrics(ImmutableSnapshot):
    """銀行専用の規制資本・健全性指標(2026-07仕様§2)。

    現時点でこれらを安定的に取得できるProviderが存在しないため、全項目が
    Noneのままとなる恒久的な制約。データが取得可能になるまで、この構造体を
    根拠にSELL/URGENT_REVIEWを出してはならない(呼び出し側の責務)。
    """

    cet1_ratio_pct: float | None = None  # 普通株式等Tier1比率
    total_capital_ratio_pct: float | None = None  # 総自己資本比率
    tier1_ratio_pct: float | None = None
    leverage_ratio_pct: float | None = None
    non_performing_loan_ratio_pct: float | None = None  # 不良債権比率
    credit_cost_ratio_pct: float | None = None  # 与信費用率
    allowance_coverage_ratio_pct: float | None = None  # 貸倒引当率
    liquidity_coverage_ratio_pct: float | None = None  # 流動性カバレッジ比率(LCR)
    tlac_ratio_pct: float | None = None
    net_interest_margin_pct: float | None = None


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
    sector: str | None = None
    equity_ratio_pct: float | None = None
    payout_ratio_pct: float | None = None
    operating_cashflow: Decimal | None = None
    capital_expenditure: Decimal | None = None  # 簡易DCF法のFCF算出用(要求仕様8節)
    net_income: Decimal | None = None
    operating_income: Decimal | None = None
    ordinary_income: Decimal | None = None
    interest_bearing_debt: Decimal | None = None
    forecast_eps: Decimal | None = None
    forecast_bps: Decimal | None = None
    shares_outstanding: Decimal | None = None  # 簡易DCF法のFCF按分用(要求仕様8節)
    is_going_concern_doubt: bool = False
    is_deficit: bool = False
    is_debt_excess: bool = False
    recent_quarters: list[QuarterlyFinancials] = []
    source: DataSourceReference

    # --- 業種別分類+金融業向け財務健全性ルール(2026-07仕様§2)で追加 ---
    bank_regulatory_metrics: BankRegulatoryMetrics | None = None


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

    # --- 減配判定の再設計(要求仕様5節・6節)で追加 ---
    comparison_source_fiscal_year: str | None = None
    comparison_target_fiscal_year: str | None = None
    dividend_comparison_outcome: DividendComparisonOutcome | None = None
    dividend_cut_pct: float | None = None
    has_dividend_floor_policy: bool | None = None
    is_one_time_factor: bool | None = None
    dividend_record_date: dt.date | None = None
    dividend_ex_date: dt.date | None = None
    dividend_record_date_unknown_reason: RecordDateUnknownReason | None = None

    # --- 配当の普通/特別分離+official/inferred区別(2026-07仕様§10・§11) ---
    # yfinance/EDINETいずれも配当の内訳(普通/特別/記念/臨時)を提供しないため、
    # 現時点ではdividend_breakdown_confirmed=False・各内訳フィールドはNoneが常態となる
    # (恒久的な制約。データソースが増えない限り解消しない)。
    ordinary_dividend_per_share: Decimal | None = None
    special_dividend_per_share: Decimal | None = None
    commemorative_dividend_per_share: Decimal | None = None
    extraordinary_dividend_per_share: Decimal | None = None
    total_dividend_per_share: Decimal | None = None
    dividend_breakdown_confirmed: bool = False
    # 会社が公式に減配・無配転落を発表したことが一次情報で確認できた場合のみTrue。
    # yfinanceの数値比較のみからは絶対に真にしない(§11)。
    official_dividend_cut_announced: bool = False
    # yfinance等の年間配当合計の単純比較から推測される減少シグナル(弱い根拠)。
    # SELL/URGENT_REVIEWの直接的な根拠にはできない(§12)。
    inferred_dividend_decrease: bool = False
    total_dividend_decrease_detected: bool = False
    special_dividend_expired: bool | None = None


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

    # --- 権利確定情報の改善(要求仕様16節)で追加 ---
    benefit_ex_date: dt.date | None = None
    long_term_holding_requirement: str | None = None
    benefit_record_date_unknown_reason: RecordDateUnknownReason | None = None


class CorporateActionEvent(ImmutableSnapshot):
    stock_code: str
    event_type: CorporateActionType
    announced_date: dt.date
    effective_date: dt.date | None = None
    ratio: Decimal | None = None  # 例: 2:1分割なら2.0
    detail: str | None = None
    source: DataSourceReference


class CashflowDecomposition(ImmutableSnapshot):
    """営業キャッシュフローの要因分解(要求仕様4節)。

    多くの銘柄でyfinanceから全項目を安定取得できないため、取得できない項目は
    Noneのままとする(推測で補完しない)。
    """

    stock_code: str
    period_end: dt.date
    pretax_income: Decimal | None = None
    depreciation_amortization: Decimal | None = None
    receivables_change: Decimal | None = None
    inventory_change: Decimal | None = None
    payables_change: Decimal | None = None
    tax_paid: Decimal | None = None
    one_time_items: Decimal | None = None
    ma_related_items: Decimal | None = None
    other_working_capital: Decimal | None = None
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
