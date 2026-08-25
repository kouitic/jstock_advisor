"""HoldingEvaluationRecordのローカルリポジトリ(Phase 2-B「銘柄分析」向け、2026-08)。

BuyCandidateEvaluationRecordRepositoryと対称的な構造。将来のLINE詳細理由照会
機能向けの参照用ストアであり、既存のRecommendation/DecisionSnapshot/
HoldingDecisionResult/AuditLogの代替ではない。無期限に増加させないため既定90日の
TTLを設定する。
"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.holding_evaluation_record import HoldingEvaluationRecord
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store

_TABLE_FILE_NAME = "holding_evaluation_records.json"
DEFAULT_TTL_SECONDS = 90 * 24 * 60 * 60

# owner#stock_code(Holding.holding_id)をHASHキーとするGSI(infra/template.yaml
# 参照)。stock_code-index+FilterExpressionによるowner絞り込みは行わない
# (Phase 2-B文章仕様レビュー指摘: 非効率なscan+filter相当になるため、holding_id
# 自体をGSIのHASHキーとして直接Queryする)。
HOLDING_ID_INDEX_NAME = "holding_id-index"


class HoldingEvaluationRecordRepository:
    def __init__(
        self, store_dir: Path | None = None, ttl_seconds: int | None = DEFAULT_TTL_SECONDS
    ) -> None:
        self._store: CollectionStore[HoldingEvaluationRecord] = build_collection_store(
            HoldingEvaluationRecord,
            _TABLE_FILE_NAME,
            "holding_evaluation_id",
            store_dir,
            ttl_seconds=ttl_seconds,
        )

    def save(self, record: HoldingEvaluationRecord) -> None:
        # holding_id(HASH)・evaluated_at(RANGE)をトップレベル属性としても書き込み、
        # GSI(holding_id-index)でQueryできるようにする(get_latest_by_holding_id参照)。
        self._store.upsert_with_index_attributes(
            record,
            {"holding_id": record.holding_id, "evaluated_at": record.evaluated_at.isoformat()},
        )

    def get_latest_by_holding_id(self, holding_id: str) -> HoldingEvaluationRecord | None:
        """指定holding_id(owner#stock_code)の直近1件を取得する(GSI Query)。"""
        items = self._store.query_by_index(HOLDING_ID_INDEX_NAME, "holding_id", holding_id)
        if not items:
            return None
        return max(items, key=lambda r: r.evaluated_at)
