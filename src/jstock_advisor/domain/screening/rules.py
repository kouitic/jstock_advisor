"""一次スクリーニング(要求仕様8節)。

除外条件を一つでも満たせば買い候補から除外する。判定に使用した理由をすべて記録し、
除外未満だが留意すべき事項はwarningsとして残す。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.config.models import ScreeningRulesConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.interfaces.types import Disclosure, DividendInfo, FinancialSummary


@dataclass(frozen=True)
class ScreeningResult:
    passed: bool
    exclusion_reasons: list[str]
    warnings: list[str]


def detect_disclosure_risk_keywords(
    disclosures: list[Disclosure], keywords: list[str]
) -> list[str]:
    """適時開示のタイトル・要約からリスクキーワードを検出する(決定論的な文字列検索)。

    LLMによる高度な抽出は将来の拡張とし、MVPでは数値判定に影響する重大リスクの
    検知漏れを防ぐための機械的なキーワード一致のみを行う(要求仕様sell_rules.yaml参照)。
    """
    found: set[str] = set()
    for disclosure in disclosures:
        text = f"{disclosure.title} {disclosure.summary or ''}"
        for keyword in keywords:
            if keyword in text:
                found.add(keyword)
    return sorted(found)


def evaluate_screening(
    financial: FinancialSummary,
    dividend: DividendInfo | None,
    total_yield_pct: float,
    average_trading_value_yen: Decimal | None,
    disclosure_risk_keywords_found: list[str],
    data_fetched_at: dt.datetime,
    now: dt.datetime,
    business_calendar: BusinessCalendar,
    config: ScreeningRulesConfig,
) -> ScreeningResult:
    reasons: list[str] = []
    warnings: list[str] = []

    if config.universe.exclude_reit and financial.security_type == "REIT":
        reasons.append("REITは対象外です")
    if config.universe.exclude_etf and financial.security_type == "ETF":
        reasons.append("ETFは対象外です")

    if total_yield_pct < config.total_yield.min_total_yield_pct:
        reasons.append(
            f"総合利回り{total_yield_pct:.2f}%が基準{config.total_yield.min_total_yield_pct}%未満"
        )

    fh = config.financial_health
    if (
        financial.payout_ratio_pct is not None
        and financial.payout_ratio_pct > fh.max_payout_ratio_pct
    ):
        reasons.append(
            f"配当性向{financial.payout_ratio_pct:.1f}%が上限{fh.max_payout_ratio_pct}%超"
        )

    if (
        financial.equity_ratio_pct is not None
        and financial.equity_ratio_pct < fh.min_equity_ratio_pct
    ):
        reasons.append(
            f"自己資本比率{financial.equity_ratio_pct:.1f}%が下限{fh.min_equity_ratio_pct}%未満"
        )

    if (
        fh.require_positive_operating_cashflow
        and financial.operating_cashflow is not None
        and financial.operating_cashflow <= 0
    ):
        reasons.append("営業キャッシュフローがマイナス")

    if fh.exclude_negative_equity and financial.is_debt_excess:
        reasons.append("債務超過")

    if fh.exclude_deficit_companies and financial.is_deficit:
        reasons.append("赤字企業")

    ce = config.corporate_events
    if ce.exclude_going_concern_doubt and financial.is_going_concern_doubt:
        reasons.append("継続企業の前提に重大な疑義")

    if (
        ce.exclude_recent_dividend_cut_announced
        and dividend is not None
        and (dividend.is_dividend_cut_announced or dividend.is_dividend_omission_announced)
    ):
        reasons.append("直近で減配・無配転落の発表あり")

    if average_trading_value_yen is not None:
        min_value = config.universe.min_avg_trading_value_20d_yen
        if average_trading_value_yen < min_value:
            reasons.append(
                f"平均売買代金{average_trading_value_yen:,.0f}円が基準{min_value:,}円未満"
            )

    industry_rules = config.industry_specific_rules
    if financial.industry in industry_rules.target_industry_classification:
        action = industry_rules.financial_sector_action
        message = f"業種({financial.industry})は個別評価ルール未実装のため対象外"
        if action == "exclude_with_warning":
            reasons.append(message)
        elif action != "custom_rules":
            warnings.append(message)

    if disclosure_risk_keywords_found:
        message = f"開示情報にリスクキーワードを検出: {', '.join(disclosure_risk_keywords_found)}"
        if ce.scandal_or_delisting_risk_action == "exclude":
            reasons.append(message)
        else:
            warnings.append(message)

    data_age_days = business_calendar.business_days_between(data_fetched_at.date(), now.date())
    max_age = config.data_quality.max_data_age_business_days
    if data_age_days > max_age:
        reasons.append(f"データが{data_age_days}営業日前と古く、基準{max_age}営業日を超過")

    return ScreeningResult(passed=not reasons, exclusion_reasons=reasons, warnings=warnings)
