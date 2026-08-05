"""企業品質スコアの比率指標メタデータ(必須/参考区分)・変動係数異常系のテスト
(実装プラン2節・20節)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.classification.financial_industry import IndustryClassificationResult
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import (
    EvidenceCoverageStatus,
    IndustryClassification,
    PeriodType,
)
from jstock_advisor.domain.financial_series import FinancialPeriodValue
from jstock_advisor.domain.signals.company_quality_scoring import (
    CompanyQualityInputs,
    score_company_quality,
)
from jstock_advisor.interfaces.types import CashflowDecomposition, FinancialSummary

_CFG = load_config()
_WEIGHTS = _CFG.holding_decision.company_quality_weights
_THRESHOLDS = _CFG.holding_decision.company_quality_score_thresholds
_RATIO_RULES = _CFG.holding_decision_ratio
_GENERAL = IndustryClassificationResult(IndustryClassification.GENERAL_CORPORATE)
_NOW = dt.datetime(2026, 4, 1, tzinfo=dt.UTC)


def _financial(**overrides) -> FinancialSummary:
    base = dict(
        stock_code="9999",
        source=DataSourceReference(provider="test", fetched_at=_NOW),
        fiscal_period_end=dt.date(2026, 3, 31),
        equity_ratio_pct=45.0,
        operating_cashflow=Decimal("1000"),
        operating_income=Decimal("900"),
        forecast_eps=Decimal("100"),
        forecast_bps=Decimal("1000"),
        is_debt_excess=False,
        is_deficit=False,
        is_going_concern_doubt=False,
    )
    base.update(overrides)
    return FinancialSummary(**base)


def _period(
    value: str, period_end: dt.date, period_type: PeriodType = PeriodType.ANNUAL
) -> FinancialPeriodValue:
    return FinancialPeriodValue(
        value=Decimal(value), period_end=period_end, period_type=period_type
    )


def _inputs(**overrides) -> CompanyQualityInputs:
    base = dict(
        financial=_financial(),
        quarterly_operating_income_periods=[],
        quarterly_operating_cashflow_periods=[],
        eps_period_values=[],
        cashflow_decomposition=None,
        industry_classification=_GENERAL,
    )
    base.update(overrides)
    return CompanyQualityInputs(**base)


def _item(result, code: str):
    return next(i for i in result.items if i.item_code == code)


def test_cf_income_ratio_not_evaluated_when_operating_income_non_positive():
    result = score_company_quality(
        _inputs(financial=_financial(operating_income=Decimal("0"))),
        _WEIGHTS, _THRESHOLDS, _RATIO_RULES,
    )
    item = _item(result, "cash_generation_cf_income_ratio")
    assert item.status == EvidenceCoverageStatus.NOT_EVALUATED


def test_cf_income_ratio_not_evaluated_when_denominator_too_small():
    tiny = Decimal(str(_RATIO_RULES.min_operating_income_absolute_yen)) / 2
    result = score_company_quality(
        _inputs(financial=_financial(operating_income=tiny, operating_cashflow=tiny)),
        _WEIGHTS, _THRESHOLDS, _RATIO_RULES,
    )
    item = _item(result, "cash_generation_cf_income_ratio")
    assert item.status == EvidenceCoverageStatus.NOT_EVALUATED


def test_cf_income_ratio_clamped_to_configured_bounds():
    huge_ratio_financial = _financial(
        operating_cashflow=Decimal("500000000"), operating_income=Decimal("50000000")
    )
    decomposition = CashflowDecomposition(
        stock_code="9999",
        period_end=dt.date(2026, 3, 31),
        pretax_income=Decimal("100"),
        receivables_change=Decimal("0"),
        inventory_change=Decimal("0"),
        payables_change=Decimal("0"),
        tax_paid=Decimal("0"),
        one_time_items=Decimal("0"),
        source=DataSourceReference(provider="test", fetched_at=_NOW),
    )
    result = score_company_quality(
        _inputs(financial=huge_ratio_financial, cashflow_decomposition=decomposition),
        _WEIGHTS, _THRESHOLDS, _RATIO_RULES,
    )
    item = _item(result, "cash_generation_cf_income_ratio")
    assert item.status == EvidenceCoverageStatus.EVALUATED
    assert item.points_earned == _WEIGHTS.cash_generation_cf_income_ratio
    detail = next(d for d in result.ratio_metric_details if d.metric_name == "cf_income_ratio")
    assert detail.clamped_input_value == _RATIO_RULES.clamp.ratio_clamp_max


def test_cf_income_ratio_not_evaluated_when_working_capital_dominant():
    decomposition = CashflowDecomposition(
        stock_code="9999",
        period_end=dt.date(2026, 3, 31),
        pretax_income=Decimal("100"),
        receivables_change=Decimal("100000"),
        inventory_change=Decimal("0"),
        payables_change=Decimal("0"),
        tax_paid=Decimal("0"),
        one_time_items=Decimal("0"),
        ma_related_items=Decimal("0"),
        other_working_capital=Decimal("0"),
        source=DataSourceReference(provider="test", fetched_at=_NOW),
    )
    result = score_company_quality(
        _inputs(
            financial=_financial(operating_income=Decimal("50000000")),
            cashflow_decomposition=decomposition,
        ),
        _WEIGHTS, _THRESHOLDS, _RATIO_RULES,
    )
    item = _item(result, "cash_generation_cf_income_ratio")
    assert item.status == EvidenceCoverageStatus.NOT_EVALUATED
    detail = next(d for d in result.ratio_metric_details if d.metric_name == "cf_income_ratio")
    assert "working_capital_dominant_period" in detail.missing_required_metadata


def test_roe_not_evaluated_when_forecast_bps_non_positive():
    result = score_company_quality(
        _inputs(financial=_financial(forecast_bps=Decimal("0"))),
        _WEIGHTS,
        _THRESHOLDS,
        _RATIO_RULES,
    )
    item = _item(result, "profitability_roe")
    assert item.status == EvidenceCoverageStatus.NOT_EVALUATED


def test_roe_evaluated_and_clamped_within_configured_bounds():
    result = score_company_quality(
        _inputs(financial=_financial(forecast_eps=Decimal("100000"), forecast_bps=Decimal("1"))),
        _WEIGHTS, _THRESHOLDS, _RATIO_RULES,
    )
    item = _item(result, "profitability_roe")
    assert item.status == EvidenceCoverageStatus.EVALUATED
    detail = next(
        d for d in result.ratio_metric_details if d.metric_name == "simplified_forecast_roe"
    )
    assert detail.clamped_input_value == _RATIO_RULES.clamp.roe_clamp_max


def test_stability_uses_profit_quarter_ratio_when_series_has_negative_values():
    periods = [
        _period("-10", dt.date(2023, 3, 31)),
        _period("20", dt.date(2024, 3, 31)),
        _period("30", dt.date(2025, 3, 31)),
        _period("40", dt.date(2026, 3, 31)),
    ]
    result = score_company_quality(
        _inputs(quarterly_operating_income_periods=periods), _WEIGHTS, _THRESHOLDS, _RATIO_RULES
    )
    item = _item(result, "stability_operating_income")
    assert item.status == EvidenceCoverageStatus.EVALUATED
    assert item.reason == "PROFIT_QUARTER_RATIO"


def test_stability_uses_coefficient_of_variation_when_series_is_all_positive():
    periods = [
        _period("100000000", dt.date(2023, 3, 31)),
        _period("105000000", dt.date(2024, 3, 31)),
        _period("95000000", dt.date(2025, 3, 31)),
        _period("110000000", dt.date(2026, 3, 31)),
    ]
    result = score_company_quality(
        _inputs(quarterly_operating_income_periods=periods), _WEIGHTS, _THRESHOLDS, _RATIO_RULES
    )
    item = _item(result, "stability_operating_income")
    assert item.status == EvidenceCoverageStatus.EVALUATED
    assert item.reason == "COEFFICIENT_OF_VARIATION"


def test_stability_not_evaluated_when_below_minimum_periods():
    periods = [_period("100", dt.date(2025, 3, 31)), _period("110", dt.date(2026, 3, 31))]
    assert len(periods) < _RATIO_RULES.min_periods_for_stability_score
    result = score_company_quality(
        _inputs(eps_period_values=periods), _WEIGHTS, _THRESHOLDS, _RATIO_RULES
    )
    item = _item(result, "profitability_eps_stability")
    assert item.status == EvidenceCoverageStatus.NOT_EVALUATED


def test_financial_industry_marks_financial_health_axis_not_applicable():
    financial_industry = IndustryClassificationResult(IndustryClassification.FINANCIAL)
    result = score_company_quality(
        _inputs(industry_classification=financial_industry), _WEIGHTS, _THRESHOLDS, _RATIO_RULES
    )
    equity_item = _item(result, "financial_health_equity_ratio")
    debt_item = _item(result, "financial_health_debt_excess")
    assert equity_item.status == EvidenceCoverageStatus.NOT_APPLICABLE
    assert debt_item.status == EvidenceCoverageStatus.NOT_APPLICABLE
    # NOT_APPLICABLEは分母(available weight)から除外されるため、財務健全性軸を
    # 除いた残り項目だけでスコアが正規化される。
    assert result.coverage_ratio > 0.0
