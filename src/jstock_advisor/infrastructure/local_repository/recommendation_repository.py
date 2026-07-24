"""推奨記録のローカルリポジトリ(要求仕様26節)。Recommendationは不変スナップショットのため上書きしない。"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.json_store import JsonCollectionStore


class RecommendationRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: JsonCollectionStore[Recommendation] = JsonCollectionStore(
            Recommendation, "recommendations.json", "recommendation_id", store_dir
        )

    def list_all(self) -> list[Recommendation]:
        return self._store.list_all()

    def list_by_stock(self, stock_code: str) -> list[Recommendation]:
        items = self._store.find(lambda r: r.stock_code == stock_code)
        return sorted(items, key=lambda r: r.recommended_at)

    def get(self, recommendation_id: str) -> Recommendation | None:
        return self._store.get(recommendation_id)

    def save(self, recommendation: Recommendation) -> None:
        if self._store.get(recommendation.recommendation_id) is not None:
            raise ValueError(
                f"recommendation_id={recommendation.recommendation_id} は既に保存済みです"
                "(推奨スナップショットは変更不可のため上書きできません)"
            )
        self._store.upsert(recommendation)

    def latest_by_stock(self, stock_code: str) -> Recommendation | None:
        items = self.list_by_stock(stock_code)
        return items[-1] if items else None
