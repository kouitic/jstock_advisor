"""監査ログのローカルリポジトリ(要求仕様13節・21節)。"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.audit import AuditLogEntry
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store
from jstock_advisor.infrastructure.record_failure_policy import (
    ItemIdDisclosure,
    RecordFailurePolicy,
)


class AuditLogRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        # Issue #63 PR-3b: 監査記録の1件がdecodeできなくても、他の記録の閲覧・
        # レビューを止める理由がない(LENIENT)。**skipした件数はWARNINGへ必ず出る**
        # ため黙って無視することにはならない(#245のO-4が週次棚卸で数える観測点)。
        # ★ ローカルJSON実装は_load()で全件decodeしてから読み書きするため、
        #   STRICTのままだと壊れた1件が「監査記録を書くだけ」の経路も止める。
        # ★ Productionの挙動は変わらない。Lambdaが使うDynamoDB実装は
        #   put_item(条件付きを含む)でdecodeを通らず、Lambdaからaudit_logを
        #   読む経路も0件である(#245 Phase Aの実測)。
        # item_idはPLAIN。audit_idの生成箇所を全件確認した(5経路)。
        #   uuid.uuid4()                                    audit_service.record()
        #   f"csv_holding_import_row:{import_id}:{row}"     import_idはCSVのsha256
        #   f"<用途>:{run_started_at}"                       evaluation_run_audit
        #   f"watchlist_removal:{stock_code}:{removed_at}"  銘柄コードを含む
        #   f"watchlist_batch_audit:{batch_id}"
        # いずれも**所有者名・氏名を含まない**(#135 Phase Aが実測した
        # `<所有者>#<銘柄コード>`形式の6 collectionと重ならない)。銘柄コードのみを
        # 含む形はrecord_failure_policy.pyが`watchlist`(stock_code)をPLAINの例として
        # 既に認めている範囲であり、新しい判断を持ち込んでいない。
        self._store: CollectionStore[AuditLogEntry] = build_collection_store(
            AuditLogEntry,
            "audit_log.json",
            "audit_id",
            store_dir,
            failure_policy=RecordFailurePolicy.LENIENT,
            item_id_disclosure=ItemIdDisclosure.PLAIN,
        )

    def list_all(self) -> list[AuditLogEntry]:
        return self._store.list_all()

    def list_by_stock(self, stock_code: str) -> list[AuditLogEntry]:
        items = self._store.find(lambda e: e.stock_code == stock_code)
        return sorted(items, key=lambda e: e.timestamp)

    def list_by_decision_type(self, decision_type: str) -> list[AuditLogEntry]:
        items = self._store.find(lambda e: e.decision_type == decision_type)
        return sorted(items, key=lambda e: e.timestamp)

    def get(self, audit_id: str) -> AuditLogEntry | None:
        return self._store.get(audit_id)

    def save(self, entry: AuditLogEntry) -> None:
        self._store.upsert(entry)

    def delete(self, audit_id: str) -> bool:
        """指定audit_idの記録を削除する(Issue #61 Phase B1)。

        **監査記録一般の削除手段として使わないこと。** 用途は
        `csv_import_ledger`が行コミットのclaimを獲得したあと、データ適用に
        失敗した場合の補償(claimの解放)に限定する。解放しないと、実データが
        未適用のまま「claim済み」が残り再実行しても適用されなくなるため。
        """
        return self._store.delete(audit_id)

    def save_if_absent(self, entry: AuditLogEntry) -> bool:
        """運用ハードニング第3弾3節: 既にaudit_idが存在すればFalse(何もしない)、
        無ければ保存してTrue(冪等な新規記録専用)。決定的なaudit_idと組み合わせて
        呼び出し側の重複記録防止に使う(CollectionStore.insert_if_absentの
        条件付き書き込みで原子的に保証される)。
        """
        return self._store.insert_if_absent(entry)
