"""判定精度向上機能Phase B第二弾: Timing Score(モメンタムベースの技術的
タイミングスコア)の評価結果スナップショット。

Historical Valuation Score(domain/entities/historical_valuation.py)と同じ
設計: 単なる`float | None`ではなく、後から「なぜこの点数だったか」を
再現・検証できるよう、score/confidence/coverage/内訳(成分別スコア)を
構造化して保持する。DecisionSnapshotへ保存する際は、このResultを一度
Recommendationへコピーしたうえで、DecisionSnapshotBuilderがRecommendation
からのみコピーする(StockSnapshotを直接参照しない既存原則を維持するため)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    TimingScoreCategory,
    TimingScoreEvaluationState,
)


class TimingScoreResult(ImmutableSnapshot):
    state: TimingScoreEvaluationState
    score: float | None = None
    category: TimingScoreCategory | None = None
    confidence: ConfidenceLevel | None = None
    coverage: float = 0.0

    # 成分別スコア(-100〜+100、算出不可の成分はNone)。
    trend_component: float | None = None
    rsi_component: float | None = None
    macd_component: float | None = None
    topix_relative_strength_component: float | None = None
    sector_relative_strength_component: float | None = None
    drawdown_component: float | None = None

    # 評価全体に関する注記コード(例: "RSI_UNAVAILABLE")。
    reason_codes: tuple[str, ...] = ()

    evaluated_at: dt.datetime
    model_version: str
