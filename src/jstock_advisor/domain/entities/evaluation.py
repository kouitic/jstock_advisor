"""推奨の定点評価結果(要求仕様29〜36節)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import EvaluationLabel


class EvaluationResult(Entity):
    evaluation_id: str
    recommendation_id: str
    horizon_business_days: int
    evaluated_at: dt.datetime
    evaluation_date: dt.date

    price_at_evaluation: Decimal
    price_return_pct: float
    buy_price_based_return_pct: float | None = None

    total_return_amount: Decimal | None = None
    total_return_pct: float | None = None

    max_gain_pct: float | None = None  # 推奨後の最高値ベース
    max_drawdown_pct: float | None = None  # 推奨後の最安値ベース

    reached_tentative_buy_price: bool | None = None
    reached_standard_buy_price: bool | None = None
    reached_aggressive_buy_price: bool | None = None
    business_days_to_reach_price: int | None = None

    benchmark_symbol: str | None = None
    benchmark_return_pct: float | None = None
    excess_return_pct: float | None = None

    evaluation_label: EvaluationLabel
    label_evidence: str
    notes: str | None = None
