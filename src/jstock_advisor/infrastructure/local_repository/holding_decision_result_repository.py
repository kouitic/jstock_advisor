"""HoldingDecisionResultのローカルリポジトリ(実装プラン18節)。不変スナップショット。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from jstock_advisor.domain.entities.holding_decision import HoldingDecisionResult
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store


class HoldingDecisionResultRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[HoldingDecisionResult] = build_collection_store(
            HoldingDecisionResult,
            "holding_decision_results.json",
            "holding_decision_result_id",
            store_dir,
        )

    def get(self, holding_decision_result_id: str) -> HoldingDecisionResult | None:
        return self._store.get(holding_decision_result_id)

    def save(self, result: HoldingDecisionResult) -> None:
        if self._store.get(result.holding_decision_result_id) is not None:
            raise ValueError(
                f"holding_decision_result_id={result.holding_decision_result_id} "
                "は既に保存済みです(不変スナップショットのため上書きできません)"
            )
        self._store.upsert(result)

    def list_by_holding(self, holding_id: str) -> list[HoldingDecisionResult]:
        items = self._store.find(lambda r: r.holding_id == holding_id)
        return sorted(items, key=lambda r: r.evaluated_at)

    def list_by_stock(self, stock_code: str) -> list[HoldingDecisionResult]:
        items = self._store.find(lambda r: r.stock_code == stock_code)
        return sorted(items, key=lambda r: r.evaluated_at)

    def latest_by_holding(self, holding_id: str) -> HoldingDecisionResult | None:
        items = self.list_by_holding(holding_id)
        return items[-1] if items else None

    def get_by_recommendation_id(self, recommendation_id: str) -> HoldingDecisionResult | None:
        items = self._store.find(lambda r: r.recommendation_id == recommendation_id)
        return items[0] if items else None

    def list_between(
        self, start: dt.datetime, end: dt.datetime
    ) -> list[HoldingDecisionResult]:
        items = self._store.find(lambda r: start <= r.evaluated_at <= end)
        return sorted(items, key=lambda r: r.evaluated_at)

    def list_all(self) -> list[HoldingDecisionResult]:
        return self._store.list_all()
