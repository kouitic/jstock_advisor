"""企業品質スコア(0-50点)の算出(実装プラン2節)。

「評価時点の企業体力」を評価する(購入時点比較は投資ストーリー維持スコアの
役割)。全項目、実測値をconfig閾値で線形/段階採点する。企業規模に依存する
絶対額(EPS絶対水準・営業CF絶対水準)は使わない。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.config.models import (
    CompanyQualityScoreThresholds,
    CompanyQualityWeights,
    HoldingDecisionRatioRulesConfig,
    LinearScoreThreshold,
)
from jstock_advisor.domain.classification.financial_industry import IndustryClassificationResult
from jstock_advisor.domain.entities.enums import EvidenceCoverageStatus, IndustryClassification
from jstock_advisor.domain.entities.holding_decision import (
    CompanyQualityScore,
    RatioMetricDetail,
    ScoreItemDetail,
)
from jstock_advisor.domain.financial_decomposition import is_fundamentally_driven
from jstock_advisor.domain.financial_series import FinancialPeriodValue
from jstock_advisor.domain.signals.simple_roe import compute_simple_forecast_roe
from jstock_advisor.interfaces.types import CashflowDecomposition, FinancialSummary


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _linear_score(value: float, weight: float, threshold: LinearScoreThreshold) -> float:
    zero_at, full_at = threshold.zero_at, threshold.full_at
    if full_at == zero_at:
        return 0.0
    ratio = (value - zero_at) / (full_at - zero_at)
    return weight * _clip(ratio, 0.0, 1.0)


@dataclass(frozen=True)
class CompanyQualityInputs:
    financial: FinancialSummary
    quarterly_operating_income_periods: list[FinancialPeriodValue]
    quarterly_operating_cashflow_periods: list[FinancialPeriodValue]
    eps_period_values: list[FinancialPeriodValue]
    cashflow_decomposition: CashflowDecomposition | None
    industry_classification: IndustryClassificationResult
    listing_risk_keyword_confirmed: bool = False


def _trailing_positive_streak(periods: list[FinancialPeriodValue]) -> int:
    """period_end昇順(古い→新しい)を前提に、直近から連続で正値が続く期数を数える。"""
    ordered = sorted(periods, key=lambda p: p.period_end)
    streak = 0
    for period in reversed(ordered):
        if period.value > 0:
            streak += 1
        else:
            break
    return streak


@dataclass(frozen=True)
class _StabilityResult:
    status: EvidenceCoverageStatus
    points: float
    method: str


def _score_stability(
    periods: list[FinancialPeriodValue],
    weight: float,
    cv_threshold: LinearScoreThreshold,
    profit_ratio_threshold: LinearScoreThreshold,
    ratio_rules: HoldingDecisionRatioRulesConfig,
) -> _StabilityResult:
    """変動係数(または赤字・符号反転時は黒字期数割合)による安定性採点(2節)。"""
    ordered = sorted(periods, key=lambda p: p.period_end)
    values = [float(p.value) for p in ordered]
    if len(values) < ratio_rules.min_periods_for_stability_score:
        return _StabilityResult(EvidenceCoverageStatus.NOT_EVALUATED, 0.0, "INSUFFICIENT_PERIODS")

    mean = statistics.fmean(values)
    has_negative = any(v <= 0 for v in values)
    if abs(mean) <= ratio_rules.min_mean_for_cv_yen or has_negative:
        positive_ratio = sum(1 for v in values if v > 0) / len(values)
        return _StabilityResult(
            EvidenceCoverageStatus.EVALUATED,
            _linear_score(positive_ratio, weight, profit_ratio_threshold),
            "PROFIT_QUARTER_RATIO",
        )

    stdev = statistics.pstdev(values)
    cv = stdev / abs(mean)
    z_clip = ratio_rules.outlier_clip_zscore
    if stdev > 0:
        clipped_values = [
            mean + _clip((v - mean) / stdev, -z_clip, z_clip) * stdev for v in values
        ]
        clipped_stdev = statistics.pstdev(clipped_values)
        cv = clipped_stdev / abs(mean)
    return _StabilityResult(
        EvidenceCoverageStatus.EVALUATED,
        _linear_score(cv, weight, cv_threshold),
        "COEFFICIENT_OF_VARIATION",
    )


def score_company_quality(
    inputs: CompanyQualityInputs,
    weights: CompanyQualityWeights,
    thresholds: CompanyQualityScoreThresholds,
    ratio_rules: HoldingDecisionRatioRulesConfig,
) -> CompanyQualityScore:
    financial = inputs.financial
    is_financial_industry = (
        inputs.industry_classification.classification == IndustryClassification.FINANCIAL
    )
    items: list[ScoreItemDetail] = []
    ratio_details: list[RatioMetricDetail] = []

    # --- 財務健全性 ---
    if is_financial_industry:
        items.append(
            ScoreItemDetail(
                item_code="financial_health_equity_ratio",
                axis="financial_health",
                weight=weights.financial_health_equity_ratio,
                status=EvidenceCoverageStatus.NOT_APPLICABLE,
                reason="金融業は一般事業会社向け自己資本比率ルールを適用しない",
            )
        )
        items.append(
            ScoreItemDetail(
                item_code="financial_health_debt_excess",
                axis="financial_health",
                weight=weights.financial_health_debt_excess,
                status=EvidenceCoverageStatus.NOT_APPLICABLE,
                reason="金融業は一般事業会社向け債務超過判定を適用しない",
            )
        )
    elif financial.equity_ratio_pct is None:
        items.append(
            ScoreItemDetail(
                item_code="financial_health_equity_ratio",
                axis="financial_health",
                weight=weights.financial_health_equity_ratio,
                status=EvidenceCoverageStatus.NOT_EVALUATED,
            )
        )
        items.append(
            ScoreItemDetail(
                item_code="financial_health_debt_excess",
                axis="financial_health",
                weight=weights.financial_health_debt_excess,
                status=EvidenceCoverageStatus.EVALUATED,
                points_earned=(
                    0.0 if financial.is_debt_excess else weights.financial_health_debt_excess
                ),
            )
        )
    else:
        items.append(
            ScoreItemDetail(
                item_code="financial_health_equity_ratio",
                axis="financial_health",
                weight=weights.financial_health_equity_ratio,
                status=EvidenceCoverageStatus.EVALUATED,
                points_earned=_linear_score(
                    financial.equity_ratio_pct,
                    weights.financial_health_equity_ratio,
                    thresholds.equity_ratio_pct,
                ),
            )
        )
        items.append(
            ScoreItemDetail(
                item_code="financial_health_debt_excess",
                axis="financial_health",
                weight=weights.financial_health_debt_excess,
                status=EvidenceCoverageStatus.EVALUATED,
                points_earned=(
                    0.0 if financial.is_debt_excess else weights.financial_health_debt_excess
                ),
            )
        )

    # --- キャッシュ創出力: 営業CF/営業利益比率 ---
    missing_required: list[str] = []
    if financial.operating_cashflow is None:
        missing_required.append("operating_cashflow")
    if financial.operating_income is None or financial.operating_income <= 0:
        missing_required.append("operating_income(>0)")
    working_capital_driven = is_fundamentally_driven(inputs.cashflow_decomposition) is False
    if working_capital_driven:
        missing_required.append("working_capital_dominant_period")
    if (
        financial.operating_income is not None
        and abs(financial.operating_income)
        < Decimal(str(ratio_rules.min_operating_income_absolute_yen))
    ):
        missing_required.append("operating_income_absolute_value_too_small")

    if missing_required:
        items.append(
            ScoreItemDetail(
                item_code="cash_generation_cf_income_ratio",
                axis="cash_generation",
                weight=weights.cash_generation_cf_income_ratio,
                status=EvidenceCoverageStatus.NOT_EVALUATED,
            )
        )
        ratio_details.append(
            RatioMetricDetail(
                metric_name="cf_income_ratio",
                calculation_status=EvidenceCoverageStatus.NOT_EVALUATED,
                missing_required_metadata=tuple(missing_required),
            )
        )
    else:
        operating_cashflow: Decimal = financial.operating_cashflow  # type: ignore[assignment]
        operating_income: Decimal = financial.operating_income  # type: ignore[assignment]
        raw_ratio = float(operating_cashflow) / float(operating_income)
        clamped = _clip(
            raw_ratio, ratio_rules.clamp.ratio_clamp_min, ratio_rules.clamp.ratio_clamp_max
        )
        missing_optional = []
        if is_fundamentally_driven(inputs.cashflow_decomposition) is None:
            missing_optional.append("cashflow_decomposition_breakdown")
        items.append(
            ScoreItemDetail(
                item_code="cash_generation_cf_income_ratio",
                axis="cash_generation",
                weight=weights.cash_generation_cf_income_ratio,
                status=EvidenceCoverageStatus.EVALUATED,
                points_earned=_linear_score(
                    clamped, weights.cash_generation_cf_income_ratio, thresholds.cf_income_ratio
                ),
            )
        )
        ratio_details.append(
            RatioMetricDetail(
                metric_name="cf_income_ratio",
                calculation_status=EvidenceCoverageStatus.EVALUATED,
                missing_optional_metadata=tuple(missing_optional),
                reason_codes=("CONSOLIDATION_SCOPE_UNCONFIRMED",) if missing_optional else (),
                raw_input_value=raw_ratio,
                clamped_input_value=clamped,
            )
        )

    # --- キャッシュ創出力: 黒字連続期数 ---
    if not inputs.quarterly_operating_cashflow_periods:
        items.append(
            ScoreItemDetail(
                item_code="cash_generation_cf_streak",
                axis="cash_generation",
                weight=weights.cash_generation_cf_streak,
                status=EvidenceCoverageStatus.NOT_EVALUATED,
            )
        )
    else:
        streak = _trailing_positive_streak(inputs.quarterly_operating_cashflow_periods)
        items.append(
            ScoreItemDetail(
                item_code="cash_generation_cf_streak",
                axis="cash_generation",
                weight=weights.cash_generation_cf_streak,
                status=EvidenceCoverageStatus.EVALUATED,
                points_earned=_linear_score(
                    float(streak), weights.cash_generation_cf_streak, thresholds.cf_streak_quarters
                ),
            )
        )

    # --- 収益力: 簡易予想ROE(domain/signals/simple_roe.pyの共通関数を利用。
    # StockType分類(QUALITY/GROWTH)からも同じ関数を呼ぶことで計算式の
    # 重複実装を避ける) ---
    roe_result = compute_simple_forecast_roe(financial.forecast_eps, financial.forecast_bps)
    if roe_result.status is EvidenceCoverageStatus.NOT_EVALUATED or roe_result.value is None:
        items.append(
            ScoreItemDetail(
                item_code="profitability_roe",
                axis="profitability",
                weight=weights.profitability_roe,
                status=EvidenceCoverageStatus.NOT_EVALUATED,
            )
        )
        ratio_details.append(
            RatioMetricDetail(
                metric_name="simplified_forecast_roe",
                calculation_status=EvidenceCoverageStatus.NOT_EVALUATED,
                missing_required_metadata=("forecast_eps", "forecast_bps(>0)"),
            )
        )
    else:
        raw_roe = roe_result.value
        clamped_roe = _clip(
            raw_roe, ratio_rules.clamp.roe_clamp_min, ratio_rules.clamp.roe_clamp_max
        )
        items.append(
            ScoreItemDetail(
                item_code="profitability_roe",
                axis="profitability",
                weight=weights.profitability_roe,
                status=EvidenceCoverageStatus.EVALUATED,
                points_earned=_linear_score(clamped_roe, weights.profitability_roe, thresholds.roe),
            )
        )
        ratio_details.append(
            RatioMetricDetail(
                metric_name="simplified_forecast_roe",
                calculation_status=EvidenceCoverageStatus.EVALUATED,
                raw_input_value=raw_roe,
                clamped_input_value=clamped_roe,
            )
        )

    # --- 収益力: EPS自社履歴の安定性 ---
    eps_stability = _score_stability(
        inputs.eps_period_values,
        weights.profitability_eps_stability,
        thresholds.cv_based_stability,
        thresholds.profit_quarter_ratio,
        ratio_rules,
    )
    items.append(
        ScoreItemDetail(
            item_code="profitability_eps_stability",
            axis="profitability",
            weight=weights.profitability_eps_stability,
            status=eps_stability.status,
            points_earned=eps_stability.points,
            reason=eps_stability.method,
        )
    )

    # --- 業績安定性: 営業利益の安定性 ---
    income_stability = _score_stability(
        inputs.quarterly_operating_income_periods,
        weights.stability_operating_income,
        thresholds.cv_based_stability,
        thresholds.profit_quarter_ratio,
        ratio_rules,
    )
    items.append(
        ScoreItemDetail(
            item_code="stability_operating_income",
            axis="stability",
            weight=weights.stability_operating_income,
            status=income_stability.status,
            points_earned=income_stability.points,
            reason=income_stability.method,
        )
    )

    # --- 業績安定性: 赤字の有無 ---
    items.append(
        ScoreItemDetail(
            item_code="stability_deficit",
            axis="stability",
            weight=weights.stability_deficit,
            status=EvidenceCoverageStatus.EVALUATED,
            points_earned=0.0 if financial.is_deficit else weights.stability_deficit,
        )
    )

    # --- ガバナンス・上場継続性 ---
    items.append(
        ScoreItemDetail(
            item_code="governance_going_concern",
            axis="governance",
            weight=weights.governance_going_concern,
            status=EvidenceCoverageStatus.EVALUATED,
            points_earned=(
                0.0 if financial.is_going_concern_doubt else weights.governance_going_concern
            ),
        )
    )
    items.append(
        ScoreItemDetail(
            item_code="governance_listing_risk",
            axis="governance",
            weight=weights.governance_listing_risk,
            status=EvidenceCoverageStatus.EVALUATED,
            points_earned=(
                0.0 if inputs.listing_risk_keyword_confirmed else weights.governance_listing_risk
            ),
        )
    )

    evaluated_weight = sum(
        i.weight for i in items if i.status == EvidenceCoverageStatus.EVALUATED
    )
    available_weight = sum(
        i.weight for i in items if i.status != EvidenceCoverageStatus.NOT_APPLICABLE
    )
    raw_points = sum(i.points_earned for i in items)

    score = (raw_points / available_weight * 50.0) if available_weight > 0 else 0.0
    coverage_ratio = (evaluated_weight / available_weight) if available_weight > 0 else 0.0

    return CompanyQualityScore(
        score=score,
        coverage_ratio=coverage_ratio,
        items=tuple(items),
        ratio_metric_details=tuple(ratio_details),
    )
