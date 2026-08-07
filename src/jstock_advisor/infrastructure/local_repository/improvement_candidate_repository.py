"""改善候補(ImprovementCandidate)のローカルリポジトリ(振り返り機能改修)。

週次スナップショットとして毎週保存する(Candidateの有無に関わらない実績の正は
WeeklyReviewMetricsが担う。ImprovementCandidateは検出された候補のみ)。
"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.improvement import ImprovementCandidate
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store


class ImprovementCandidateRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[ImprovementCandidate] = build_collection_store(
            ImprovementCandidate, "improvement_candidates.json", "candidate_id", store_dir
        )

    def list_all(self) -> list[ImprovementCandidate]:
        return self._store.list_all()

    def get(self, candidate_id: str) -> ImprovementCandidate | None:
        return self._store.get(candidate_id)

    def save(self, candidate: ImprovementCandidate) -> None:
        self._store.upsert(candidate)

    def list_by_candidate_key(self, candidate_key: str) -> list[ImprovementCandidate]:
        items = self._store.find(lambda c: c.candidate_key == candidate_key)
        return sorted(items, key=lambda c: c.review_week, reverse=True)
