"""判定精度向上機能Phase A: DecisionSnapshot(自己評価基盤)。

企業評価・タイミング判定等のスコアリングロジック自体はPhase B以降で追加する。
Phase Aでは、判断が確定した時点で「実際に確定した最終判断値」だけを不変
スナップショットとして保存し、将来スコアが埋まった際の追跡・検証基盤を先に
用意する(スコア項目は全てNoneのまま)。

重要な設計原則(コードレビュー対応): DecisionSnapshotはRecommendationを唯一の
正本とする。StockSnapshot(判定処理の中間生成物)を直接参照しない。BUYパイプライン
等ではStockSnapshot取得後に補正・ゲート適用を行いfinal Recommendationを作るため、
StockSnapshotの値と最終Recommendationの値は一致するとは限らない。将来「現在の
ロジックで過去の判断を再計算する」ことを絶対に行わないための設計であり、
Recommendationに値が存在しない場合はNoneのまま保存する(推測で補完しない)。

`HoldingDecisionResult`(domain/entities/holding_decision.py)と同型のパターン
(Recommendationとは独立した全件保存のshadowレコード、recommendation_idはnullable)
を踏襲する。

バージョン管理の役割分担:
- `rule_version`: 既存BUY/SELL/ProfitTaking等の判定ロジック自体のバージョン
  (Recommendation.rule_versionのコピー、RuleVersionServiceが管理する既存概念)。
- `model_version`: DecisionSnapshotのスキーマ/スコアリング方式(Decision
  Enhancement Layer自体)のバージョン。Phase Bでスコアリングサービスが追加され
  始めたら値を上げる(下記DECISION_SNAPSHOT_MODEL_VERSION参照)。
- `config_values_used`: 判定当時に実際に使用された設定値(Recommendation.
  config_values_usedのコピー)。同じrule_version/model_versionでも設定値だけ
  変更された判定を区別できるようにする(Phase B以降、各スコアリングモデルの
  config値もこの領域へ同様に保存していく)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import ConfidenceLevel, DecisionType, RecommendationType

# DecisionSnapshot自体のスキーマ/スコアリング方式のバージョン(rule_versionとは別物、
# rule_versionはBUY/SELL判定ロジック自体のバージョンを指す)。Phase Bでスコアが
# 実装され始めたら値を上げる。
DECISION_SNAPSHOT_MODEL_VERSION = "phase_a_unscored_v1"


class DecisionSnapshot(ImmutableSnapshot):
    # decision_type+recommendation_idから決定的に生成する(build_decision_id()参照)。
    # 同一Recommendation・同一DecisionTypeの再実行が同じdecision_idになり、
    # Repositoryのupsertと組み合わせて冪等性(増殖しないこと)を保証する。
    decision_id: str
    decision_type: DecisionType
    stock_code: str
    evaluated_at: dt.datetime
    evaluation_date_jst: dt.date

    # --- Recommendationとの紐付け(Phase Aでは常に非None、将来の拡張を見越し
    # スキーマ上はOptionalのままにする) ---
    recommendation_id: str | None = None
    existing_action: RecommendationType | None = None

    # --- market_price / fair_value。Recommendationに保存された「最終判断値」を
    # そのままコピーする(StockSnapshotから補完しない、値が無ければNoneのまま)。---
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

    # --- 監査・バージョニング(モジュールdocstring参照) ---
    rule_version: str
    model_version: str
    config_values_used: dict[str, Any] = {}

    # --- データ出所(Recommendation.data_sourcesのコピー) ---
    data_sources: tuple[DataSourceReference, ...] = ()


def build_decision_id(decision_type: DecisionType, recommendation_id: str) -> str:
    """decision_typeとrecommendation_idから決定的なdecision_idを生成する
    (コードレビュー対応: 同一Recommendationの保存処理が再実行されてもDecisionSnapshotが
    増殖しないようにするため、毎回uuid4せず決定的IDにする)。"""
    return f"{decision_type.value}|{recommendation_id}"
