"""判定精度向上機能Phase B: Historical Valuation Score(過去バリュエーション比較
スコア)の評価結果スナップショット。

コードレビュー対応: 単なる`float | None`ではなく、後から「なぜこの点数だったか」
を再現・検証できるよう、score/confidence/coverage/内訳(percentile・データ
件数・basis)を構造化して保持する。DecisionSnapshotへ保存する際は、この
Resultを一度Recommendationへコピーしたうえで、DecisionSnapshotBuilderが
Recommendationからのみコピーする(StockSnapshotを直接参照しない既存原則を
維持するため)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    HistoricalValuationCategory,
    HistoricalValuationEvaluationState,
    ValuationBasis,
)


class HistoricalValuationResult(ImmutableSnapshot):
    state: HistoricalValuationEvaluationState
    score: float | None = None
    category: HistoricalValuationCategory | None = None
    confidence: ConfidenceLevel | None = None
    coverage: float = 0.0

    per_score: float | None = None
    pbr_score: float | None = None
    per_percentile: float | None = None
    pbr_percentile: float | None = None

    current_per: Decimal | None = None
    current_pbr: Decimal | None = None
    current_per_basis: ValuationBasis | None = None
    current_pbr_basis: ValuationBasis | None = None

    per_data_count_raw: int = 0
    per_data_count_used: int = 0
    pbr_data_count_raw: int = 0
    pbr_data_count_used: int = 0

    # PBR算出に使った過去データに株式数近似(HistoricalValuation.pbr_is_approximate)
    # が1件でも含まれ、かつPBRコンポーネントが実際にスコアへ使われた場合True
    # (コードレビュー対応)。この場合confidenceはHIGHへ到達しない。
    pbr_is_approximate: bool = False

    # データ品質フィルタで除外した理由コード群(重複なし)。
    # 例: "BASIS_MISMATCH_EXCLUDED", "FUTURE_DATE_EXCLUDED", "OUTLIER_EXCLUDED"。
    excluded_data_reasons: tuple[str, ...] = ()
    # 評価全体に関する注記コード(データ不足でconfidenceを下げた等)。
    reason_codes: tuple[str, ...] = ()

    evaluated_at: dt.datetime
    model_version: str
