"""平準化EPS(2026-07 BUYパイプライン再設計。要求仕様13節)。

景気循環銘柄や市況上昇で利益が一時的に増えている銘柄では、単年度予想EPSだけで
PER方式の適正価格を算出しない。過去の黒字年度の中央値と予想EPSの小さい方を
採用することで、一時的な増益を適正価格へそのまま反映しないようにする。

株式分割未調整期間・特別利益が大きい年度・事業売却等の非継続事業・会計基準
変更で比較不能な年度を機械的に除外するためのデータソースは存在しないため
(推測で補完しない方針)、赤字年度(異常値)のみを自動除外し、それ以外の
除外条件を確認できない状態で平準化を行った場合は信頼度をMEDIUM以下に
制限する。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.interfaces.types import HistoricalValuation

_MIN_POSITIVE_EPS_YEARS_REQUIRED = 2


@dataclass(frozen=True)
class EpsNormalizationResult:
    normalized_eps: Decimal | None
    method: str
    confidence: ConfidenceLevel
    # 平準化対象から除外した年度・理由(通知・監査ログ用)
    excluded_years: tuple[str, ...] = ()


def normalize_eps(
    forecast_eps: Decimal | None,
    historical_valuations: list[HistoricalValuation],
    is_cyclical_industry: bool,
) -> EpsNormalizationResult:
    if forecast_eps is None:
        return EpsNormalizationResult(
            normalized_eps=None,
            method="NO_FORECAST_EPS",
            confidence=ConfidenceLevel.LOW,
        )

    if not is_cyclical_industry:
        return EpsNormalizationResult(
            normalized_eps=forecast_eps,
            method="NOT_APPLICABLE_NON_CYCLICAL",
            confidence=ConfidenceLevel.HIGH,
        )

    excluded_years: list[str] = []
    positive_points: list[tuple[str, Decimal]] = []
    for point in historical_valuations:
        if point.eps is None:
            continue
        if point.eps <= 0:
            excluded_years.append(f"{point.date.isoformat()}: 赤字転落による異常値のため除外")
            continue
        positive_points.append((point.date.isoformat(), point.eps))

    if len(positive_points) < _MIN_POSITIVE_EPS_YEARS_REQUIRED:
        return EpsNormalizationResult(
            normalized_eps=forecast_eps,
            method="FORECAST_EPS_INSUFFICIENT_HISTORY",
            confidence=ConfidenceLevel.MEDIUM,
            excluded_years=tuple(excluded_years),
        )

    median_positive_eps = statistics.median(value for _, value in positive_points)
    normalized_eps = min(forecast_eps, median_positive_eps)

    # 株式分割未調整・特別利益・非継続事業・会計基準変更の除外を機械的に確認できる
    # データソースが無いため、正常値のみでの平準化であっても信頼度はMEDIUM上限とする。
    return EpsNormalizationResult(
        normalized_eps=normalized_eps,
        method="MIN_FORECAST_AND_5Y_MEDIAN_POSITIVE_EPS",
        confidence=ConfidenceLevel.MEDIUM,
        excluded_years=tuple(excluded_years),
    )
