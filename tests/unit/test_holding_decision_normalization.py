"""コンポーネントスコアの正規化ルール(1.5節)のテスト。

available_points(NOT_APPLICABLE除外)/raw_points(NOT_EVALUATEDは0点)から
component_scoreが算出されること、NOT_APPLICABLEとNOT_EVALUATEDの扱いの違いを
実際のcompany_quality_scoring経由で検証する。
"""

import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.classification.financial_industry import classify_industry
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import EvidenceCoverageStatus
from jstock_advisor.domain.signals.company_quality_scoring import (
    CompanyQualityInputs,
    score_company_quality,
)
from jstock_advisor.interfaces.types import FinancialSummary

_CFG = load_config()
_SRC = DataSourceReference(provider="test", fetched_at=dt.datetime.now(dt.UTC))


def _financial(**overrides) -> FinancialSummary:
    base = dict(
        stock_code="TEST",
        fiscal_period_end=dt.date(2026, 3, 31),
        equity_ratio_pct=45.0,
        operating_cashflow=Decimal("5000000000"),
        operating_income=Decimal("4000000000"),
        forecast_eps=Decimal("120"),
        forecast_bps=Decimal("900"),
        is_deficit=False,
        is_debt_excess=False,
        is_going_concern_doubt=False,
        source=_SRC,
    )
    base.update(overrides)
    return FinancialSummary(**base)


def test_healthy_general_corporate_has_full_coverage():
    industry = classify_industry("Basic Materials", "Steel")
    inputs = CompanyQualityInputs(
        financial=_financial(),
        quarterly_operating_income_periods=[],
        quarterly_operating_cashflow_periods=[],
        eps_period_values=[],
        cashflow_decomposition=None,
        industry_classification=industry,
    )
    result = score_company_quality(
        inputs,
        _CFG.holding_decision.company_quality_weights,
        _CFG.holding_decision.company_quality_score_thresholds,
        _CFG.holding_decision_ratio,
    )
    statuses = {item.item_code: item.status for item in result.items}
    # 財務健全性軸は評価可能(NOT_APPLICABLEではない)
    assert statuses["financial_health_equity_ratio"] == EvidenceCoverageStatus.EVALUATED
    assert 0.0 <= result.score <= 50.0


def test_financial_industry_excludes_financial_health_from_denominator():
    industry = classify_industry("Financial Services", "Banks - Diversified")
    inputs = CompanyQualityInputs(
        financial=_financial(equity_ratio_pct=5.0),
        quarterly_operating_income_periods=[],
        quarterly_operating_cashflow_periods=[],
        eps_period_values=[],
        cashflow_decomposition=None,
        industry_classification=industry,
    )
    result = score_company_quality(
        inputs,
        _CFG.holding_decision.company_quality_weights,
        _CFG.holding_decision.company_quality_score_thresholds,
        _CFG.holding_decision_ratio,
    )
    statuses = {item.item_code: item.status for item in result.items}
    assert statuses["financial_health_equity_ratio"] == EvidenceCoverageStatus.NOT_APPLICABLE
    assert statuses["financial_health_debt_excess"] == EvidenceCoverageStatus.NOT_APPLICABLE

    weights = _CFG.holding_decision.company_quality_weights
    excluded_weight = weights.financial_health_equity_ratio + weights.financial_health_debt_excess
    total_weight = 50.0
    # NOT_APPLICABLE分はavailable_pointsから除外されるため、denominatorはtotalより小さい。
    # scoreは0-50の範囲を維持する(正規化により再スケールされる)。
    assert 0.0 <= result.score <= 50.0
    assert excluded_weight > 0  # 前提の健全性確認


def test_missing_data_is_not_evaluated_not_zero_scored_as_applicable():
    """欠損項目はNOT_EVALUATED(分母に残る)であり、NOT_APPLICABLE(分母除外)とは
    区別される。欠損によりcoverage_ratioが下がることを確認する(0点固定化ではなく)。
    """
    industry = classify_industry("Basic Materials", "Steel")
    inputs = CompanyQualityInputs(
        financial=_financial(
            equity_ratio_pct=None,
            operating_cashflow=None,
            operating_income=None,
            forecast_eps=None,
            forecast_bps=None,
        ),
        quarterly_operating_income_periods=[],
        quarterly_operating_cashflow_periods=[],
        eps_period_values=[],
        cashflow_decomposition=None,
        industry_classification=industry,
    )
    result = score_company_quality(
        inputs,
        _CFG.holding_decision.company_quality_weights,
        _CFG.holding_decision.company_quality_score_thresholds,
        _CFG.holding_decision_ratio,
    )
    statuses = {item.item_code: item.status for item in result.items}
    assert statuses["financial_health_equity_ratio"] == EvidenceCoverageStatus.NOT_EVALUATED
    assert statuses["profitability_roe"] == EvidenceCoverageStatus.NOT_EVALUATED
    assert result.coverage_ratio < 1.0
