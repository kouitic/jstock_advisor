import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import RecommendationType, TriggerStatus
from jstock_advisor.domain.signals.sell_signal import (
    SellRuleTriggerInputs,
    build_sell_rule_inputs_from_data,
    classify_disclosure_risk_keywords,
    detect_continuous_decline,
    evaluate_sell_signal,
)
from jstock_advisor.interfaces.types import CashflowDecomposition, DividendInfo, FinancialSummary

_NOW = dt.datetime(2026, 7, 24, 7, 0, tzinfo=dt.UTC)
_SOURCE = DataSourceReference(provider="test", fetched_at=_NOW)
_CONFIG = load_config()


def _financial(**overrides: object) -> FinancialSummary:
    defaults: dict[str, object] = {
        "stock_code": "8136",
        "fiscal_period_end": _NOW.date(),
        "equity_ratio_pct": 50.0,
        "source": _SOURCE,
    }
    defaults.update(overrides)
    return FinancialSummary(**defaults)  # type: ignore[arg-type]


def _empty_inputs() -> SellRuleTriggerInputs:
    return build_sell_rule_inputs_from_data(
        dividend=None,
        financial=_financial(),
        benefit=None,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
    )


def test_no_triggers_is_hold() -> None:
    result = evaluate_sell_signal(_empty_inputs(), Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.HOLD
    assert result.stop_review_price is None
    assert result.immediate_execution_price is None


def test_detect_continuous_decline_true() -> None:
    values = [Decimal("100"), Decimal("90"), Decimal("80")]
    assert detect_continuous_decline(values, 2) is True


def test_detect_continuous_decline_false_when_not_monotonic() -> None:
    values = [Decimal("100"), Decimal("110"), Decimal("80")]
    assert detect_continuous_decline(values, 2) is False


def test_detect_continuous_decline_false_when_insufficient_data() -> None:
    assert detect_continuous_decline([Decimal("100"), Decimal("90")], 2) is False


def test_classify_disclosure_risk_keywords() -> None:
    flags = classify_disclosure_risk_keywords(["第三者委員会", "監理銘柄"])
    assert flags["major_scandal"] is True
    assert flags["listing_maintenance_risk"] is True
    assert flags["accounting_problem"] is False


# --- 業種別分類+金融業向け財務健全性ルール(§2) -------------------------------


def test_bank_equity_ratio_below_threshold_does_not_become_critical() -> None:
    # 三菱UFJフィナンシャル・グループのように業態上、自己資本比率が低い銀行に
    # 一般事業会社向けの15%基準を適用してはならない。
    financial = _financial(
        equity_ratio_pct=5.0, sector="Financial Services", industry="Banks - Diversified"
    )
    inputs = build_sell_rule_inputs_from_data(
        dividend=None,
        financial=financial,
        benefit=None,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
    )
    rule = inputs.evaluations["financial_health_severe_deterioration"]
    assert rule.status == TriggerStatus.NOT_EVALUATED

    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.HOLD


def test_bank_general_company_metrics_become_not_evaluated() -> None:
    financial = _financial(sector="Financial Services", industry="Banks - Diversified")
    inputs = build_sell_rule_inputs_from_data(
        dividend=None,
        financial=financial,
        benefit=None,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
    )
    assert (
        inputs.evaluations["financial_health_severe_deterioration"].status
        == TriggerStatus.NOT_EVALUATED
    )
    assert inputs.evaluations["regulatory_capital_breach"].status == TriggerStatus.NOT_EVALUATED
    assert inputs.evaluations["interest_bearing_debt_surge"].status == TriggerStatus.NOT_EVALUATED


def test_bank_specific_metrics_unavailable_never_sell_or_above() -> None:
    # 規制資本指標が常に取得不能な現状では、財務健全性を理由にSELL以上は絶対に出ない。
    financial = _financial(
        equity_ratio_pct=3.0, sector="Financial Services", industry="Banks - Diversified"
    )
    inputs = build_sell_rule_inputs_from_data(
        dividend=None,
        financial=financial,
        benefit=None,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
    )
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type not in (
        RecommendationType.SELL,
        RecommendationType.URGENT_REVIEW,
    )


def test_unknown_industry_financial_health_never_critical_when_equity_missing() -> None:
    financial = _financial(equity_ratio_pct=None, sector=None, industry=None)
    inputs = build_sell_rule_inputs_from_data(
        dividend=None,
        financial=financial,
        benefit=None,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
    )
    assert (
        inputs.evaluations["financial_health_severe_deterioration"].status
        == TriggerStatus.NOT_EVALUATED
    )


def test_negative_equity_ratio_is_immediate_critical_even_for_bank() -> None:
    financial = _financial(
        equity_ratio_pct=-5.0, sector="Financial Services", industry="Banks - Diversified"
    )
    inputs = build_sell_rule_inputs_from_data(
        dividend=None,
        financial=financial,
        benefit=None,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
    )
    rule = inputs.evaluations["balance_sheet_insolvency"]
    assert rule.status == TriggerStatus.TRIGGERED
    assert rule.is_immediate_critical is True
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.URGENT_REVIEW


# --- SELL/URGENT_REVIEW判定ラダー(§4) ----------------------------------------


def test_single_major_alone_never_becomes_sell() -> None:
    financial = _financial(equity_ratio_pct=50.0)
    inputs = build_sell_rule_inputs_from_data(
        dividend=DividendInfo(
            stock_code="8136",
            fiscal_year="2026",
            official_dividend_cut_announced=True,
            source=_SOURCE,
        ),
        financial=financial,
        benefit=None,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
    )
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.REVIEW


def test_single_non_immediate_critical_alone_never_becomes_urgent() -> None:
    financial = _financial(equity_ratio_pct=10.0)  # 一般事業会社、閾値未満=critical、非即時
    inputs = build_sell_rule_inputs_from_data(
        dividend=None,
        financial=financial,
        benefit=None,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
    )
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.REVIEW


def test_two_independent_major_groups_become_sell_candidate() -> None:
    financial = _financial(equity_ratio_pct=50.0)
    inputs = build_sell_rule_inputs_from_data(
        dividend=DividendInfo(
            stock_code="8136",
            fiscal_year="2026",
            official_dividend_cut_announced=True,
            source=_SOURCE,
        ),
        financial=financial,
        benefit=None,
        quarterly_operating_incomes=[Decimal("100"), Decimal("90"), Decimal("80")],
        quarterly_operating_cashflows=[],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
    )
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.SELL
    assert result.independent_evidence_group_count >= 2


def test_same_evidence_group_rules_count_as_one() -> None:
    # financial_health_severe_deterioration と interest_bearing_debt_surge は
    # いずれもBALANCE_SHEETグループのため、両方該当しても独立根拠は1件のまま。
    from jstock_advisor.domain.signals.sell_signal import SellRuleOverride

    financial = _financial(equity_ratio_pct=10.0)
    inputs = build_sell_rule_inputs_from_data(
        dividend=None,
        financial=financial,
        benefit=None,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
        interest_bearing_debt_surge=SellRuleOverride(
            status=TriggerStatus.TRIGGERED,
            explanation="有利子負債が急増",
            primary_source_confirmed=True,
        ),
    )
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.independent_evidence_group_count == 1
    assert result.recommendation_type == RecommendationType.REVIEW


def test_immediate_critical_alone_becomes_urgent_review_candidate() -> None:
    inputs = build_sell_rule_inputs_from_data(
        dividend=None,
        financial=_financial(),
        benefit=None,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[],
        disclosure_risk_keywords_found=["第三者委員会"],
        config=_CONFIG.sell,
    )
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.URGENT_REVIEW
    assert result.immediate_execution_price is not None


# --- 配当(yfinance単独の推測でofficial_dividend_cut_announcedにしない、§10・§11・§12) --


def test_yfinance_only_inference_never_sets_official_dividend_cut_announced() -> None:
    dividend = DividendInfo(
        stock_code="4631",
        fiscal_year="2026",
        is_dividend_cut_announced=True,  # 後方互換フィールド(推測)
        inferred_dividend_decrease=True,
        official_dividend_cut_announced=False,
        source=_SOURCE,
    )
    inputs = build_sell_rule_inputs_from_data(
        dividend=dividend,
        financial=_financial(),
        benefit=None,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
    )
    assert inputs.evaluations["dividend_cut"].status == TriggerStatus.NOT_TRIGGERED


def test_official_dividend_cut_confirmed_triggers_dividend_cut_rule() -> None:
    dividend = DividendInfo(
        stock_code="8136", fiscal_year="2026", official_dividend_cut_announced=True, source=_SOURCE
    )
    inputs = build_sell_rule_inputs_from_data(
        dividend=dividend,
        financial=_financial(),
        benefit=None,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
    )
    rule = inputs.evaluations["dividend_cut"]
    assert rule.status == TriggerStatus.TRIGGERED
    assert rule.primary_source_confirmed is True


# --- 営業CF要因不明時の判定抑制(§14) -----------------------------------------


def test_continuous_cashflow_decline_not_evaluated_without_decomposition() -> None:
    inputs = build_sell_rule_inputs_from_data(
        dividend=None,
        financial=_financial(),
        benefit=None,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[Decimal("100"), Decimal("90"), Decimal("80")],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
        cashflow_decomposition=None,
    )
    rule = inputs.evaluations["continuous_operating_cashflow_decline"]
    assert rule.status == TriggerStatus.NOT_EVALUATED
    assert rule.severity is None


def test_continuous_cashflow_decline_suppressed_when_working_capital_driven() -> None:
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
        financial=_financial(),
        benefit=None,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[Decimal("100"), Decimal("90"), Decimal("80")],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
        cashflow_decomposition=decomposition,
    )
    assert (
        inputs.evaluations["continuous_operating_cashflow_decline"].status
        == TriggerStatus.NOT_TRIGGERED
    )


def test_continuous_cashflow_decline_triggers_major_when_fundamentally_driven() -> None:
    decomposition = CashflowDecomposition(
        stock_code="8136",
        period_end=_NOW.date(),
        pretax_income=Decimal("1000"),
        receivables_change=Decimal("0"),
        inventory_change=Decimal("0"),
        payables_change=Decimal("0"),
        one_time_items=Decimal("0"),
        ma_related_items=Decimal("0"),
        other_working_capital=Decimal("0"),
        source=_SOURCE,
    )
    inputs = build_sell_rule_inputs_from_data(
        dividend=None,
        financial=_financial(),
        benefit=None,
        quarterly_operating_incomes=[],
        quarterly_operating_cashflows=[Decimal("100"), Decimal("90"), Decimal("80")],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
        cashflow_decomposition=decomposition,
    )
    rule = inputs.evaluations["continuous_operating_cashflow_decline"]
    assert rule.status == TriggerStatus.TRIGGERED
    assert rule.severity == "major"


# --- 現在値の価格フィールド自動コピー廃止(§7) --------------------------------


def test_review_and_sell_do_not_set_immediate_execution_price() -> None:
    financial = _financial(equity_ratio_pct=50.0)
    inputs = build_sell_rule_inputs_from_data(
        dividend=DividendInfo(
            stock_code="8136",
            fiscal_year="2026",
            official_dividend_cut_announced=True,
            source=_SOURCE,
        ),
        financial=financial,
        benefit=None,
        quarterly_operating_incomes=[Decimal("100"), Decimal("90"), Decimal("80")],
        quarterly_operating_cashflows=[],
        disclosure_risk_keywords_found=[],
        config=_CONFIG.sell,
    )
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.SELL
    assert result.immediate_execution_price is None
    assert result.stop_review_price is None
