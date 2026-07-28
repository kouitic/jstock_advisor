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


class FairValueRange(ImmutableSnapshot):
    bear: Decimal | None
    neutral: Decimal | None
    bull: Decimal | None
    overall_confidence: ConfidenceLevel
    methods_used: list[FairValueMethodResult]
    methods_excluded: list[FairValueMethodResult]
    usable_for_trading_judgment: bool
    unusable_reason: str | None = None
