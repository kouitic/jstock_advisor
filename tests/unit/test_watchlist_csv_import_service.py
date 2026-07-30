from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import Priority
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
)
from jstock_advisor.services.watchlist_csv_import_service import (
    CsvRowStatus,
    WatchlistCsvImportService,
)
from jstock_advisor.services.watchlist_service import WatchlistService

_HEADER = "stock_code"


@pytest.fixture
def repository(tmp_path: Path) -> WatchlistRepository:
    return WatchlistRepository(store_dir=tmp_path)


@pytest.fixture
def watchlist_service(repository: WatchlistRepository) -> WatchlistService:
    return WatchlistService(repository=repository)


@pytest.fixture
def import_service(watchlist_service: WatchlistService) -> WatchlistCsvImportService:
    return WatchlistCsvImportService(watchlist_service=watchlist_service)


def _write_csv(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "import.csv"
    path.write_text(content, encoding="utf-8")
    return path


def test_import_minimal_row(
    import_service: WatchlistCsvImportService,
    repository: WatchlistRepository,
    tmp_path: Path,
) -> None:
    csv_path = _write_csv(tmp_path, f"{_HEADER}\n7203\n")
    summary = import_service.import_file(csv_path)
    assert summary.success_count == 1
    saved = repository.get("7203")
    assert saved is not None
    assert saved.priority == Priority.MEDIUM
    assert saved.notify_enabled is True


def test_import_all_optional_columns(
    import_service: WatchlistCsvImportService,
    repository: WatchlistRepository,
    tmp_path: Path,
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER},stock_name,reason,desired_total_yield_pct,desired_buy_price,"
        "benefit_interest,priority,notify_enabled,memo\n"
        "7203,トヨタ自動車,割安スクリーニング,4.5,2000,true,HIGH,false,テストメモ\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.success_count == 1
    saved = repository.get("7203")
    assert saved is not None
    assert saved.stock_name == "トヨタ自動車"
    assert saved.reason == "割安スクリーニング"
    assert saved.desired_total_yield_pct == 4.5
    assert saved.desired_buy_price == Decimal("2000")
    assert saved.benefit_interest is True
    assert saved.priority == Priority.HIGH
    assert saved.notify_enabled is False
    assert saved.memo == "テストメモ"


def test_import_invalid_stock_code_is_row_error(
    import_service: WatchlistCsvImportService, tmp_path: Path
) -> None:
    csv_path = _write_csv(tmp_path, f"{_HEADER}\ntoolongcode\n")
    summary = import_service.import_file(csv_path)
    assert summary.error_count == 1
    assert summary.results[0].status == CsvRowStatus.ERROR


def test_import_invalid_priority_is_row_error(
    import_service: WatchlistCsvImportService, tmp_path: Path
) -> None:
    csv_path = _write_csv(tmp_path, f"{_HEADER},priority\n7203,INVALID\n")
    summary = import_service.import_file(csv_path)
    assert summary.error_count == 1


def test_import_missing_required_column_raises(
    import_service: WatchlistCsvImportService, tmp_path: Path
) -> None:
    csv_path = _write_csv(tmp_path, "stock_name\nトヨタ\n")
    with pytest.raises(ValueError, match="必須列"):
        import_service.import_file(csv_path)


def test_import_overwrites_existing(
    import_service: WatchlistCsvImportService,
    repository: WatchlistRepository,
    tmp_path: Path,
) -> None:
    csv_path1 = _write_csv(tmp_path, f"{_HEADER},priority\n7203,LOW\n")
    import_service.import_file(csv_path1)

    csv_path2 = tmp_path / "import2.csv"
    csv_path2.write_text(f"{_HEADER},priority\n7203,HIGH\n", encoding="utf-8")
    import_service.import_file(csv_path2)

    saved = repository.get("7203")
    assert saved is not None
    assert saved.priority == Priority.HIGH


def test_import_row_level_error_isolation(
    import_service: WatchlistCsvImportService,
    repository: WatchlistRepository,
    tmp_path: Path,
) -> None:
    csv_path = _write_csv(tmp_path, f"{_HEADER},priority\n7203,LOW\n8136,INVALID\n")
    summary = import_service.import_file(csv_path)
    assert summary.total_rows == 2
    assert summary.success_count == 1
    assert summary.error_count == 1
    assert repository.get("7203") is not None
    assert repository.get("8136") is None
