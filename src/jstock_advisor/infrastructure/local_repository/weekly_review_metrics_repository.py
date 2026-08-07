"""週次改善レビューの実績(WeeklyReviewMetrics)のローカルリポジトリ(振り返り機能改修)。

Candidateの有無に関わらず毎週保存する履歴の正であり、前週比較・
consecutive_bad_weeksの算出のため、RecommendationType×rule_version×segment_key
単位で過去複数週分を取得できる必要がある。
"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.entities.improvement import WeeklyReviewMetrics
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store


class WeeklyReviewMetricsRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[WeeklyReviewMetrics] = build_collection_store(
            WeeklyReviewMetrics, "weekly_review_metrics.json", "metrics_id", store_dir
        )

    def list_all(self) -> list[WeeklyReviewMetrics]:
        return self._store.list_all()

    def get(self, metrics_id: str) -> WeeklyReviewMetrics | None:
        return self._store.get(metrics_id)

    def save(self, metrics: WeeklyReviewMetrics) -> None:
        self._store.upsert(metrics)

    def list_by_type_version_segment(
        self,
        recommendation_type: RecommendationType,
        rule_version: str,
        segment_key: str | None,
    ) -> list[WeeklyReviewMetrics]:
        """同一RecommendationType×rule_version×segment_keyの行を、review_week
        の新しい順で返す(前週比較・consecutive_bad_weeks算出用)。異なる
        rule_versionの行は含まれない(振り返り機能改修: 過去ルールと現行ルールの
        成績を混在させないため)。
        """
        items = self._store.find(
            lambda m: m.recommendation_type == recommendation_type
            and m.rule_version == rule_version
            and m.segment_key == segment_key
        )
        return sorted(items, key=lambda m: m.review_week, reverse=True)
