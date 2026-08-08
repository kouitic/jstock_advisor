"""判定精度向上機能Phase A: DecisionSnapshot(自己評価基盤)。

企業評価・タイミング判定等のスコアリングロジック自体はPhase B以降で追加する。
Phase Aでは、判断が確定した時点で実際に分かっていた事実(現在値・適正価格レンジ・
どのRecommendationへ紐づくか)だけを不変スナップショットとして保存し、将来
スコアが埋まった際の追跡・検証基盤を先に用意する(スコア項目は全てNoneのまま)。

`HoldingDecisionResult`(domain/entities/holding_decision.py)と同型のパターン
(Recommendationとは独立した全件保存のshadowレコード、recommendation_idはnullable)
を踏襲する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import ConfidenceLevel, DecisionType, RecommendationType

# DecisionSnapshot自体のスキーマ/スコアリング方式のバージョン(rule_versionとは別物、
# rule_versionはBUY/SELL判定ロジック自体のバージョンを指す)。Phase Bでスコアが
# 実装され始めたら値を上げる。
DECISION_SNAPSHOT_MODEL_VERSION = "phase_a_unscored_v1"


class DecisionSnapshot(ImmutableSnapshot):
    decision_id: str
    decision_type: DecisionType
    stock_code: str
    evaluated_at: dt.datetime
    evaluation_date_jst: dt.date

    # --- Recommendationとの紐付け(Phase Aでは常に非None、将来の拡張を見越し
    # スキーマ上はOptionalのままにする) ---
    recommendation_id: str | None = None
    existing_action: RecommendationType | None = None

    # --- market_price / fair_value(Phase Aで実データを入れる唯一の項目群) ---
    market_price: Decimal
    fair_value_bear: Decimal | None = None
    fair_value_neutral: Decimal | None = None
    fair_value_bull: Decimal | None = None
    fair_value_confidence: ConfidenceLevel | None = None

    # --- スコア項目(Phase Aでは全件None。Phase B/C/D/Eが埋める) ---
    timing_score: float | None = None
    historical_valuation_score: float | None = None
    earnings_surprise_score: float | None = None
    earnings_trend_score: float | None = None
    market_score: float | None = None
    sector_score: float | None = None
    environment_score: float | None = None

    # --- 監査・バージョニング ---
    rule_version: str
    model_version: str

    # --- データ出所(point-in-time保証の根拠) ---
    data_sources: tuple[DataSourceReference, ...] = ()
    data_fetched_at: dt.datetime
