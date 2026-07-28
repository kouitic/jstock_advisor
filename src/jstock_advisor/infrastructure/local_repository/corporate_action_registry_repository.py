"""企業行動(株式分割等)の手動登録データのローカルリポジトリ(要求仕様2節)。

yfinanceが自動取得できるのはSPLIT/REVERSE_SPLITのみのため、無償割当・
スピンオフ・銘柄コード変更・合併・上場廃止・配当基準変更は、運用者が
一次情報を確認したうえで手動登録する(株主優待の手動登録と同じ設計方針)。
1銘柄に複数のイベントが起こりうるため、(stock_code, event_type,
effective_date)の組を合成キーとする。
"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store
from jstock_advisor.interfaces.types import CorporateActionEvent


def _event_id(event: CorporateActionEvent) -> str:
    effective = event.effective_date.isoformat() if event.effective_date else "unscheduled"
    return f"{event.stock_code}:{event.event_type.value}:{effective}"


class _KeyedCorporateActionEvent(CorporateActionEvent):
    """CollectionStoreのid_fieldに合成キーを持たせるための内部ラッパー。"""

    event_id: str


class CorporateActionRegistryRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[_KeyedCorporateActionEvent] = build_collection_store(
            _KeyedCorporateActionEvent, "corporate_action_registry.json", "event_id", store_dir
        )

    def list_by_stock(self, stock_code: str) -> list[CorporateActionEvent]:
        return list(self._store.find(lambda e: e.stock_code == stock_code))

    def save(self, event: CorporateActionEvent) -> None:
        keyed = _KeyedCorporateActionEvent(event_id=_event_id(event), **event.model_dump())
        self._store.upsert(keyed)

    def delete(self, event: CorporateActionEvent) -> bool:
        return self._store.delete(_event_id(event))
