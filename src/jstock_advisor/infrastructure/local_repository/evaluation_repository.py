"""推奨の定点評価結果のローカルリポジトリ(要求仕様29〜36節)。"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store


class EvaluationResultRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[EvaluationResult] = build_collection_store(
            EvaluationResult, "evaluation_results.json", "evaluation_id", store_dir
        )

    def list_all(self) -> list[EvaluationResult]:
        return self._store.list_all()

    def list_by_recommendation(self, recommendation_id: str) -> list[EvaluationResult]:
        return self._store.find(lambda e: e.recommendation_id == recommendation_id)

    def get(self, evaluation_id: str) -> EvaluationResult | None:
        return self._store.get(evaluation_id)

    def exists_for_horizon(self, recommendation_id: str, horizon_business_days: int) -> bool:
        return any(
            e.recommendation_id == recommendation_id
            and e.horizon_business_days == horizon_business_days
            for e in self._store.list_all()
        )

    def save(self, evaluation: EvaluationResult) -> None:
        self._store.upsert(evaluation)
