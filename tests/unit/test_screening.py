import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import IndustrySpecificRules
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import FinancialIndustryCategory
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


# --- Issue #29: 金融業除外(classify_industryベース) --------------------------
# 以前はconfigの日本語TSE33ラベルとyfinance英語industry値を直接比較しており
# 一度も一致しなかった(除外が機能していなかった)。実在銘柄の実測値
# (8306/8604/8766相当)を固定fixtureとして再現する。


def _evaluate_industry(
    sector: str | None,
    industry: str | None,
    config: object = None,
):
    return evaluate_screening(
        financial=_healthy_financial(sector=sector, industry=industry),
        dividend=_healthy_dividend(),
        average_trading_value_yen=Decimal("50_000_000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=_NOW,
        now=_NOW,
        business_calendar=_CALENDAR,
        config=config or _CONFIG.screening,
    )


def _screening_config_with_industries(
    categories: list[FinancialIndustryCategory],
    financial_sector_action: str = "exclude_with_warning",
):
    rules = _CONFIG.screening.industry_specific_rules.model_copy(
        update={
            "target_industry_classification": categories,
            "financial_sector_action": financial_sector_action,
        }
    )
    return _CONFIG.screening.model_copy(update={"industry_specific_rules": rules})


def test_bank_excluded_like_8306() -> None:
    result = _evaluate_industry("Financial Services", "Banks - Diversified")
    assert not result.passed
    assert any("BANKING" in r for r in result.exclusion_reasons)


def test_securities_excluded_like_8604() -> None:
    result = _evaluate_industry("Financial Services", "Capital Markets")
    assert not result.passed
    assert any("SECURITIES" in r for r in result.exclusion_reasons)


def test_insurance_excluded_like_8766() -> None:
    result = _evaluate_industry("Financial Services", "Insurance - Property & Casualty")
    assert not result.passed
    assert any("INSURANCE" in r for r in result.exclusion_reasons)


def test_other_financial_lease_passes_by_default() -> None:
    """OTHER_FINANCIAL(リース・Credit Services等)は既定では除外しない。"""
    result = _evaluate_industry("Financial Services", "Credit Services")
    assert result.passed
    assert result.exclusion_reasons == []


def test_other_financial_excluded_when_added_to_config() -> None:
    config = _screening_config_with_industries(
        [
            FinancialIndustryCategory.BANKING,
            FinancialIndustryCategory.SECURITIES,
            FinancialIndustryCategory.INSURANCE,
            FinancialIndustryCategory.OTHER_FINANCIAL,
        ]
    )
    result = _evaluate_industry("Financial Services", "Credit Services", config=config)
    assert not result.passed
    assert any("OTHER_FINANCIAL" in r for r in result.exclusion_reasons)


def test_japanese_financial_input_excluded() -> None:
    """日本語入力はclassify_industryの既存仕様どおり判定される(将来データソース対応)。"""
    result = _evaluate_industry("金融", "銀行業")
    assert not result.passed
    assert any("BANKING" in r for r in result.exclusion_reasons)


def test_japanese_industry_without_sector_is_unknown_and_passes() -> None:
    """sector欠損はclassify_industryの既存仕様どおりUNKNOWN。金融業と推測して除外しない。"""
    result = _evaluate_industry(None, "銀行業")
    assert result.passed


def test_general_corporate_sector_passes() -> None:
    result = _evaluate_industry("Technology", "Consumer Electronics")
    assert result.passed


def test_unknown_sector_passes_without_crash() -> None:
    for sector, industry in ((None, None), ("", ""), ("Unknown Sector X", "Something")):
        result = _evaluate_industry(sector, industry)
        assert result.passed


def test_financial_sector_action_non_exclude_value_warns_only() -> None:
    config = _screening_config_with_industries(
        [FinancialIndustryCategory.BANKING], financial_sector_action="warn"
    )
    result = _evaluate_industry("Financial Services", "Banks - Diversified", config=config)
    assert result.passed
    assert any("BANKING" in w for w in result.warnings)


def test_invalid_target_industry_classification_fails_fast() -> None:
    """不正なsubcategory名はconfigロード時にpydanticのenum検証でfail-fastする。"""
    with pytest.raises(ValidationError):
        IndustrySpecificRules(
            financial_sector_action="exclude_with_warning",
            target_industry_classification=["BANKING", "TSE_BANKS"],  # type: ignore[list-item]
        )


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

