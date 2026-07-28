"""減配判定の再設計(要求仕様5節・6節)。

比較対象の2つのDPS(1株当たり配当金)を、企業行動調整サービスで同一基準日へ
揃えたうえで比較する。分割前後を未調整のまま比較した「減配」判定は禁止する
(根本原因レポート: 原因3)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import DividendComparisonOutcome
from jstock_advisor.services.corporate_action_service import CorporateActionService

_ZERO_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class DividendClassificationResult:
    outcome: DividendComparisonOutcome
    comparison_source_period: str | None
    comparison_target_period: str | None
    source_dividend_per_share: Decimal | None  # 調整後(比較に使用した値)
    target_dividend_per_share: Decimal | None  # 調整後(比較に使用した値)
    pre_adjustment_source_dps: Decimal | None
    pre_adjustment_target_dps: Decimal | None
    cut_pct: float | None
    is_actual_vs_forecast: bool


def classify_dividend_change(
    stock_code: str,
    source_dps_raw: Decimal | None,
    source_date: dt.date | None,
    source_period_label: str | None,
    target_dps_raw: Decimal | None,
    target_date: dt.date | None,
    target_period_label: str | None,
    is_forecast_comparison: bool,
    source_ref: DataSourceReference,
    corporate_action_service: CorporateActionService | None = None,
) -> DividendClassificationResult:
    """2つのDPSを同一基準日へ揃えたうえで比較し、6種類の結果に分類する。

    is_forecast_comparison=Trueの場合(予想同士、または実績と予想の比較)は
    ACTUAL_DIVIDEND_CUTではなくFORECAST_DIVIDEND_CUTとする
    (確定的な「減配」と「予想減配」を混同しない)。
    """
    if (
        source_dps_raw is None
        or source_date is None
        or target_dps_raw is None
        or target_date is None
    ):
        return DividendClassificationResult(
            outcome=DividendComparisonOutcome.COMPARISON_NOT_POSSIBLE,
            comparison_source_period=source_period_label,
            comparison_target_period=target_period_label,
            source_dividend_per_share=None,
            target_dividend_per_share=None,
            pre_adjustment_source_dps=source_dps_raw,
            pre_adjustment_target_dps=target_dps_raw,
            cut_pct=None,
            is_actual_vs_forecast=is_forecast_comparison,
        )

    basis_date = max(source_date, target_date)
    if corporate_action_service is not None:
        source_adjusted = corporate_action_service.adjust_per_share_metric(
            source_dps_raw, stock_code, source_date, basis_date, source_ref
        )
        target_adjusted = corporate_action_service.adjust_per_share_metric(
            target_dps_raw, stock_code, target_date, basis_date, source_ref
        )
        corporate_action_service.require_matching_basis_dates(source_adjusted, target_adjusted)
        src = source_adjusted.adjusted_value
        tgt = target_adjusted.adjusted_value
        split_occurred = (
            source_adjusted.adjustment_factor != 1 or target_adjusted.adjustment_factor != 1
        )
    else:
        src, tgt = source_dps_raw, target_dps_raw
        split_occurred = False

    if abs(src - tgt) <= _ZERO_TOLERANCE:
        outcome = (
            DividendComparisonOutcome.SPLIT_ADJUSTMENT_ONLY
            if split_occurred and source_dps_raw != target_dps_raw
            else DividendComparisonOutcome.DIVIDEND_MAINTAINED
        )
        cut_pct = None
    elif tgt > src:
        outcome = DividendComparisonOutcome.DIVIDEND_INCREASE
        cut_pct = None
    else:
        outcome = (
            DividendComparisonOutcome.FORECAST_DIVIDEND_CUT
            if is_forecast_comparison
            else DividendComparisonOutcome.ACTUAL_DIVIDEND_CUT
        )
        cut_pct = float((src - tgt) / src * 100) if src > 0 else None

    return DividendClassificationResult(
        outcome=outcome,
        comparison_source_period=source_period_label,
        comparison_target_period=target_period_label,
        source_dividend_per_share=src,
        target_dividend_per_share=tgt,
        pre_adjustment_source_dps=source_dps_raw,
        pre_adjustment_target_dps=target_dps_raw,
        cut_pct=cut_pct,
        is_actual_vs_forecast=is_forecast_comparison,
    )
