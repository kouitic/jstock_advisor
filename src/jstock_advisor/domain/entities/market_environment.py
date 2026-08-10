"""判定精度向上機能Phase D: Market Environment Score(市場全体の地合い)の
評価結果スナップショット。

TOPIXの価格バーのみを使い、「市場全体が個別銘柄の将来成績にとって追い風か
逆風か」を-100〜+100のsigned scoreで表す。個別銘柄のエントリー適性を表す
Timing Score(domain/entities/timing_score.py)とは概念が異なり、既存の
BUY候補判定・保有判断スコア・旧売却判定・ProfitTaking判定・LINE通知など
既存の判定ロジックには一切影響しないShadow計測専用。

Historical Valuation Score/Timing Scoreと同じ設計パターン(state/score/
category/confidence/coverage/reason_codes/model_version)を踏襲する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from pydantic import model_validator

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EnvironmentCategory,
    MarketEnvironmentEvaluationState,
    TrendClassification,
)
from jstock_advisor.domain.jst import require_timezone_aware


class MarketEnvironmentResult(ImmutableSnapshot):
    state: MarketEnvironmentEvaluationState
    score: float | None = None
    category: EnvironmentCategory | None = None
    confidence: ConfidenceLevel | None = None
    coverage: float = 0.0

    # 成分別スコア(-100〜+100、算出不可の成分はNone)。評価可能な成分のみで
    # 加重平均を再正規化してscoreを算出する(欠損成分を0点として扱わない)。
    trend_structure_component: float | None = None
    medium_term_return_component: float | None = None
    drawdown_component: float | None = None

    # 監査用raw metrics(result_to_metrics()がここから復元する。他既存
    # Result(HistoricalValuationResult等)と同じく、算出できた値はstateに
    # かかわらず保持する)。
    ma20: Decimal | None = None
    ma60: Decimal | None = None
    ma20_slope_pct: float | None = None
    trend_classification: TrendClassification | None = None
    return_20d_pct: float | None = None
    return_60d_pct: float | None = None
    drawdown_from_high_pct: float | None = None
    bars_used: int = 0
    latest_bar_date: dt.date | None = None
    future_bars_filtered: bool = False
    bars_stale: bool = False

    reason_codes: tuple[str, ...] = ()

    evaluated_at: dt.datetime
    model_version: str

    @model_validator(mode="after")
    def _validate_invariants(self) -> MarketEnvironmentResult:
        require_timezone_aware(self.evaluated_at)
        if not (0 <= self.coverage <= 1):
            raise ValueError("coverageは0〜1の範囲である必要があります")
        if not self.model_version:
            raise ValueError("model_versionは必須です")
        if self.state == MarketEnvironmentEvaluationState.EVALUATED:
            if self.score is None or self.category is None or self.confidence is None:
                raise ValueError("state=EVALUATEDならscore/category/confidenceは必須です")
            if not (-100 <= self.score <= 100):
                raise ValueError("scoreは-100〜100の範囲である必要があります")
        else:
            if self.score is not None or self.category is not None or self.confidence is not None:
                raise ValueError(
                    "state=NOT_EVALUATEDならscore/category/confidenceはNoneである必要があります"
                )
        return self
