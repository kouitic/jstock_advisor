"""判定精度向上機能Phase B第二弾: Timing Score(モメンタムベースの技術的
タイミングスコア)の評価結果スナップショット。

コードレビュー対応(v2): 「モメンタムが強いほど高得点」ではなく、「良い
トレンドを維持しながら、過熱しておらず、エントリーしやすい価格位置に
あるか」を評価する。trend_qualityはRSIを使わず(既存のトレンド分類は
STRONG判定にRSIを使うため、二重評価を避けるためTiming Score専用に
current_price/ma20/ma60/ma20_slope_pctのみから独立算出する)、rsi_component
は過熱・エントリー適性のみを見る。price_vs_ma20/ma60・drawdownの正のスコア
区分は全てtrend_qualityが0以下の場合0以下へキャップされ、下降トレンド中の
価格位置だけを理由に追い風点を与えない。TOPIX/セクター相対強度は将来の
Market/Sector Environment Scoreとの二重評価を避けるためTiming Scoreの
算出対象から除外する(MomentumSnapshot側のフィールド自体は温存)。

コードレビュー対応(v3): 短期急騰・過熱ペナルティ(overheat)は、他7成分と
同じ加重平均成分ではなく、base_score算出後に適用するmodifierとして分離した
(過熱情報が欠損しているだけでスコアが底上げされる不整合を解消するため)。
score(final_score)はbase_scoreからoverheat_penalty_pointsを差し引いた値。
過熱判定が不能な場合、confidenceはHIGHへ到達しない(短期急騰を確認できない
状態でエントリータイミングの信頼度を最高評価にしないため)。

コードレビュー対応(v4): current_priceのas_of_dateより未来のPriceBarが
入力へ混入した場合、MomentumSnapshot側(domain/signals/momentum.py)で
technical指標の計算全体から除外するようにした(look-ahead bias対策)。
本エンティティ自体にフィールド追加は無いが、reason_codesへ
PRICE_HISTORY_FUTURE_BARS_EXCLUDED/PRICE_HISTORY_BEHIND_CURRENT_PRICEが
追加されうる(domain/signals/timing_score.py参照)。

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

    # base成分別スコア(-100〜+100、算出不可の成分はNone)。overheatはここに
    # 含まれない(コードレビュー対応v3、下記base_score/overheat_*参照)。
    trend_quality_component: float | None = None
    price_vs_ma20_component: float | None = None
    price_vs_ma60_component: float | None = None
    rsi_component: float | None = None
    macd_component: float | None = None
    drawdown_component: float | None = None
    volume_component: float | None = None

    # コードレビュー対応(v3): overheat penaltyは通常の加重平均成分ではなく、
    # base_score算出後に適用するmodifier。base_score=7成分(trend_quality/
    # price_vs_ma20/price_vs_ma60/rsi/macd/drawdown/volume)の加重平均。
    # score(final_score)=clamp(base_score - overheat_penalty_points)。
    # overheat_penalty_applied: True=発動・False=評価可能だが不発動・
    # None=評価不能(過熱情報欠損によりscoreが上がることはない)。
    base_score: float | None = None
    overheat_penalty_applied: bool | None = None
    overheat_penalty_points: float | None = None

    # 評価全体に関する注記コード(例: "RSI_UNAVAILABLE")。
    reason_codes: tuple[str, ...] = ()

    evaluated_at: dt.datetime
    model_version: str
