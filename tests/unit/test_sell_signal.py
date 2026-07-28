import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.signals.sell_signal import (
    SellRuleTriggerInputs,
    build_sell_rule_inputs_from_data,
    classify_disclosure_risk_keywords,
    detect_continuous_decline,
    detect_financial_health_severe_deterioration,
    evaluate_sell_signal,
)
from jstock_advisor.interfaces.types import CashflowDecomposition, DividendInfo, FinancialSummary

_NOW = dt.datetime(2026, 7, 24, 7, 0, tzinfo=dt.UTC)
_SOURCE = DataSourceReference(provider="test", fetched_at=_NOW)
_CONFIG = load_config()


def test_no_triggers_is_hold() -> None:
    result = evaluate_sell_signal(SellRuleTriggerInputs(), Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.HOLD
    assert result.stop_review_price is None


def test_price_decline_alone_does_not_trigger_sell() -> None:
    # 株価が半値になっても、個別ルールが一つも該当しなければHOLD
    result = evaluate_sell_signal(SellRuleTriggerInputs(), Decimal("500"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.HOLD


def test_dividend_cut_alone_triggers_sell() -> None:
    result = evaluate_sell_signal(
        SellRuleTriggerInputs(dividend_cut=True), Decimal("1000"), _CONFIG.sell
    )
    assert result.recommendation_type == RecommendationType.SELL
    assert result.stop_review_price is not None
    assert result.stop_review_price.price == Decimal("1000")


def test_dividend_omission_is_critical_and_triggers_urgent_review() -> None:
    result = evaluate_sell_signal(
        SellRuleTriggerInputs(dividend_omission=True), Decimal("1000"), _CONFIG.sell
    )
    assert result.recommendation_type == RecommendationType.URGENT_REVIEW


def test_two_major_triggers_escalate_to_urgent_review() -> None:
    result = evaluate_sell_signal(
        SellRuleTriggerInputs(dividend_cut=True, financial_health_severe_deterioration=True),
        Decimal("1000"),
        _CONFIG.sell,
    )
    assert result.recommendation_type == RecommendationType.URGENT_REVIEW


def test_detect_continuous_decline_true() -> None:
    values = [Decimal("100"), Decimal("90"), Decimal("80")]
    assert detect_continuous_decline(values, 2) is True


def test_detect_continuous_decline_false_when_not_monotonic() -> None:
    values = [Decimal("100"), Decimal("110"), Decimal("80")]
    assert detect_continuous_decline(values, 2) is False


def test_detect_continuous_decline_false_when_insufficient_data() -> None:
    assert detect_continuous_decline([Decimal("100"), Decimal("90")], 2) is False


def test_detect_financial_health_severe_deterioration() -> None:
    financial = FinancialSummary(
        stock_code="8136", fiscal_period_end=_NOW.date(), equity_ratio_pct=10.0, source=_SOURCE
    )
    assert detect_financial_health_severe_deterioration(financial, 15.0) is True
    assert detect_financial_health_severe_deterioration(financial, 5.0) is False


def test_classify_disclosure_risk_keywords() -> None:
    flags = classify_disclosure_risk_keywords(["第三者委員会", "監理銘柄"])
    assert flags["major_scandal"] is True
    assert flags["listing_maintenance_risk"] is True
    assert flags["accounting_problem"] is False


def test_build_sell_rule_inputs_from_data_detects_dividend_cut() -> None:
    dividend = DividendInfo(
        stock_code="8136", fiscal_year="2026", is_dividend_cut_announced=True, source=_SOURCE
    )
    financial = FinancialSummary(
        stock_code="8136", fiscal_period_end=_NOW.date(), equity_ratio_pct=50.0, source=_SOURCE
    )
    inputs = build_sell_rule_inputs_from_data(
        dividend=dividend,
        financial=financial,
        benefit=None,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
    )
    assert inputs.dividend_cut is True
    assert inputs.financial_health_severe_deterioration is False


def test_build_sell_rule_inputs_from_data_detects_continuous_income_decline() -> None:
    financial = FinancialSummary(
        stock_code="8136", fiscal_period_end=_NOW.date(), equity_ratio_pct=50.0, source=_SOURCE
    )
    inputs = build_sell_rule_inputs_from_data(
        dividend=None,
        financial=financial,
        benefit=None,
        quarterly_operating_incomes=[Decimal("100"), Decimal("90"), Decimal("80")],
        quarterly_operating_cashflows=[],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
    )
    assert inputs.continuous_operating_income_decline is True


def test_continuous_cashflow_decline_detected_without_decomposition() -> None:
    financial = FinancialSummary(
        stock_code="8136", fiscal_period_end=_NOW.date(), equity_ratio_pct=50.0, source=_SOURCE
    )
    inputs = build_sell_rule_inputs_from_data(
        dividend=None,
        financial=financial,
        benefit=None,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[Decimal("100"), Decimal("90"), Decimal("80")],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
        cashflow_decomposition=None,
    )
    # 要因分解データが無い場合、データ不足を理由に元のシグナルを弱めない
    assert inputs.continuous_operating_cashflow_decline is True


def test_continuous_cashflow_decline_suppressed_when_working_capital_driven() -> None:
    financial = FinancialSummary(
        stock_code="8136", fiscal_period_end=_NOW.date(), equity_ratio_pct=50.0, source=_SOURCE
    )
    decomposition = CashflowDecomposition(
        stock_code="8136",
        period_end=_NOW.date(),
        pretax_income=Decimal("1000"),
        receivables_change=Decimal("-3000"),  # 運転資本要因が税引前利益を上回る
        inventory_change=Decimal("0"),
        payables_change=Decimal("0"),
        one_time_items=Decimal("0"),
        ma_related_items=Decimal("0"),
        other_working_capital=Decimal("0"),
        source=_SOURCE,
    )
    inputs = build_sell_rule_inputs_from_data(
        dividend=None,
        financial=financial,
        benefit=None,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[Decimal("100"), Decimal("90"), Decimal("80")],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
        cashflow_decomposition=decomposition,
    )
    assert inputs.continuous_operating_cashflow_decline is False
