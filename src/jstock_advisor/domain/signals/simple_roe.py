"""簡易予想ROE(forecast_eps / forecast_bps)の算出(BUY候補裾野拡大機能2026-08)。

`domain/signals/company_quality_scoring.py`に実装済みだった計算式を共通の
純粋関数へ切り出したもの。`FinancialSummary`にROEフィールドそのものは
存在しないため、予想EPS・予想BPSから近似する「簡易予想ROE」であり、
実績ROEとは異なる点を呼び出し側・通知文言・監査ログで明示すること。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.domain.entities.enums import EvidenceCoverageStatus


@dataclass(frozen=True)
class SimpleForecastRoeResult:
    # 簡易予想ROE(比率。例: 0.08 = 8%)。clampはしない生値。
    value: float | None
    status: EvidenceCoverageStatus


def compute_simple_forecast_roe(
    forecast_eps: Decimal | None, forecast_bps: Decimal | None
) -> SimpleForecastRoeResult:
    if forecast_eps is None or forecast_bps is None or forecast_bps <= 0:
        return SimpleForecastRoeResult(value=None, status=EvidenceCoverageStatus.NOT_EVALUATED)
    return SimpleForecastRoeResult(
        value=float(forecast_eps) / float(forecast_bps),
        status=EvidenceCoverageStatus.EVALUATED,
    )
