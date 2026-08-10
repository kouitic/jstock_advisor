"""判定精度向上機能Phase A/B/C: DecisionSnapshot(自己評価基盤)。

Phase Aでは、判断が確定した時点で「実際に確定した最終判断値」だけを不変
スナップショットとして保存し、将来スコアが埋まった際の追跡・検証基盤を先に
用意した(スコア項目は全てNoneのまま)。Phase B第一弾として
historical_valuation_score(銘柄自身の過去PER/PBR水準に対する現在値のランク
ベーススコア)を実装済み(domain/signals/historical_valuation.py参照)。
Phase B第二弾としてtiming_score(既存MomentumSnapshotを基にしたモメンタム
ベースの技術的タイミングスコア)を実装済み(domain/signals/timing_score.py
参照)。Phase Cとしてearnings_surprise_score(決算サプライズスコア、
domain/signals/earnings_surprise.py参照)・earnings_trend_score(業績
トレンドスコア、domain/signals/earnings_trend.py参照)を実装済み。いずれも
Shadow計測専用であり、BUY候補判定・保有判断スコア・旧売却判定・ProfitTaking
判定・LINE通知など既存の判定ロジックには一切影響しない。market_score/
sector_score/environment_scoreは将来のPhase D(Market/Sector Environment
Score)で追加していく。

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

from pydantic import Field

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import ConfidenceLevel, DecisionType, RecommendationType

# DecisionSnapshot自体のスキーマ/スコアリング方式のバージョン(rule_versionとは別物、
# rule_versionはBUY/SELL判定ロジック自体のバージョンを指す)。Phase B第一弾
# (historical_valuation_score実装)、Phase B第二弾(timing_score実装・v2/v3/v4への
# 再設計、コードレビュー対応)、Phase C(earnings_surprise_score/
# earnings_trend_score実装・v2への再設計、コードレビュー対応)に伴い値を
# 上げた。他のスコア項目が実装され始めたらさらに値を上げる。
DECISION_SNAPSHOT_MODEL_VERSION = "phase_c_earnings_v2"


class DecisionSnapshot(ImmutableSnapshot):
    # recommendation_idから決定的に生成する(build_decision_id()参照)。横断調査の結果、
    # 生産コードでは1つのRecommendationは常に単一のDecisionTypeでのみDecisionSnapshotを
    # 保存する(recommendation_idはRecommendation構築時にuuid4で都度新規発行され、以降
    # 同じRecommendationインスタンスに対してsave_decision_snapshot_safely()が呼ばれるのは
    # 1回のみ)。そのため「1 Recommendation = 1 DecisionSnapshot」をモデルとして採用し、
    # decision_idはrecommendation_idのみから決定的に生成する(decision_typeは含めない)。
    # RepositoryはDecisionSnapshotに対してupsertを行わず、insert_if_absentのみを使う
    # (コードレビュー対応: 一度保存された記録は後から絶対に上書きしない。同一
    # decision_idの再実行は「完全に同一内容」の場合のみ正常な冪等再実行として扱い、
    # 内容が異なる場合はdecision_snapshot_conflictとして検知し既存の値を保持する)。
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

    # --- スコア項目 ---
    # 銘柄自身の過去PER/PBR水準に対する現在値のランクベーススコア
    # (-100〜+100、算出不可時はNone)。Phase B第一弾で実装済み
    # (domain/signals/historical_valuation.py参照、Shadow計測専用)。
    # confidence/coverage/reason_codes/metricsは「なぜこの点数だったか」を
    # 後から再現・検証できるようにするための監査情報(コードレビュー対応)。
    historical_valuation_score: float | None = None
    historical_valuation_confidence: ConfidenceLevel | None = None
    historical_valuation_coverage: float | None = None
    historical_valuation_reason_codes: tuple[str, ...] = ()
    historical_valuation_metrics: dict[str, Any] = Field(default_factory=dict)
    # モメンタムベースの技術的タイミングスコア(-100〜+100、算出不可時はNone)。
    # Phase B第二弾で実装済み(domain/signals/timing_score.py参照、
    # Shadow計測専用)。historical_valuation_*と同じ5フィールドパターン。
    timing_score: float | None = None
    timing_confidence: ConfidenceLevel | None = None
    timing_coverage: float | None = None
    timing_reason_codes: tuple[str, ...] = ()
    timing_metrics: dict[str, Any] = Field(default_factory=dict)
    # 決算サプライズスコア(-100〜+100、算出不可時はNone)。Phase Cで実装済み
    # (domain/signals/earnings_surprise.py参照、Shadow計測専用)。
    # historical_valuation_*と同じ5フィールドパターン。
    earnings_surprise_score: float | None = None
    earnings_surprise_confidence: ConfidenceLevel | None = None
    earnings_surprise_coverage: float | None = None
    earnings_surprise_reason_codes: tuple[str, ...] = ()
    earnings_surprise_metrics: dict[str, Any] = Field(default_factory=dict)
    # 業績トレンドスコア(-100〜+100、算出不可時はNone)。Phase Cで実装済み
    # (domain/signals/earnings_trend.py参照、Shadow計測専用)。
    # historical_valuation_*と同じ5フィールドパターン。
    earnings_trend_score: float | None = None
    earnings_trend_confidence: ConfidenceLevel | None = None
    earnings_trend_coverage: float | None = None
    earnings_trend_reason_codes: tuple[str, ...] = ()
    earnings_trend_metrics: dict[str, Any] = Field(default_factory=dict)
    market_score: float | None = None
    sector_score: float | None = None
    environment_score: float | None = None

    # --- 監査・バージョニング(モジュールdocstring参照) ---
    rule_version: str
    model_version: str
    config_values_used: dict[str, Any] = Field(default_factory=dict)

    # --- データ出所(Recommendation.data_sourcesのコピー) ---
    data_sources: tuple[DataSourceReference, ...] = ()


def build_decision_id(recommendation_id: str) -> str:
    """recommendation_idから決定的なdecision_idを生成する(コードレビュー対応:
    同一Recommendationの保存処理が再実行されてもDecisionSnapshotが増殖しないよう、
    毎回uuid4せず決定的IDにする)。「decision|」は名前空間を明確にするための固定
    プレフィックスであり、decision_typeは含めない(1 Recommendation = 1
    DecisionSnapshotのため、decision_typeをIDに含める必要がない)。"""
    return f"decision|{recommendation_id}"
