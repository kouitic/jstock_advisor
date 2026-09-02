"""実際の売買記録のローカルリポジトリ(要求仕様27節)。"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.transaction import SkippedRecommendation, Transaction
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store


class TransactionRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[Transaction] = build_collection_store(
            Transaction, "transactions.json", "transaction_id", store_dir
        )

    def list_all(self) -> list[Transaction]:
        return self._store.list_all()

    def list_by_stock(self, stock_code: str) -> list[Transaction]:
        items = self._store.find(lambda t: t.stock_code == stock_code)
        return sorted(items, key=lambda t: t.execution_date)

    def list_by_recommendation(self, recommendation_id: str) -> list[Transaction]:
        return self._store.find(lambda t: t.recommendation_id == recommendation_id)

    def get(self, transaction_id: str) -> Transaction | None:
        return self._store.get(transaction_id)

    def save(self, transaction: Transaction) -> None:
        self._store.upsert(transaction)

    def save_if_absent(self, transaction: Transaction) -> bool:
        """transaction_idが未登録なら保存してTrue、既に存在すればFalse(Issue #61 Phase B3)。

        CSV取込の冪等性を**永続データそのもの**で保証するために使う。
        DynamoDB実装は`attribute_not_exists(transaction_id)`の条件付き書き込みで
        原子的にこれを保証するため、呼び出し側でexists()→save()という
        check-then-actを書いてはならない(TOCTOU raceが残るため)。

        既存Transactionの内容は**上書きしない**。同じtransaction_idが既にある
        場合は「取込済み」とみなし、何も書かずにFalseを返す。
        """
        return self._store.insert_if_absent(transaction)


class SkippedRecommendationRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[SkippedRecommendation] = build_collection_store(
            SkippedRecommendation, "skipped_recommendations.json", "recommendation_id", store_dir
        )

    def list_all(self) -> list[SkippedRecommendation]:
        return self._store.list_all()

    def get(self, recommendation_id: str) -> SkippedRecommendation | None:
        return self._store.get(recommendation_id)

    def save(self, skipped: SkippedRecommendation) -> None:
        self._store.upsert(skipped)
