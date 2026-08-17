"""ウォッチリスト管理サービス(要求仕様3節 watchlist_service、6節)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from jstock_advisor.domain.entities.enums import Priority
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.infrastructure.local_repository.watchlist_repository import WatchlistRepository


class WatchlistService:
    def __init__(self, repository: WatchlistRepository | None = None) -> None:
        self._repository = repository or WatchlistRepository()

    def list_items(self) -> list[WatchlistItem]:
        return self._repository.list_all()

    def get_item(self, stock_code: str) -> WatchlistItem | None:
        return self._repository.get(stock_code)

    def add_item(
        self,
        stock_code: str,
        stock_name: str | None = None,
        reason: str | None = None,
        desired_total_yield_pct: float | None = None,
        desired_buy_price: Decimal | None = None,
        benefit_interest: bool = False,
        priority: Priority = Priority.MEDIUM,
        notify_enabled: bool = True,
        memo: str | None = None,
    ) -> WatchlistItem:
        """build_add_item_plan()を呼び出した直後にその場で永続化する薄い
        ラッパー(LINEボタン起点会話型UI・実装プランv2 3節。挙動・戻り値は
        従来と完全に同じ)。"""
        item = self.build_add_item_plan(
            stock_code,
            stock_name=stock_name,
            reason=reason,
            desired_total_yield_pct=desired_total_yield_pct,
            desired_buy_price=desired_buy_price,
            benefit_interest=benefit_interest,
            priority=priority,
            notify_enabled=notify_enabled,
            memo=memo,
        )
        self._repository.upsert(item)
        return item

    def build_add_item_plan(
        self,
        stock_code: str,
        stock_name: str | None = None,
        reason: str | None = None,
        desired_total_yield_pct: float | None = None,
        desired_buy_price: Decimal | None = None,
        benefit_interest: bool = False,
        priority: Priority = Priority.MEDIUM,
        notify_enabled: bool = True,
        memo: str | None = None,
    ) -> WatchlistItem:
        """add_item()と同じ計算(既存登録のcreated_at保持)を行うが、一切の
        永続化を行わず追加対象のWatchlistItemのみを返す(LINEボタン起点
        会話型UI・実装プランv2 3節)。add_item()自体がupsertで自然に冪等
        なため、この計画はTransactWriteItemsで楽観ロック条件を付けずに
        Put(無条件)として扱ってよい(実装プランv2 3節)。
        """
        now = dt.datetime.now(dt.UTC)
        existing = self._repository.get(stock_code)
        return WatchlistItem(
            stock_code=stock_code,
            stock_name=stock_name,
            reason=reason,
            desired_total_yield_pct=desired_total_yield_pct,
            desired_buy_price=desired_buy_price,
            benefit_interest=benefit_interest,
            priority=priority,
            notify_enabled=notify_enabled,
            memo=memo,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )

    def update_item(self, stock_code: str, **fields: Any) -> WatchlistItem:
        existing = self._repository.get(stock_code)
        if existing is None:
            raise ValueError(f"銘柄コード{stock_code}はウォッチリストに登録されていません")
        merged = {
            **existing.model_dump(mode="python"),
            **fields,
            "updated_at": dt.datetime.now(dt.UTC),
        }
        updated = WatchlistItem.model_validate(merged)
        self._repository.upsert(updated)
        return updated

    def delete_item(self, stock_code: str) -> bool:
        return self._repository.delete(stock_code)
