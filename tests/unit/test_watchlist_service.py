from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.enums import Priority
from jstock_advisor.services.watchlist_service import WatchlistService


def test_add_and_list_item(watchlist_service: WatchlistService) -> None:
    watchlist_service.add_item(stock_code="7203", stock_name="トヨタ自動車", priority=Priority.HIGH)
    items = watchlist_service.list_items()
    assert len(items) == 1
    assert items[0].stock_code == "7203"
    assert items[0].priority == Priority.HIGH


def test_update_item_merges_fields(watchlist_service: WatchlistService) -> None:
    watchlist_service.add_item(stock_code="7203", stock_name="トヨタ自動車")
    updated = watchlist_service.update_item(
        "7203", desired_buy_price=Decimal("2500"), memo="決算後に再検討"
    )
    assert updated.desired_buy_price == Decimal("2500")
    assert updated.memo == "決算後に再検討"
    # 更新していないフィールドは保持される
    assert updated.stock_name == "トヨタ自動車"


def test_update_missing_item_raises(watchlist_service: WatchlistService) -> None:
    with pytest.raises(ValueError, match="登録されていません"):
        watchlist_service.update_item("9999", memo="x")


def test_delete_item(watchlist_service: WatchlistService) -> None:
    watchlist_service.add_item(stock_code="7203")
    assert watchlist_service.delete_item("7203") is True
    assert watchlist_service.list_items() == []
    assert watchlist_service.delete_item("7203") is False
