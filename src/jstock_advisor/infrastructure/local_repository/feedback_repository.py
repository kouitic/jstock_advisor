"""ユーザー定性フィードバックのローカルリポジトリ(要求仕様47節)。"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.feedback import UserFeedback
from jstock_advisor.infrastructure.local_repository.json_store import JsonCollectionStore


class UserFeedbackRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: JsonCollectionStore[UserFeedback] = JsonCollectionStore(
            UserFeedback, "user_feedback.json", "feedback_id", store_dir
        )

    def list_all(self) -> list[UserFeedback]:
        return self._store.list_all()

    def list_by_recommendation(self, recommendation_id: str) -> list[UserFeedback]:
        return self._store.find(lambda f: f.recommendation_id == recommendation_id)

    def get(self, feedback_id: str) -> UserFeedback | None:
        return self._store.get(feedback_id)

    def save(self, feedback: UserFeedback) -> None:
        self._store.upsert(feedback)
