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
    # 直近で取得できた財務諸表(年次)の対象期間末日。データ鮮度の判定に使う
    # (2026-07仕様レビュー対応: 以前はデータ取得日時そのものが入っており、鮮度判定が
    # 常に「最新」と誤判定される不具合があった。必ず実際の開示期間末日を設定する)。
    # 決算反映確認(四半期単位)にはrecent_quartersの方を優先する。年次決算期末を
    # 取得できない場合はNone(データ取得日時を代替値として使用しない。デプロイ前対応)。
    fiscal_period_end: dt.date | None = None
    # 企業の正式な決算期末月(例: 3月決算なら3)。配当・優待基準日の周期推定にのみ使う。
    # 直近開示期間末(fiscal_period_end、四半期の場合がある)と混同しない
    # (2026-07仕様レビュー対応: 以前はfiscal_period_end=直近四半期末を決算期末として
    # 誤って使い、3月決算企業が「1月末・7月末」等と誤表示される不具合があった)。
    fiscal_year_end_month: int | None = None
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

    # --- 無配転落のofficial/inferred分離(2026-07仕様レビュー対応) ---
    official_dividend_omission_announced: bool = False
    inferred_dividend_omission: bool = False


class BenefitDetail(ImmutableSnapshot):
    category: BenefitUtilityCategory
    description: str
    estimated_value: Decimal | None = None
    min_shares_for_tier: int
    long_term_holding_condition_months: int | None = None

    # --- 保有株数×保有期間のマトリクス型優待対応(2026-07仕様追加) ---
    # 同一tier_groupを持つ明細は「保有株数・保有期間の両方を満たす中で最も条件の
    # 良い1件のみ」を採用する(段階制優待の重複加算を防ぐ)。Noneの場合は従来通り
    # 各明細を独立した優待として個別に加算する(複数の優待が同時に併存する銘柄向け)。
    tier_group: str | None = None

    # long_term_holding_condition_months(下限)のみでは「◯年以上◯年未満」のように
    # 上限が定められた優待(NTTのdポイント進呈等)を正しく表現できず、対象期間を
    # 過ぎても該当し続けてしまうバグの原因になっていた。Noneの場合は従来通り上限なし。
    long_term_holding_condition_max_months: int | None = None


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

    # --- 権利確定日の周期管理(2026-07仕様追加) ---
    # 「毎年3月末・9月末」のような周期を月単位で保持し、次回の権利確定日を
    # カレンダー上の実際の月末日から自動算出する(閏年の2月末等も正しく扱う)。
    benefit_record_date_recurrence_months: list[int] = []
    next_benefit_record_date: dt.date | None = None


class StockNameOverride(ImmutableSnapshot):
    """銘柄名の手動オーバーライド(2026-07 BUYパイプライン第2次修正。要求仕様19節)。

    EDINET提出書類のfilerNameが取得できない、または表記の見直しが必要な
    銘柄のみ、運用者が手動で正式な日本語社名を登録する(株主優待・企業行動と
    同じ「自動取得できない情報は手動登録する」という設計方針)。
    """

    stock_code: str
    stock_name: str


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
