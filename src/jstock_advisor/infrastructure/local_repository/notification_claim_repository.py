"""LINE通知dedup claim(Issue #17)のリポジトリ。

NORMAL(SEND)実行のみが読み書きする。VALIDATION/DRY_RUNはclaim機構自体を
使用しないため、このリポジトリへ到達しない(LineNotificationService側で保証)。
"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.notification_claim import NotificationClaim
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store

# TTL(cleanup専用、30日)。DynamoDB Native TTLの削除は最大48時間程度遅延しうる
# ため、送信可否・stale判定には絶対に使わない(claimed_at比較のみで判定する)。
# 値の根拠: claimはresend判定の正本ではなく「送信決定の一意化」レコードであり、
# identityにJST暦日・直前logのid等を含むため翌日以降は新identityになる。30日は
# デバッグ・監査のための保持期間として十分で、テーブルの無限成長を防ぐ。
_CLAIM_TTL_SECONDS = 30 * 24 * 60 * 60


class NotificationClaimRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[NotificationClaim] = build_collection_store(
            NotificationClaim,
            "notification_claims.json",
            "claim_id",
            store_dir,
            ttl_seconds=_CLAIM_TTL_SECONDS,
        )

    def try_claim(self, claim: NotificationClaim) -> bool:
        """原子的に新規claimを取得する(既存があればFalse)。"""
        return self._store.insert_if_absent(claim)

    def get_raw(self, claim_id: str) -> str | None:
        """保存中のclaimの生JSON(CASのexpected値に使う)。"""
        return self._store.get_raw_data(claim_id)

    def replace_if_raw_matches(
        self, claim_id: str, expected_raw_data: str, claim: NotificationClaim
    ) -> bool:
        """CAS(SENT化・stale takeoverに使う)。"""
        return self._store.replace_if_raw_matches(claim_id, expected_raw_data, claim)

    def delete_if_raw_matches(self, claim_id: str, expected_raw_data: str) -> bool:
        """条件付き削除(push失敗の補償deleteに使う)。"""
        return self._store.delete_if_raw_matches(claim_id, expected_raw_data)

    def list_all(self) -> list[NotificationClaim]:
        """テスト・調査用(本番の送信経路では使わない)。"""
        return self._store.list_all()
