"""適正価格の複数手法化(要求仕様8節)。

単一の適正価格を絶対値として扱わず、複数手法の結果をレンジ(弱気/中立/強気)と
して保持する。各手法は算出できなかった場合、捏造せずexclusion_reasonを残す。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.enums import ConfidenceLevel


class FairValueMethodResult(ImmutableSnapshot):
    method: str  # "target_yield" | "per" | "pbr" | "historical_range" | "dcf"
    fair_value: Decimal | None
    input_values: dict[str, str] = {}
    input_dates: dict[str, dt.date] = {}
    assumptions: dict[str, str] = {}
    confidence: ConfidenceLevel
    exclusion_reason: str | None = None

    # --- BUYパイプライン再設計(2026-07)で追加。算出できたことと、その結果を
    # 適正価格集計に採用してよいことは別(要求仕様9節・10節)。不適切な前提
    # (EPS負数・分割未調整・特別配当の恒常化等)の場合はapplicable=Falseとし、
    # exclusion_reasonへ理由を残したうえで集計(min/max/median/mean)から除外する ---
    applicable: bool = True
    source_date: dt.date | None = None


class FairValueRange(ImmutableSnapshot):
    bear: Decimal | None
    neutral: Decimal | None
    bull: Decimal | None
    overall_confidence: ConfidenceLevel
    methods_used: list[FairValueMethodResult]
    methods_excluded: list[FairValueMethodResult]
    usable_for_trading_judgment: bool
    unusable_reason: str | None = None

    # --- BUYパイプライン再設計(2026-07)で追加。単一の「最終適正価格」ではなく
    # 手法間のバラつきを扱えるようにする(要求仕様9節)。SELL側の
    # usable_for_trading_judgmentはそのまま維持し、これらは追加情報として扱う ---
    valuation_min: Decimal | None = None
    valuation_max: Decimal | None = None
    valuation_median: Decimal | None = None
    valuation_mean: Decimal | None = None
    valuation_dispersion_ratio: float | None = None  # = valuation_max / valuation_min
    methods_used_count: int | None = None
