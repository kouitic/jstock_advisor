"""判定精度向上機能Phase D: Sector Environment Score(所属セクターの地合い)の
評価結果スナップショット。

config.momentum.sector_etf_mapに対応ETFが登録されている業種のみ評価する。
対応ETFが無い業種は0点/NEUTRALではなくstate=NOT_APPLICABLEとして明示的に
区別する(判定精度向上機能Phase Dの missing semantics 原則)。Market
Environmentと同じ3成分パターンに加え、TOPIXに対する相対強度(relative
strength)を主要成分として持つ(市場全体が上昇していてもセクターがそれに
劣後していれば負のスコアになる)。Shadow計測専用で既存判定ロジックには
一切影響しない。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from pydantic import model_validator

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EnvironmentCategory,
    SectorEnvironmentEvaluationState,
    TrendClassification,
)
from jstock_advisor.domain.jst import require_timezone_aware


class SectorEnvironmentResult(ImmutableSnapshot):
    state: SectorEnvironmentEvaluationState
    # 対応するETFシンボル(sector_etf_mapに未登録の業種はNone)。
    sector_etf_symbol: str | None = None
    score: float | None = None
    category: EnvironmentCategory | None = None
    confidence: ConfidenceLevel | None = None
    coverage: float = 0.0

    trend_structure_component: float | None = None
    medium_term_return_component: float | None = None
    relative_strength_component: float | None = None

    # 監査用raw metrics(result_to_metrics()がここから復元する)。
    ma20: Decimal | None = None
    ma60: Decimal | None = None
    ma20_slope_pct: float | None = None
    trend_classification: TrendClassification | None = None
    return_20d_pct: float | None = None
    return_60d_pct: float | None = None
    relative_strength_20d_pct: float | None = None
    relative_strength_60d_pct: float | None = None
    bars_used: int = 0
    latest_bar_date: dt.date | None = None
    future_bars_filtered: bool = False
    bars_stale: bool = False

    reason_codes: tuple[str, ...] = ()

    evaluated_at: dt.datetime
    model_version: str

    @model_validator(mode="after")
    def _validate_invariants(self) -> SectorEnvironmentResult:
        require_timezone_aware(self.evaluated_at)
        if not (0 <= self.coverage <= 1):
            raise ValueError("coverageは0〜1の範囲である必要があります")
        if not self.model_version:
            raise ValueError("model_versionは必須です")
        if self.state == SectorEnvironmentEvaluationState.EVALUATED:
            if self.sector_etf_symbol is None:
                raise ValueError("state=EVALUATEDならsector_etf_symbolは必須です")
            if self.score is None or self.category is None or self.confidence is None:
                raise ValueError("state=EVALUATEDならscore/category/confidenceは必須です")
            if not (-100 <= self.score <= 100):
                raise ValueError("scoreは-100〜100の範囲である必要があります")
        else:
            if self.score is not None or self.category is not None or self.confidence is not None:
                raise ValueError(
                    "state=NOT_EVALUATED/NOT_APPLICABLEならscore/category/confidenceは"
                    "Noneである必要があります"
                )
        return self
