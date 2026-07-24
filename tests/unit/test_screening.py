import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.screening.rules import (
    detect_disclosure_risk_keywords,
    evaluate_screening,
)
from jstock_advisor.interfaces.types import Disclosure, DividendInfo, FinancialSummary

_NOW = dt.datetime(2026, 7, 24, 7, 0, tzinfo=dt.UTC)
_SOURCE = DataSourceReference(provider="test", fetched_at=_NOW)
_CONFIG = load_config()
_CALENDAR = BusinessCalendar.from_config(_CONFIG.holiday_calendar)


def _healthy_financial(**overrides: object) -> FinancialSummary:
    base = dict(
        stock_code="8136",
        fiscal_period_end=_NOW.date(),
        security_type="STOCK",
        industry="その他製品",
        equity_ratio_pct=60.0,
        payout_ratio_pct=45.0,
        operating_cashflow=Decimal("100"),
        is_going_concern_doubt=False,
        is_deficit=False,
        is_debt_excess=False,
        source=_SOURCE,
    )
    base.update(overrides)
    return FinancialSummary(**base)  # type: ignore[arg-type]


def _healthy_dividend() -> DividendInfo:
    return DividendInfo(
        stock_code="8136",
        fiscal_year="2026",
        forecast_annual_dividend_per_share=Decimal("70"),
        is_dividend_cut_announced=False,
        is_dividend_omission_announced=False,
        source=_SOURCE,
    )


def test_healthy_stock_passes_screening() -> None:
    result = evaluate_screening(
        financial=_healthy_financial(),
        dividend=_healthy_dividend(),
        total_yield_pct=4.0,
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert result.passed
    assert result.exclusion_reasons == []


def test_low_total_yield_excluded() -> None:
    result = evaluate_screening(
        financial=_healthy_financial(),
        dividend=_healthy_dividend(),
        total_yield_pct=2.0,
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert not result.passed
    assert any("総合利回り" in r for r in result.exclusion_reasons)


def test_deficit_company_excluded() -> None:
    result = evaluate_screening(
        financial=_healthy_financial(is_deficit=True),
        dividend=_healthy_dividend(),
        total_yield_pct=4.0,
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert not result.passed
    assert any("赤字企業" in r for r in result.exclusion_reasons)


def test_debt_excess_excluded() -> None:
    result = evaluate_screening(
        financial=_healthy_financial(is_debt_excess=True),
        dividend=_healthy_dividend(),
        total_yield_pct=4.0,
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert not result.passed
    assert any("債務超過" in r for r in result.exclusion_reasons)


def test_dividend_cut_excluded() -> None:
    dividend = DividendInfo(
        stock_code="8136",
        fiscal_year="2026",
        is_dividend_cut_announced=True,
        source=_SOURCE,
    )
    result = evaluate_screening(
        financial=_healthy_financial(),
        dividend=dividend,
        total_yield_pct=4.0,
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert not result.passed
    assert any("減配" in r for r in result.exclusion_reasons)


def test_low_liquidity_excluded() -> None:
    result = evaluate_screening(
        financial=_healthy_financial(),
        dividend=_healthy_dividend(),
        total_yield_pct=4.0,
        average_trading_value_yen=Decimal("1_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert not result.passed
    assert any("平均売買代金" in r for r in result.exclusion_reasons)


def test_financial_sector_excluded_with_warning_config() -> None:
    result = evaluate_screening(
        financial=_healthy_financial(industry="銀行業", equity_ratio_pct=6.0),
        dividend=_healthy_dividend(),
        total_yield_pct=4.0,
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert not result.passed
    assert any("銀行業" in r for r in result.exclusion_reasons)


def test_stale_data_excluded() -> None:
    old_fetch = _NOW - dt.timedelta(days=10)
    result = evaluate_screening(
        financial=_healthy_financial(),
        dividend=_healthy_dividend(),
        total_yield_pct=4.0,
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=old_fetch,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert not result.passed
    assert any("データが" in r for r in result.exclusion_reasons)


def test_reit_excluded() -> None:
    result = evaluate_screening(
        financial=_healthy_financial(security_type="REIT"),
        dividend=_healthy_dividend(),
        total_yield_pct=5.0,
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert not result.passed
    assert any("REIT" in r for r in result.exclusion_reasons)


def test_detect_disclosure_risk_keywords() -> None:
    disclosures = [
        Disclosure(
            stock_code="8136",
            published_at=_NOW,
            title="第三者委員会設置に関するお知らせ",
            source=_SOURCE,
        )
    ]
    found = detect_disclosure_risk_keywords(disclosures, ["第三者委員会", "監理銘柄"])
    assert found == ["第三者委員会"]


def test_scandal_keyword_excludes_when_configured() -> None:
    result = evaluate_screening(
        financial=_healthy_financial(),
        dividend=_healthy_dividend(),
        total_yield_pct=4.0,
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=["第三者委員会"],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert not result.passed  # 初期設定はexclude
