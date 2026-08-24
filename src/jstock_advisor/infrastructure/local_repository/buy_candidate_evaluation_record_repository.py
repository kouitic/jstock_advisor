"""BuyCandidateEvaluationRecordのローカルリポジトリ(買い候補サマリー表示改修2026-08)。

将来のLINE詳細理由照会機能に向けた参照用ストアであり、既存のRecommendation/
DecisionSnapshot/NotificationLog/AuditLogTableの代替ではない。無期限に増加させない
ため既定90日のTTLを設定する(config.notification.buy_candidatesで調整可能)。
"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.buy_candidate_evaluation_record import (
    BuyCandidateEvaluationRecord,
)
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store

_TABLE_FILE_NAME = "buy_candidate_evaluation_records.json"
DEFAULT_TTL_SECONDS = 90 * 24 * 60 * 60

# LINE UI第二弾「対象確認」機能(2026-08)向け。batch_idをHASHキーとするGSI
# (infra/template.yaml参照)。evaluation_idはbatch_id:stock_codeの複合文字列
# でしかなく、batch_id単体でのQueryができないため新設した。
BATCH_ID_INDEX_NAME = "batch_id-index"


class BuyCandidateEvaluationRecordRepository:
    def __init__(
        self, store_dir: Path | None = None, ttl_seconds: int | None = DEFAULT_TTL_SECONDS
    ) -> None:
        self._store: CollectionStore[BuyCandidateEvaluationRecord] = build_collection_store(
            BuyCandidateEvaluationRecord,
            _TABLE_FILE_NAME,
            "evaluation_id",
            store_dir,
            ttl_seconds=ttl_seconds,
        )

    def get(self, evaluation_id: str) -> BuyCandidateEvaluationRecord | None:
        return self._store.get(evaluation_id)

    def list_by_stock(self, stock_code: str) -> list[BuyCandidateEvaluationRecord]:
        items = self._store.find(lambda r: r.stock_code == stock_code)
        return sorted(items, key=lambda r: r.evaluated_at)

    def list_by_batch(self, batch_id: str) -> list[BuyCandidateEvaluationRecord]:
        """指定batch_idの全評価レコードを取得する(GSI Query、対象確認機能向け)。

        stock_code昇順で返す(呼び出し側でのランキング等の並べ替えを妨げない
        安定した既定順序)。
        """
        items = self._store.query_by_index(BATCH_ID_INDEX_NAME, "batch_id", batch_id)
        return sorted(items, key=lambda r: r.stock_code)

    def upsert(self, record: BuyCandidateEvaluationRecord) -> None:
        # batch_idをトップレベル属性としても書き込み、GSI(batch_id-index)で
        # Queryできるようにする(list_by_batch参照)。
        self._store.upsert_with_index_attributes(record, {"batch_id": record.batch_id})
