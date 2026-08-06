"""LINE通知履歴のローカルリポジトリ(要求仕様10節・16節)。同一内容の重複通知防止に使用する。"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.domain.entities.enums import NotificationType
from jstock_advisor.domain.entities.notification import NotificationLog
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store


class NotificationLogRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[NotificationLog] = build_collection_store(
            NotificationLog, "notification_log.json", "notification_id", store_dir
        )

    def list_all(self) -> list[NotificationLog]:
        return self._store.list_all()

    def list_by_stock_and_type(
        self, stock_code: str, notification_type: NotificationType
    ) -> list[NotificationLog]:
        items = self._store.find(
            lambda n: n.stock_code == stock_code and n.notification_type == notification_type
        )
        return sorted(items, key=lambda n: n.sent_at)

    def latest_by_stock_and_type(
        self, stock_code: str, notification_type: NotificationType
    ) -> NotificationLog | None:
        items = self.list_by_stock_and_type(stock_code, notification_type)
        return items[-1] if items else None

    def list_by_recommendation_id(self, recommendation_id: str) -> list[NotificationLog]:
        """backtest/compareのhistory replayが「実際にLINE送信が成功したか」を
        判定するために使う(コードレビュー対応)。複数件ある場合は重複送信の
        可能性があるため、呼び出し側で件数を確認すること。"""
        items = self._store.find(lambda n: n.related_recommendation_id == recommendation_id)
        return sorted(items, key=lambda n: n.sent_at)

    def save(self, log: NotificationLog) -> None:
        self._store.upsert(log)
