from pathlib import Path

from jstock_advisor.infrastructure.local_repository.stock_name_override_repository import (
    StockNameOverrideRepository,
)


def test_get_returns_none_when_not_registered(tmp_path: Path) -> None:
    repo = StockNameOverrideRepository(store_dir=tmp_path)
    assert repo.get("4246") is None


def test_save_and_get_roundtrip(tmp_path: Path) -> None:
    repo = StockNameOverrideRepository(store_dir=tmp_path)
    repo.save("4246", "ダイキョーニシカワ")
    assert repo.get("4246") == "ダイキョーニシカワ"


def test_save_overwrites_existing_entry(tmp_path: Path) -> None:
    repo = StockNameOverrideRepository(store_dir=tmp_path)
    repo.save("4246", "旧名称")
    repo.save("4246", "ダイキョーニシカワ")
    assert repo.get("4246") == "ダイキョーニシカワ"


def test_list_all_returns_every_registered_entry(tmp_path: Path) -> None:
    repo = StockNameOverrideRepository(store_dir=tmp_path)
    repo.save("4246", "ダイキョーニシカワ")
    repo.save("4251", "恵和")
    codes = {item.stock_code for item in repo.list_all()}
    assert codes == {"4246", "4251"}


def test_delete_removes_entry(tmp_path: Path) -> None:
    repo = StockNameOverrideRepository(store_dir=tmp_path)
    repo.save("4246", "ダイキョーニシカワ")
    assert repo.delete("4246") is True
    assert repo.get("4246") is None
