"""ルールバージョン・改善提案のローカルリポジトリ(要求仕様41・43節)。"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.rule_version import RuleProposal, RuleVersion
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store


class RuleVersionRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[RuleVersion] = build_collection_store(
            RuleVersion, "rule_versions.json", "rule_version", store_dir
        )

    def list_all(self) -> list[RuleVersion]:
        return self._store.list_all()

    def get(self, rule_version: str) -> RuleVersion | None:
        return self._store.get(rule_version)

    def save(self, version: RuleVersion) -> None:
        self._store.upsert(version)


class RuleProposalRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[RuleProposal] = build_collection_store(
            RuleProposal, "rule_proposals.json", "proposal_id", store_dir
        )

    def list_all(self) -> list[RuleProposal]:
        return self._store.list_all()

    def get(self, proposal_id: str) -> RuleProposal | None:
        return self._store.get(proposal_id)

    def save(self, proposal: RuleProposal) -> None:
        self._store.upsert(proposal)
