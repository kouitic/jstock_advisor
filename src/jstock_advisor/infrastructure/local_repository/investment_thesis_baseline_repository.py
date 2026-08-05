"""InvestmentThesisBaselineのローカルリポジトリ(実装プラン3節)。不変スナップショット。

「現在有効なbaseline」の判定はこのリポジトリではなくInvestmentThesisBaselinePointer
(infrastructure/aws/baseline_pointer.py)が担う。
"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.holding_decision import InvestmentThesisBaseline
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store


class InvestmentThesisBaselineRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[InvestmentThesisBaseline] = build_collection_store(
            InvestmentThesisBaseline, "investment_thesis_baselines.json", "baseline_id", store_dir
        )

    def get(self, baseline_id: str) -> InvestmentThesisBaseline | None:
        return self._store.get(baseline_id)

    def save(self, baseline: InvestmentThesisBaseline) -> None:
        if self._store.get(baseline.baseline_id) is not None:
            raise ValueError(
                f"baseline_id={baseline.baseline_id} は既に保存済みです"
                "(不変スナップショットのため上書きできません)"
            )
        self._store.upsert(baseline)

    def save_if_absent(self, baseline: InvestmentThesisBaseline) -> bool:
        return self._store.insert_if_absent(baseline)

    def list_by_holding(self, holding_id: str) -> list[InvestmentThesisBaseline]:
        items = self._store.find(lambda b: b.holding_id == holding_id)
        return sorted(items, key=lambda b: b.version)

    def list_all(self) -> list[InvestmentThesisBaseline]:
        return self._store.list_all()
