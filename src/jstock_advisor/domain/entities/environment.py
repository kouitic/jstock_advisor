"""判定精度向上機能Phase D: Environment Composite Score(市場+セクターの
統合的外部環境)の評価結果スナップショット。

Market Environmentを必須のバックボーンとし(NOT_EVALUATEDなら本Resultも
NOT_EVALUATED)、Sector Environmentが評価可能なら加重平均、評価不能
(NOT_EVALUATED/NOT_APPLICABLE)ならMarketのみで評価を継続する(Sectorを
0点として混ぜない。missing semantics原則)。Sector欠損時はconfidenceの
上限をキャップし、情報が薄い状態であることを明示する。Shadow計測専用で
既存判定ロジックには一切影響しない。
"""

from __future__ import annotations

import datetime as dt

from pydantic import model_validator

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EnvironmentCategory,
    EnvironmentEvaluationState,
)
from jstock_advisor.domain.jst import require_timezone_aware


class EnvironmentResult(ImmutableSnapshot):
    state: EnvironmentEvaluationState
    score: float | None = None
    category: EnvironmentCategory | None = None
    confidence: ConfidenceLevel | None = None
    coverage: float = 0.0

    # Sectorが評価に使われたか(監査用。NOT_EVALUATED/NOT_APPLICABLEの場合False)。
    sector_available: bool = False
    market_weight_used: float | None = None
    sector_weight_used: float | None = None

    reason_codes: tuple[str, ...] = ()

    evaluated_at: dt.datetime
    model_version: str

    @model_validator(mode="after")
    def _validate_invariants(self) -> EnvironmentResult:
        require_timezone_aware(self.evaluated_at)
        if not (0 <= self.coverage <= 1):
            raise ValueError("coverageは0〜1の範囲である必要があります")
        if not self.model_version:
            raise ValueError("model_versionは必須です")
        if self.state == EnvironmentEvaluationState.EVALUATED:
            if self.score is None or self.category is None or self.confidence is None:
                raise ValueError("state=EVALUATEDならscore/category/confidenceは必須です")
            if not (-100 <= self.score <= 100):
                raise ValueError("scoreは-100〜100の範囲である必要があります")
            if self.market_weight_used is None:
                raise ValueError("state=EVALUATEDならmarket_weight_usedは必須です")
            if self.sector_available and self.sector_weight_used is None:
                raise ValueError("sector_available=Trueならsector_weight_usedは必須です")
            if not self.sector_available and self.sector_weight_used is not None:
                raise ValueError("sector_available=Falseならsector_weight_usedはNoneです")
        else:
            if self.score is not None or self.category is not None or self.confidence is not None:
                raise ValueError(
                    "state=NOT_EVALUATEDならscore/category/confidenceはNoneである必要があります"
                )
            if self.sector_available:
                raise ValueError("state=NOT_EVALUATEDならsector_available=Falseです")
            if self.market_weight_used is not None or self.sector_weight_used is not None:
                raise ValueError(
                    "state=NOT_EVALUATEDならmarket_weight_used/sector_weight_usedはNoneです"
                )
        return self
