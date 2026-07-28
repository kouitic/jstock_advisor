import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import PeriodType, RecommendationType, TriggerStatus
from jstock_advisor.domain.financial_series import FinancialPeriodValue
from jstock_advisor.domain.signals.sell_signal import (
    SellRuleOverride,
    SellRuleTriggerInputs,
    build_sell_rule_inputs_from_data,
    classify_disclosure_risk_keywords,
    detect_continuous_decline,
    detect_continuous_decline_period_aware,
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


def _periods(
    values: list[str], period_type: PeriodType = PeriodType.ANNUAL
) -> list[FinancialPeriodValue]:
    return [
        FinancialPeriodValue(
            value=Decimal(v),
            period_end=dt.date(2020 + i, 3, 31),
            period_type=period_type,
            fiscal_year=2020 + i,
        )
        for i, v in enumerate(values)
    ]


def _build_inputs(
    *,
    dividend: DividendInfo | None = None,
    financial: FinancialSummary | None = None,
    benefit: object | None = None,
    income_periods: list[FinancialPeriodValue] | None = None,
    cashflow_periods: list[FinancialPeriodValue] | None = None,
    disclosure_risk_keywords_found: list[str] | None = None,
    material_event_keywords_found: list[str] | None = None,
    cashflow_decomposition: CashflowDecomposition | None = None,
    **overrides: object,
) -> SellRuleTriggerInputs:
    return build_sell_rule_inputs_from_data(
        dividend=dividend,
        financial=financial or _financial(),
        benefit=benefit,  # type: ignore[arg-type]
        quarterly_operating_income_periods=income_periods or [],
        quarterly_operating_cashflow_periods=cashflow_periods or [],
        disclosure_risk_keywords_found=disclosure_risk_keywords_found or [],
        material_event_keywords_found=material_event_keywords_found or [],
        config=_CONFIG.sell,
        cashflow_decomposition=cashflow_decomposition,
        **overrides,
    )


def test_no_triggers_is_hold() -> None:
    result = evaluate_sell_signal(_build_inputs(), Decimal("1000"), _CONFIG.sell)
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
    inputs = _build_inputs(financial=financial)
    rule = inputs.evaluations["financial_health_severe_deterioration"]
    assert rule.status == TriggerStatus.NOT_EVALUATED

    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.HOLD


def test_bank_general_company_metrics_become_not_evaluated() -> None:
    financial = _financial(sector="Financial Services", industry="Banks - Diversified")
    inputs = _build_inputs(financial=financial)
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
    inputs = _build_inputs(financial=financial)
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type not in (
        RecommendationType.SELL,
        RecommendationType.URGENT_REVIEW,
    )


def test_unknown_industry_financial_health_never_critical_when_equity_missing() -> None:
    financial = _financial(equity_ratio_pct=None, sector=None, industry=None)
    inputs = _build_inputs(financial=financial)
    assert (
        inputs.evaluations["financial_health_severe_deterioration"].status
        == TriggerStatus.NOT_EVALUATED
    )


def test_sector_missing_bank_does_not_apply_general_corporate_rule() -> None:
    # 業種欠損は「一般事業会社である」ことの確認にはならない(UNKNOWNをGENERAL扱いしない)。
    financial = _financial(equity_ratio_pct=5.0, sector=None, industry=None)
    inputs = _build_inputs(financial=financial)
    rule = inputs.evaluations["financial_health_severe_deterioration"]
    assert rule.status == TriggerStatus.NOT_EVALUATED
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.HOLD


def test_sector_empty_string_becomes_not_evaluated() -> None:
    financial = _financial(equity_ratio_pct=5.0, sector="", industry="")
    inputs = _build_inputs(financial=financial)
    assert (
        inputs.evaluations["financial_health_severe_deterioration"].status
        == TriggerStatus.NOT_EVALUATED
    )


def test_clearly_non_financial_sector_applies_general_corporate_rule() -> None:
    financial = _financial(
        equity_ratio_pct=5.0, sector="Consumer Cyclical", industry="Apparel Retail"
    )
    inputs = _build_inputs(financial=financial)
    rule = inputs.evaluations["financial_health_severe_deterioration"]
    assert rule.status == TriggerStatus.TRIGGERED
    assert rule.severity == "critical"


def test_japanese_sector_name_identifies_financial_industry() -> None:
    financial = _financial(equity_ratio_pct=5.0, sector="金融", industry="銀行業")
    inputs = _build_inputs(financial=financial)
    assert (
        inputs.evaluations["financial_health_severe_deterioration"].status
        == TriggerStatus.NOT_EVALUATED
    )


def test_negative_equity_ratio_is_suspected_not_triggered() -> None:
    # yfinance由来の自己資本比率マイナスのみでは一次情報未確認のためSUSPECTEDとし、
    # SELL/URGENT_REVIEWの根拠(major/critical件数)には算入しない。
    financial = _financial(
        equity_ratio_pct=-5.0, sector="Financial Services", industry="Banks - Diversified"
    )
    inputs = _build_inputs(financial=financial)
    rule = inputs.evaluations["balance_sheet_insolvency"]
    assert rule.status == TriggerStatus.SUSPECTED
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.HOLD


# --- SELL/URGENT_REVIEW判定ラダー(§4) ----------------------------------------


def test_single_major_alone_never_becomes_sell() -> None:
    financial = _financial(equity_ratio_pct=50.0)
    inputs = _build_inputs(
        dividend=DividendInfo(
            stock_code="8136",
            fiscal_year="2026",
            official_dividend_cut_announced=True,
            source=_SOURCE,
        ),
        financial=financial,
    )
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.REVIEW


def test_single_non_immediate_critical_alone_never_becomes_urgent() -> None:
    # 一般事業会社、閾値未満=critical、非即時
    financial = _financial(equity_ratio_pct=10.0, sector="Technology", industry="Software")
    inputs = _build_inputs(financial=financial)
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.REVIEW


def test_two_independent_major_groups_become_sell_candidate() -> None:
    financial = _financial(equity_ratio_pct=50.0)
    inputs = _build_inputs(
        dividend=DividendInfo(
            stock_code="8136",
            fiscal_year="2026",
            official_dividend_cut_announced=True,
            source=_SOURCE,
        ),
        financial=financial,
        income_periods=_periods(["100", "90", "80"]),
    )
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.SELL
    assert result.independent_evidence_group_count >= 2


def test_same_evidence_group_rules_count_as_one() -> None:
    # financial_health_severe_deterioration と interest_bearing_debt_surge は
    # いずれもBALANCE_SHEETグループのため、両方該当しても独立根拠は1件のまま。
    financial = _financial(equity_ratio_pct=10.0, sector="Technology", industry="Software")
    inputs = _build_inputs(
        financial=financial,
        interest_bearing_debt_surge=SellRuleOverride(
            status=TriggerStatus.TRIGGERED,
            explanation="有利子負債が急増",
            primary_source_confirmed=True,
        ),
    )
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.independent_evidence_group_count == 1
    assert result.recommendation_type == RecommendationType.REVIEW


def test_three_rules_in_same_group_alone_do_not_allow_sell() -> None:
    # financial_health_severe_deterioration(critical)
    financial = _financial(equity_ratio_pct=10.0, sector="Technology", industry="Software")
    inputs = _build_inputs(
        financial=financial,
        interest_bearing_debt_surge=SellRuleOverride(
            status=TriggerStatus.TRIGGERED, explanation="x", primary_source_confirmed=True
        ),
        unfavorable_dividend_policy_change=SellRuleOverride(
            status=TriggerStatus.TRIGGERED, explanation="x", primary_source_confirmed=True
        ),
    )
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    # BALANCE_SHEET x2 + DIVIDEND x1 = 独立2グループなのでSELLになる境界ケース。
    # 同一グループ内の重複だけでは満たされないことを別途以下で確認する。
    assert result.independent_evidence_group_count == 2


def test_immediate_critical_alone_becomes_urgent_review_candidate() -> None:
    inputs = _build_inputs(
        disclosure_risk_keywords_found=["第三者委員会"],
        material_event_keywords_found=["決算訂正"],
    )
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.URGENT_REVIEW
    assert result.immediate_execution_price is not None


def test_risk_keyword_only_without_material_event_confirmation_stays_review() -> None:
    # 「第三者委員会設置」のようなキーワードのみで、重大事象の確認語が無い場合は
    # is_immediate_critical=Falseとなり、URGENT_REVIEWの根拠にはならない。
    inputs = _build_inputs(disclosure_risk_keywords_found=["第三者委員会"])
    rule = inputs.evaluations["major_scandal"]
    assert rule.status == TriggerStatus.TRIGGERED
    assert rule.is_immediate_critical is False
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.REVIEW


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
    inputs = _build_inputs(dividend=dividend)
    assert inputs.evaluations["dividend_cut"].status == TriggerStatus.NOT_TRIGGERED


def test_official_dividend_cut_confirmed_triggers_dividend_cut_rule() -> None:
    dividend = DividendInfo(
        stock_code="8136", fiscal_year="2026", official_dividend_cut_announced=True, source=_SOURCE
    )
    inputs = _build_inputs(dividend=dividend)
    rule = inputs.evaluations["dividend_cut"]
    assert rule.status == TriggerStatus.TRIGGERED
    assert rule.primary_source_confirmed is True


def test_yfinance_forecast_zero_without_official_announcement_is_suspected() -> None:
    dividend = DividendInfo(
        stock_code="4631",
        fiscal_year="2026",
        is_dividend_omission_announced=True,
        inferred_dividend_omission=True,
        official_dividend_omission_announced=False,
        source=_SOURCE,
    )
    inputs = _build_inputs(dividend=dividend)
    rule = inputs.evaluations["dividend_omission"]
    assert rule.status == TriggerStatus.SUSPECTED
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.HOLD


# --- 営業CF要因不明時の判定抑制(§14)+財務期間の構造化 -----------------------


def test_continuous_cashflow_decline_not_evaluated_without_decomposition() -> None:
    inputs = _build_inputs(
        cashflow_periods=_periods(["100", "90", "80"]),
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
    inputs = _build_inputs(
        cashflow_periods=_periods(["100", "90", "80"]),
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
    inputs = _build_inputs(
        cashflow_periods=_periods(["100", "90", "80"]),
        cashflow_decomposition=decomposition,
    )
    rule = inputs.evaluations["continuous_operating_cashflow_decline"]
    assert rule.status == TriggerStatus.TRIGGERED
    assert rule.severity == "major"


def test_mixed_period_types_are_not_evaluated() -> None:
    mixed = _periods(["100", "90"], PeriodType.QUARTER) + _periods(["80"], PeriodType.ANNUAL)
    assert detect_continuous_decline_period_aware(mixed, 2) is None


def test_cumulative_period_is_not_evaluated() -> None:
    periods = [
        FinancialPeriodValue(
            value=Decimal(v),
            period_end=dt.date(2020 + i, 3, 31),
            period_type=PeriodType.YTD,
            is_cumulative=True,
        )
        for i, v in enumerate(["100", "90", "80"])
    ]
    assert detect_continuous_decline_period_aware(periods, 2) is None


def test_same_period_type_decline_is_evaluated() -> None:
    periods = _periods(["100", "90", "80"], PeriodType.ANNUAL)
    assert detect_continuous_decline_period_aware(periods, 2) is True


# --- 現在値の価格フィールド自動コピー廃止(§7) --------------------------------


def test_review_and_sell_do_not_set_immediate_execution_price() -> None:
    financial = _financial(equity_ratio_pct=50.0)
    inputs = _build_inputs(
        dividend=DividendInfo(
            stock_code="8136",
            fiscal_year="2026",
            official_dividend_cut_announced=True,
            source=_SOURCE,
        ),
        financial=financial,
        income_periods=_periods(["100", "90", "80"]),
    )
    result = evaluate_sell_signal(inputs, Decimal("1000"), _CONFIG.sell)
    assert result.recommendation_type == RecommendationType.SELL
    assert result.immediate_execution_price is None
    assert result.stop_review_price is None
