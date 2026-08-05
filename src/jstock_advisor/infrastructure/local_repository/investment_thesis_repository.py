"""InvestmentThesis(個別購入理由)のローカルリポジトリ(実装プラン18節)。"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.holding_decision import InvestmentThesis
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store


class InvestmentThesisRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[InvestmentThesis] = build_collection_store(
            InvestmentThesis, "investment_theses.json", "investment_thesis_id", store_dir
        )

    def get(self, investment_thesis_id: str) -> InvestmentThesis | None:
        return self._store.get(investment_thesis_id)

    def get_by_holding(self, holding_id: str) -> InvestmentThesis | None:
        items = self._store.find(lambda t: t.holding_id == holding_id)
        return items[0] if items else None

    def save(self, thesis: InvestmentThesis) -> None:
        self._store.upsert(thesis)

    def list_all(self) -> list[InvestmentThesis]:
        return self._store.list_all()
