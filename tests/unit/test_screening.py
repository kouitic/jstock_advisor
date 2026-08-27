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
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert result.passed
    assert result.exclusion_reasons == []


def test_deficit_company_not_excluded_but_warned() -> None:
    """BUY候補裾野拡大機能(2026-08): 単年度赤字は全銘柄共通ハード除外から
    warningsへ格下げされた(GROWTH/VALUE等の他タイプでは評価継続するため)。"""
    result = evaluate_screening(
        financial=_healthy_financial(is_deficit=True),
        dividend=_healthy_dividend(),
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert result.passed
    assert result.exclusion_reasons == []
    assert any("赤字" in w for w in result.warnings)


def test_debt_excess_excluded() -> None:
    """債務超過は継続企業疑義と並ぶ重大リスクとして、全タイプ共通ハード除外を維持する。"""
    result = evaluate_screening(
        financial=_healthy_financial(is_debt_excess=True),
        dividend=_healthy_dividend(),
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert not result.passed
    assert any("債務超過" in r for r in result.exclusion_reasons)


def test_dividend_cut_not_excluded_but_warned() -> None:
    """BUY候補裾野拡大機能(2026-08): 直近減配発表は全銘柄共通ハード除外から
    warningsへ格下げされた(HIGH_DIVIDEND/DIVIDEND_GROWTH分類条件側で個別に判定する)。"""
    dividend = DividendInfo(
        stock_code="8136",
        fiscal_year="2026",
        is_dividend_cut_announced=True,
        source=_SOURCE,
    )
    result = evaluate_screening(
        financial=_healthy_financial(),
        dividend=dividend,
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert result.passed
    assert result.exclusion_reasons == []
    assert any("減配" in w for w in result.warnings)


def test_low_liquidity_excluded() -> None:
    result = evaluate_screening(
        financial=_healthy_financial(),
        dividend=_healthy_dividend(),
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
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=["第三者委員会"],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert not result.passed  # 初期設定はexclude


def test_high_payout_ratio_not_excluded_but_warned() -> None:
    """BUY候補裾野拡大機能(2026-08): 配当性向上限もwarningsへ格下げされた
    (screening.financial_health.max_payout_ratio_ptはcompute_score()が
    引き続きスコアリング係数として参照するため、値・フィールド名は変更しない)。"""
    result = evaluate_screening(
        financial=_healthy_financial(payout_ratio_pct=90.0),
        dividend=_healthy_dividend(),
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert result.passed
    assert any("配当性向" in w for w in result.warnings)


def test_low_equity_ratio_not_excluded_but_warned() -> None:
    result = evaluate_screening(
        financial=_healthy_financial(equity_ratio_pct=5.0),
        dividend=_healthy_dividend(),
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert result.passed
    assert any("自己資本比率" in w for w in result.warnings)


def test_negative_operating_cashflow_not_excluded_but_warned() -> None:
    result = evaluate_screening(
        financial=_healthy_financial(operating_cashflow=Decimal("-10")),
        dividend=_healthy_dividend(),
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert result.passed
    assert any("キャッシュフロー" in w for w in result.warnings)


def test_going_concern_doubt_still_excluded() -> None:
    """継続企業の前提に重大な疑義は全タイプ共通ハード除外を維持する(変更なし)。"""
    result = evaluate_screening(
        financial=_healthy_financial(is_going_concern_doubt=True),
        dividend=_healthy_dividend(),
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert not result.passed
    assert any("継続企業" in r for r in result.exclusion_reasons)


def test_issue23_stale_boundary_uses_jst_business_dates() -> None:
    """Issue #23: データ鮮度のJPX営業日計算は両端ともJST暦日で行う。
    fetched=2026-07-06(月)、now=2026-07-09T23:30Z(UTC木曜/JST金曜08:30)の場合、
    JST基準ではmax_data_age_business_days(3)を超過する4営業日となりstale除外
    される(修正前はUTC暦日で3営業日と数え、除外を免れていた)。"""
    fetched = dt.datetime(2026, 7, 6, 7, 0, tzinfo=dt.UTC)  # 月曜(UTC=JST同日)
    now = dt.datetime(2026, 7, 9, 23, 30, tzinfo=dt.UTC)  # UTC木曜 / JST金曜08:30
    result = evaluate_screening(
        financial=_healthy_financial(),
        dividend=_healthy_dividend(),
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=fetched,
        now=now,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert not result.passed
    assert any("営業日" in r for r in result.exclusion_reasons)


def test_issue23_stale_boundary_not_excluded_at_exact_max_age_jst() -> None:
    """Issue #23の対照ケース: JST基準でちょうどmax_data_age_business_days(3)
    営業日ならstale除外しない(境界の内側)。"""
    fetched = dt.datetime(2026, 7, 6, 7, 0, tzinfo=dt.UTC)  # 月曜
    now = dt.datetime(2026, 7, 8, 23, 30, tzinfo=dt.UTC)  # UTC水曜 / JST木曜08:30 -> 3営業日
    result = evaluate_screening(
        financial=_healthy_financial(),
        dividend=_healthy_dividend(),
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=fetched,
        now=now,
        business_calendar=_CALENDAR,
        config=_CONFIG.screening,
    )
    assert result.passed

