"""判定精度向上機能Phase A: DecisionSnapshotの成績集計(非永続)。

コードレビュー対応: DecisionSnapshot専用のEvaluationResultは生成しない
(recommendation_evaluation_service.pyが既に生成しているEvaluationResultを
そのまま再利用する)。よってjoinは`EvaluationResult.recommendation_id` ==
`DecisionSnapshot.recommendation_id`で行い、Phase A成績評価の対象ホライズン
(既定5/20/60/120/250営業日、config.decision_evaluation.horizons_business_days)
のみへ絞り込む。これにより1営業日の短期チェックポイントや7暦日評価
(週次改善レビュー専用)がDecisionPerformanceへ混入しない。

performance_metrics_service.pyのMetricsBucket/build_metrics_bucketをそのまま
再利用し、既存のPerformanceMetricsService/週次改善レビューとは完全に独立させる
(既存ファイルは変更しない)。

判定精度向上機能次フェーズ(DecisionPerformance分析強化)で追加: 4つの
Shadow Score(historical_valuation/timing/earnings_surprise/earnings_trend)を
category/confidence/coverage tier/個別model_version別に分析できるように
した。以下の原則を厳守する。

- 過去DecisionSnapshotを現在ロードしたAppConfigで再分類しない
  (category/confidenceはDecisionSnapshotへ保存済みの当時の値をそのまま使い、
  coverage tierも当時の閾値(DecisionSnapshot.config_values_used)から復元する。
  現在のAppConfigの閾値を過去レコードへ適用することはしない)。
- 異なるhorizon_business_daysのOutcomeを1つのbucketへ混在させない
  (summarize_score_segments()/compare_segments()はhorizon_business_daysを
  必須引数とする。既存summarize()のhorizon=None全合算仕様は後方互換のため
  維持する)。
- 週次レビューと共有するMetricsBucketは肥大化させない
  (DecisionPerformance専用のDecisionPerformanceSegmentを新設し、
  median/MFE/MAE等の追加指標はこちらにのみ持たせる)。
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import statistics
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.decision_snapshot import DecisionSnapshot
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.services.performance_metrics_service import MetricsBucket, build_metrics_bucket

logger = logging.getLogger(__name__)

# CloudWatch Logsで固定文字列として検索可能なイベントキー(コードレビュー対応:
# モデル上「1 Recommendation = 1 DecisionSnapshot」のはずだが、不正なデータ投入等で
# 同一recommendation_idに複数のDecisionSnapshotが存在した場合、list順によって
# 集計結果が変わってしまうのを防ぐため、黙って1件を採用せず対象を集計除外する)。
DECISION_PERFORMANCE_DUPLICATE_SNAPSHOT_EVENT = "decision_performance_duplicate_snapshot"
# コードレビュー対応: 過去DecisionSnapshotのconfig_values_usedに保存された
# coverage閾値が壊れている(欠損・型不正・範囲外・medium>=high等)場合に、
# 1件の異常データでレポート全体を失敗させず、当該DecisionSnapshotを
# coverage_tier分析からのみ除外して継続するためのイベントキー。
DECISION_PERFORMANCE_INVALID_COVERAGE_THRESHOLD_EVENT = (
    "decision_performance_invalid_coverage_threshold"
)

ScoreName = Literal["historical_valuation", "timing", "earnings_surprise", "earnings_trend"]
_SCORE_NAMES: tuple[ScoreName, ...] = (
    "historical_valuation",
    "timing",
    "earnings_surprise",
    "earnings_trend",
)

# DecisionSnapshot.config_values_used内のキー名。{name}_confidence等のフィールド名
# prefixと必ずしも一致しない(例: フィールドは"timing_confidence"だが
# config_values_used内のキーは"timing_score")。
_CONFIG_VALUES_KEY: dict[ScoreName, str] = {
    "historical_valuation": "historical_valuation",
    "timing": "timing_score",
    "earnings_surprise": "earnings_surprise",
    "earnings_trend": "earnings_trend",
}


@dataclass(frozen=True)
class DecisionPerformanceSummary:
    generated_at: dt.datetime
    horizon_business_days: int | None
    overall: MetricsBucket
    # MFE(Maximum Favorable Excursion)/MAE(Maximum Adverse Excursion)。
    # 既存EvaluationResult.max_gain_pct/max_drawdown_pctの平均値をそのまま使う。
    median_price_return_pct: float | None = None
    avg_mfe_pct: float | None = None
    avg_mae_pct: float | None = None
    by_decision_type: list[MetricsBucket] = field(default_factory=list)
    by_existing_action: list[MetricsBucket] = field(default_factory=list)
    by_model_version: list[MetricsBucket] = field(default_factory=list)


@dataclass(frozen=True)
class DecisionPerformanceSegment:
    """DecisionPerformance専用のセグメント別集計(コードレビュー対応:
    週次レビューと共有するMetricsBucketは肥大化させず、median/MFE/MAE等
    DecisionPerformance固有の指標をこちらにのみ持たせる)。"""

    dimension: str
    bucket_key: str
    sample_count: int
    conclusive_count: int
    success_rate_pct: float | None
    average_return_pct: float | None
    median_return_pct: float | None
    average_excess_return_pct: float | None
    median_excess_return_pct: float | None
    average_mfe_pct: float | None
    average_mae_pct: float | None


@dataclass(frozen=True)
class ScoreSegmentSummary:
    generated_at: dt.datetime
    score_name: ScoreName
    horizon_business_days: int
    by_category: list[DecisionPerformanceSegment]
    by_confidence: list[DecisionPerformanceSegment]
    by_coverage_tier: list[DecisionPerformanceSegment]
    # スコア個別のmodel_version別(例: timing_v3 vs timing_v4)。
    # DecisionSnapshot.model_version(Decision Enhancement Layer全体のバージョン、
    # 既存by_model_versionが対象)とは別物。
    by_model_version: list[DecisionPerformanceSegment]


@dataclass(frozen=True)
class DecisionPerformanceComparison:
    generated_at: dt.datetime
    horizon_business_days: int
    group_a: DecisionPerformanceSegment
    group_b: DecisionPerformanceSegment
    # 両群に同時に属したdecision_idの件数(コードレビュー対応: 呼び出し側が
    # 意図せず重複する条件を指定した場合に検知できるようにする)。
    overlap_count: int


def _build_decision_index(decisions: list[DecisionSnapshot]) -> dict[str, DecisionSnapshot]:
    """recommendation_id -> DecisionSnapshotのインデックスを構築する。

    モデル上は「1 Recommendation = 1 DecisionSnapshot」だが、不正なデータ投入等で
    同一recommendation_idに複数件存在した場合、list順に依存して結果が変わらないよう、
    該当recommendation_idはインデックスから完全に除外する(黙って最後の1件を
    採用しない)。除外対象はWARNINGログへ1件ずつ記録する。
    """
    index: dict[str, DecisionSnapshot] = {}
    duplicate_recommendation_ids: set[str] = set()
    for decision in decisions:
        recommendation_id = decision.recommendation_id
        if recommendation_id is None:
            continue
        if recommendation_id in index or recommendation_id in duplicate_recommendation_ids:
            duplicate_recommendation_ids.add(recommendation_id)
            index.pop(recommendation_id, None)
            continue
        index[recommendation_id] = decision
    for recommendation_id in sorted(duplicate_recommendation_ids):
        logger.warning(
            "%s recommendation_id=%s",
            DECISION_PERFORMANCE_DUPLICATE_SNAPSHOT_EVENT,
            recommendation_id,
        )
    return index


def _group_bucket(
    pairs: list[tuple[EvaluationResult, DecisionSnapshot]],
    key_fn: Callable[[DecisionSnapshot], str],
) -> list[MetricsBucket]:
    grouped: dict[str, list[EvaluationResult]] = {}
    for evaluation, decision in pairs:
        key = key_fn(decision)
        grouped.setdefault(key, []).append(evaluation)
    return [build_metrics_bucket(key, evals) for key, evals in sorted(grouped.items())]


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _build_segment(
    dimension: str, bucket_key: str, evaluations: list[EvaluationResult]
) -> DecisionPerformanceSegment:
    """既存build_metrics_bucket()を再利用してcount/conclusive_count/
    success_rate_pct/avg_price_return_pct/avg_excess_return_ptcを二重計算せず取得し、
    median/MFE/MAEのみDecisionPerformance側で追加算出する。"""
    bucket = build_metrics_bucket(bucket_key, evaluations)
    price_returns = [e.price_return_pct for e in evaluations]
    excess_returns = [e.excess_return_pct for e in evaluations if e.excess_return_pct is not None]
    mfe_values = [e.max_gain_pct for e in evaluations if e.max_gain_pct is not None]
    mae_values = [e.max_drawdown_pct for e in evaluations if e.max_drawdown_pct is not None]
    return DecisionPerformanceSegment(
        dimension=dimension,
        bucket_key=bucket_key,
        sample_count=bucket.count,
        conclusive_count=bucket.conclusive_count,
        success_rate_pct=bucket.success_rate_pct,
        average_return_pct=bucket.avg_price_return_pct,
        median_return_pct=_median(price_returns),
        average_excess_return_pct=bucket.avg_excess_return_pct,
        median_excess_return_pct=_median(excess_returns),
        average_mfe_pct=_average(mfe_values),
        average_mae_pct=_average(mae_values),
    )


def _group_segments(
    dimension: str,
    pairs: list[tuple[EvaluationResult, DecisionSnapshot]],
    key_fn: Callable[[DecisionSnapshot], str | None],
) -> list[DecisionPerformanceSegment]:
    """key_fnがNoneを返したペア(未評価・当時の閾値が復元できない等)は
    "UNKNOWN"等へ丸めず、そのままbucketから除外する。"""
    grouped: dict[str, list[EvaluationResult]] = {}
    for evaluation, decision in pairs:
        key = key_fn(decision)
        if key is None:
            continue
        grouped.setdefault(key, []).append(evaluation)
    return [_build_segment(dimension, key, evals) for key, evals in sorted(grouped.items())]


def _extract_category(decision: DecisionSnapshot, score_name: ScoreName) -> str | None:
    """DecisionSnapshotへ保存された当時のcategory文字列をそのまま返す。
    現在のCategory Enumで再検証・再解釈しない(コードレビュー対応: 将来Enum
    メンバー名が変わっても、過去の事実としてのcategoryを分析対象から除外
    しないため)。"""
    raw = getattr(decision, f"{score_name}_metrics").get("category")
    if not isinstance(raw, str) or not raw:
        return None
    return raw


def _extract_confidence(decision: DecisionSnapshot, score_name: ScoreName) -> str | None:
    """DecisionSnapshotのConfidenceLevelフィールドに保存済みの値をそのまま使う
    (現在のconfigとは無関係)。"""
    value = getattr(decision, f"{score_name}_confidence", None)
    return value.value if value is not None else None


def _extract_model_version(decision: DecisionSnapshot, score_name: ScoreName) -> str | None:
    raw = getattr(decision, f"{score_name}_metrics").get("model_version")
    return raw if isinstance(raw, str) and raw else None


def _is_valid_threshold_pair(medium: object, high: object) -> bool:
    if isinstance(medium, bool) or isinstance(high, bool):
        return False
    if not isinstance(medium, int | float) or not isinstance(high, int | float):
        return False
    if not (math.isfinite(medium) and math.isfinite(high)):
        return False
    return 0 <= medium < high <= 1


def _is_valid_coverage(value: object) -> bool:
    """coverage自身がNaN/Infinity/範囲外/bool/非数値でないことを検証する。
    コードレビュー対応: NaNは比較演算がすべてFalseになるため、検証せずに
    素通しすると`coverage >= high`/`coverage >= medium`がいずれもFalseとなり
    誤ってLOWへ分類されてしまう(異常値を勝手にLOW/HIGHへ丸めない)。"""
    if isinstance(value, bool):
        return False
    if not isinstance(value, int | float):
        return False
    numeric = float(value)
    return math.isfinite(numeric) and 0 <= numeric <= 1


def _extract_coverage_tier(decision: DecisionSnapshot, score_name: ScoreName) -> str | None:
    """当時実際に使われたcoverage閾値(DecisionSnapshot.config_values_used)から
    分類する。現在ロードしたAppConfigの閾値は一切使わない(過去判断を現在
    ロジックで再計算しない、というPhase Aの原則を厳守する)。coverage自身、
    config_values_usedのネスト値の型、閾値ペアのいずれかが壊れている
    (欠損・型不正・Mapping以外・NaN/Infinity・範囲外・medium>=high等)場合は
    推測せず、当該DecisionSnapshotをcoverage_tier分析からのみ除外する
    (category/confidence/model_version等の他の分析軸には影響しない。1件の
    異常な過去Snapshotでレポート全体を失敗させない)。"""
    coverage = getattr(decision, f"{score_name}_coverage", None)
    if coverage is None:
        return None
    if not _is_valid_coverage(coverage):
        logger.warning(
            "%s score=%s decision_id=%s coverage=%r",
            DECISION_PERFORMANCE_INVALID_COVERAGE_THRESHOLD_EVENT,
            score_name,
            decision.decision_id,
            coverage,
        )
        return None
    score_values = decision.config_values_used.get(_CONFIG_VALUES_KEY[score_name])
    if not isinstance(score_values, Mapping):
        logger.warning(
            "%s score=%s decision_id=%s config_values=%r",
            DECISION_PERFORMANCE_INVALID_COVERAGE_THRESHOLD_EVENT,
            score_name,
            decision.decision_id,
            score_values,
        )
        return None
    high = score_values.get("coverage_high_threshold")
    medium = score_values.get("coverage_medium_threshold")
    if not _is_valid_threshold_pair(medium, high):
        logger.warning(
            "%s score=%s decision_id=%s medium=%r high=%r",
            DECISION_PERFORMANCE_INVALID_COVERAGE_THRESHOLD_EVENT,
            score_name,
            decision.decision_id,
            medium,
            high,
        )
        return None
    if coverage >= high:
        return "HIGH"
    if coverage >= medium:
        return "MEDIUM"
    return "LOW"


class DecisionPerformanceService:
    def __init__(
        self,
        evaluation_repository: EvaluationResultRepository | None = None,
        decision_repository: DecisionSnapshotRepository | None = None,
        config: AppConfig | None = None,
    ) -> None:
        self._evaluations = evaluation_repository or EvaluationResultRepository()
        self._decisions = decision_repository or DecisionSnapshotRepository()
        self._config = config or load_config()

    def _joined_pairs(
        self, horizon_business_days: int | None
    ) -> list[tuple[EvaluationResult, DecisionSnapshot]]:
        """Phase A成績評価の対象ホライズンのみへ絞り込み(1営業日共通
        チェックポイント・7暦日評価等は対象外)、重複安全なDecisionSnapshot
        インデックスとrecommendation_idでJOINする。summarize()/
        summarize_score_segments()/compare_segments()が共通して使う
        (horizon許可リスト・JOINロジックの二重実装を避ける)。"""
        phase_a_horizons = set(self._config.decision_evaluation.horizons_business_days)
        evaluations = [
            e
            for e in self._evaluations.list_all()
            if e.horizon_business_days is not None and e.horizon_business_days in phase_a_horizons
        ]
        if horizon_business_days is not None:
            evaluations = [
                e for e in evaluations if e.horizon_business_days == horizon_business_days
            ]

        decisions_by_recommendation_id = _build_decision_index(self._decisions.list_all())
        pairs: list[tuple[EvaluationResult, DecisionSnapshot]] = []
        for evaluation in evaluations:
            decision = decisions_by_recommendation_id.get(evaluation.recommendation_id)
            if decision is not None:
                pairs.append((evaluation, decision))
        return pairs

    def _require_phase_a_horizon(self, horizon_business_days: int) -> None:
        allowed = set(self._config.decision_evaluation.horizons_business_days)
        if horizon_business_days not in allowed:
            raise ValueError(
                f"horizon_business_daysはPhase A許可リスト({sorted(allowed)})の"
                "いずれかを指定してください"
            )

    def summarize(
        self, horizon_business_days: int | None = None, now: dt.datetime | None = None
    ) -> DecisionPerformanceSummary:
        pairs = self._joined_pairs(horizon_business_days)

        price_returns = [e.price_return_pct for e, _ in pairs]
        mfe_values = [e.max_gain_pct for e, _ in pairs if e.max_gain_pct is not None]
        mae_values = [e.max_drawdown_pct for e, _ in pairs if e.max_drawdown_pct is not None]

        return DecisionPerformanceSummary(
            generated_at=now or dt.datetime.now(dt.UTC),
            horizon_business_days=horizon_business_days,
            overall=build_metrics_bucket("overall", [e for e, _ in pairs]),
            median_price_return_pct=_median(price_returns),
            avg_mfe_pct=_average(mfe_values),
            avg_mae_pct=_average(mae_values),
            by_decision_type=_group_bucket(pairs, lambda d: d.decision_type.value),
            by_existing_action=_group_bucket(
                pairs, lambda d: d.existing_action.value if d.existing_action else "UNKNOWN"
            ),
            by_model_version=_group_bucket(pairs, lambda d: d.model_version),
        )

    def summarize_score_segments(
        self,
        score_name: ScoreName,
        horizon_business_days: int,
        now: dt.datetime | None = None,
    ) -> ScoreSegmentSummary:
        """4つのShadow Scoreをcategory/confidence/coverage_tier/個別
        model_version別に分析する。horizon_business_daysは必須(異なる評価
        期間のOutcomeを1つのbucketへ混在させないため)。"""
        if score_name not in _SCORE_NAMES:
            raise ValueError(f"不明なscore_nameです: {score_name!r}")
        self._require_phase_a_horizon(horizon_business_days)

        pairs = self._joined_pairs(horizon_business_days)
        return ScoreSegmentSummary(
            generated_at=now or dt.datetime.now(dt.UTC),
            score_name=score_name,
            horizon_business_days=horizon_business_days,
            by_category=_group_segments(
                "category", pairs, lambda d: _extract_category(d, score_name)
            ),
            by_confidence=_group_segments(
                "confidence", pairs, lambda d: _extract_confidence(d, score_name)
            ),
            by_coverage_tier=_group_segments(
                "coverage_tier", pairs, lambda d: _extract_coverage_tier(d, score_name)
            ),
            by_model_version=_group_segments(
                "model_version", pairs, lambda d: _extract_model_version(d, score_name)
            ),
        )

    def compare_segments(
        self,
        label_a: str,
        predicate_a: Callable[[DecisionSnapshot], bool],
        label_b: str,
        predicate_b: Callable[[DecisionSnapshot], bool],
        horizon_business_days: int,
        now: dt.datetime | None = None,
    ) -> DecisionPerformanceComparison:
        """任意の2つの母集団(predicateで指定)の成績を比較する。常に2グループ
        のみを返し、どこにも永続化しない(全組み合わせの自動生成は行わない)。"""
        self._require_phase_a_horizon(horizon_business_days)
        pairs = self._joined_pairs(horizon_business_days)
        pairs_a = [(e, d) for e, d in pairs if predicate_a(d)]
        pairs_b = [(e, d) for e, d in pairs if predicate_b(d)]
        overlap_ids = {d.decision_id for _, d in pairs_a} & {d.decision_id for _, d in pairs_b}
        return DecisionPerformanceComparison(
            generated_at=now or dt.datetime.now(dt.UTC),
            horizon_business_days=horizon_business_days,
            group_a=_build_segment("comparison", label_a, [e for e, _ in pairs_a]),
            group_b=_build_segment("comparison", label_b, [e for e, _ in pairs_b]),
            overlap_count=len(overlap_ids),
        )


def score_predicate(
    score_name: ScoreName, minimum: float | None = None, maximum: float | None = None
) -> Callable[[DecisionSnapshot], bool]:
    """DecisionSnapshot.{name}_scoreという型付きフィールドを直接読む比較用
    predicateビルダー。カテゴリ境界値はconfigの`category_thresholds`を
    そのまま渡せば良く、新規ハードコードを必要としない。"""

    def _predicate(decision: DecisionSnapshot) -> bool:
        value = getattr(decision, f"{score_name}_score", None)
        if value is None:
            return False
        if minimum is not None and value < minimum:
            return False
        return maximum is None or value <= maximum

    return _predicate
