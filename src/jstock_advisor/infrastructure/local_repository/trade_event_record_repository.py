"""TradeEventRecordのローカルリポジトリ(Issue #71 F-C11 Phase 1)。

HoldingsSnapshotRepositoryと同様にNORMAL/VALIDATIONで物理テーブルを分離する
(通知検証モードの「本番の永続状態・通常運用へ影響させない」という既存方針)。
"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.trade_event_record import TradeEventRecord
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store

PRODUCTION_FILE_NAME = "trade_event_records.json"
VALIDATION_FILE_NAME = "validation_trade_event_records.json"
_VALIDATION_TTL_SECONDS = 2 * 60 * 60

# sparse GSI(Phase 2のconsumption stepがquery_by_index()で使う。Phase 1では
# 未消費eventの一覧取得は行わないため、本モジュールはHASHキー名の宣言のみ持つ)。
PENDING_INDEX_NAME = "pending-marker-index"


class TradeEventRecordRepository:
    def __init__(
        self,
        store_dir: Path | None = None,
        file_name: str = PRODUCTION_FILE_NAME,
        ttl_seconds: int | None = None,
    ) -> None:
        self._store: CollectionStore[TradeEventRecord] = build_collection_store(
            TradeEventRecord, file_name, "event_id", store_dir, ttl_seconds=ttl_seconds
        )

    @classmethod
    def for_execution_context(
        cls, execution_context: ExecutionContext, store_dir: Path | None = None
    ) -> TradeEventRecordRepository:
        if execution_context.is_validation:
            return cls(
                store_dir=store_dir,
                file_name=VALIDATION_FILE_NAME,
                ttl_seconds=_VALIDATION_TTL_SECONDS,
            )
        return cls(store_dir=store_dir, file_name=PRODUCTION_FILE_NAME, ttl_seconds=None)

    def get(self, event_id: str) -> TradeEventRecord | None:
        return self._store.get(event_id)

    def create_pending(self, record: TradeEventRecord) -> bool:
        """新規イベントを冪等に記録し、sparse GSI用のpending_marker属性を
        書き込む(Issue #71 F-C11設計、書込み順序2ステップ)。

        1. insert_if_absent()で原子的な新規追加のみを許可する。既に存在する
           場合(Falseを返す)は、その既存項目には一切触れない
           (★ 消費済み[consumed_at設定・pending_marker削除済み]の項目を
           リトライで誤って未消費状態へ巻き戻さないため)。
        2. 新規追加できた場合のみ、pending_marker属性をGSI用に追加書き込みする。

        戻り値は「新規に作成したか」(insert_if_absent()の結果をそのまま返す)。
        呼び出し側はこの戻り値をcontrol flowの分岐には使わない設計
        (Falseでも後続のsnapshot更新は必ず実行すること)。

        ★ 既知の限定的なギャップ: ステップ1成功後・ステップ2実行前にクラッシュ
          すると、レコードは存在するがpending_marker未設定のまま残る
          (Phase 1ではconsumption未実装のため実害なし。Phase 2でconsumption
          stepを実装する際、この状態を検出・回収する設計が必要)。
        """
        created = self._store.insert_if_absent(record)
        if created and record.pending_marker is not None:
            self._store.upsert_with_index_attributes(
                record,
                {
                    "pending_marker": record.pending_marker,
                    "detected_at": record.detected_at.isoformat(),
                },
            )
        return created
