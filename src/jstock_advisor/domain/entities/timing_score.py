"""判定精度向上機能Phase B第二弾: Timing Score(モメンタムベースの技術的
タイミングスコア)の評価結果スナップショット。

コードレビュー対応(v2): 「モメンタムが強いほど高得点」ではなく、「良い
トレンドを維持しながら、過熱しておらず、エントリーしやすい価格位置に
あるか」を評価する。trend_qualityはRSIを使わず(既存のトレンド分類は
STRONG判定にRSIを使うため、二重評価を避けるためTiming Score専用に
current_price/ma20/ma60/ma20_slope_pctのみから独立算出する)、rsi_component
は過熱・エントリー適性のみを見る。price_vs_ma20/ma60・drawdownの「適度な
押し目」評価はtrend_qualityが0以下の場合0へキャップされ、下降トレンド中の
下落を押し目として評価しない。TOPIX/セクター相対強度は将来のMarket/Sector
Environment Scoreとの二重評価を避けるためTiming Scoreの算出対象から除外する
(MomentumSnapshot側のフィールド自体は温存)。

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
    trend_quality_component: float | None = None
    price_vs_ma20_component: float | None = None
    price_vs_ma60_component: float | None = None
    rsi_component: float | None = None
    macd_component: float | None = None
    drawdown_component: float | None = None
    volume_component: float | None = None
    overheat_penalty_component: float | None = None

    # 評価全体に関する注記コード(例: "RSI_UNAVAILABLE")。
    reason_codes: tuple[str, ...] = ()

    evaluated_at: dt.datetime
    model_version: str
