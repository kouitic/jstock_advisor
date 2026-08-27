"""推奨記録のローカルリポジトリ(要求仕様26節)。Recommendationは不変スナップショットのため上書きしない。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store

PRODUCTION_FILE_NAME = "recommendations.json"
# 通知検証モード機能(2026-08追加)。BUY候補のfan-out(_process_single_candidate→
# _finalize_batch)は複数のLambda呼び出しをまたいでRecommendationを実際に
# 保存・再取得する必要があるため単純なno-op化ができない(functional_spec.md
# §通知検証モード参照)。本番RecommendationsTableを汚さないよう、VALIDATION専用の
# 使い捨てテーブルへ向ける。
VALIDATION_FILE_NAME = "validation_recommendations.json"
_VALIDATION_TTL_SECONDS = 2 * 60 * 60  # 異常終了時の安全網(正常完了時は明示的に削除する)


class RecommendationRepository:
    def __init__(
        self,
        store_dir: Path | None = None,
        file_name: str = PRODUCTION_FILE_NAME,
        ttl_seconds: int | None = None,
    ) -> None:
        self._store: CollectionStore[Recommendation] = build_collection_store(
            Recommendation, file_name, "recommendation_id", store_dir, ttl_seconds=ttl_seconds
        )
        self._file_name = file_name

    @property
    def file_name(self) -> str:
        return self._file_name

    @classmethod
    def for_execution_context(
        cls, execution_context: ExecutionContext, store_dir: Path | None = None
    ) -> RecommendationRepository:
        """NORMALは必ず本番テーブル、VALIDATIONは必ず検証用テーブルへ向ける唯一の生成経路。

        呼び出し側はこのファクトリだけを使い、RecommendationRepository()を
        直接VALIDATION分岐で呼ばない(生成経路を1本化し切替漏れを構造的に防ぐ)。
        """
        if execution_context.is_validation:
            return cls(
                store_dir=store_dir,
                file_name=VALIDATION_FILE_NAME,
                ttl_seconds=_VALIDATION_TTL_SECONDS,
            )
        return cls(store_dir=store_dir, file_name=PRODUCTION_FILE_NAME, ttl_seconds=None)

    def delete(self, recommendation_id: str) -> bool:
        """通知検証モード専用の後始末(functional_spec.md参照)。既存のsave()の
        上書き禁止は「不変スナップショット」の原則(内容を書き換えない)を指すもので
        あり、検証専用の使い捨てレコードを完全に削除するこの操作とは矛盾しない。
        """
        return self._store.delete(recommendation_id)

    def list_all(self) -> list[Recommendation]:
        return self._store.list_all()

    def list_by_stock(self, stock_code: str) -> list[Recommendation]:
        items = self._store.find(lambda r: r.stock_code == stock_code)
        return sorted(items, key=lambda r: r.recommended_at)

    def get(self, recommendation_id: str) -> Recommendation | None:
        return self._store.get(recommendation_id)

    def get_many(self, recommendation_ids: Iterable[str]) -> dict[str, Recommendation]:
        """複数のrecommendation_idを一括取得する(対象確認機能2026-08、N+1回避)。

        戻り値には実際に存在したIDのみを含める(get()と同じ意味)。DynamoDB
        実装はBatchGetItemで一括取得するため、1件ずつGetItemを呼ぶ実装には
        ならない(infrastructure/aws/dynamodb_store.py::get_many()参照)。
        """
        return self._store.get_many(recommendation_ids)

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

    def get_latest_by_type(
        self, recommendation_type: RecommendationType
    ) -> Recommendation | None:
        """振り返り機能改修: 「現在有効なrule_version」の解決(resolve_current_rule_version)で、
        正式なRuleVersion.ACTIVE版が無い場合のfallbackとして使う。当該
        RecommendationTypeのうちrecommended_atが最新の1件を返す(無ければNone)。
        """
        items = self._store.find(lambda r: r.recommendation_type == recommendation_type)
        return max(items, key=lambda r: r.recommended_at) if items else None
