import datetime as dt
from pathlib import Path

from jstock_advisor.domain.entities.enums import WatchlistRegistrationSource
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
)

_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)


def _auto_item(stock_code: str, reason: str = "高配当") -> WatchlistItem:
    return WatchlistItem(
        stock_code=stock_code,
        reason=reason,
        registration_source=WatchlistRegistrationSource.AUTO_SCREENING,
        registration_policy="high_dividend_financial_health",
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_add_if_new_adds_absent_item_and_returns_true(tmp_path: Path) -> None:
    repo = WatchlistRepository(store_dir=tmp_path / "local_store")

    added = repo.add_if_new(_auto_item("1234"))

    assert added is True
    stored = repo.get("1234")
    assert stored is not None
    assert stored.registration_source == WatchlistRegistrationSource.AUTO_SCREENING


def test_add_if_new_does_not_overwrite_existing_manual_registration(tmp_path: Path) -> None:
    repo = WatchlistRepository(store_dir=tmp_path / "local_store")
    manual_item = WatchlistItem(
        stock_code="1234",
        reason="手動で気になっている",
        memo="決算後に検討",
        created_at=_NOW,
        updated_at=_NOW,
    )
    repo.upsert(manual_item)

    added = repo.add_if_new(_auto_item("1234", reason="自動追加された理由"))

    assert added is False
    stored = repo.get("1234")
    assert stored is not None
    assert stored.reason == "手動で気になっている"
    assert stored.memo == "決算後に検討"
    assert stored.registration_source == WatchlistRegistrationSource.MANUAL


def test_manual_watchlist_item_defaults_to_manual_registration_source(tmp_path: Path) -> None:
    """既存レコード互換: registration_sourceを明示しない(既存の手動登録経路の)
    WatchlistItemは既定でMANUALになる。"""
    repo = WatchlistRepository(store_dir=tmp_path / "local_store")
    repo.upsert(WatchlistItem(stock_code="9999", created_at=_NOW, updated_at=_NOW))

    stored = repo.get("9999")

    assert stored is not None
    assert stored.registration_source == WatchlistRegistrationSource.MANUAL
    assert stored.registration_policy is None
