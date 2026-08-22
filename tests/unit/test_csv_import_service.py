import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER
from jstock_advisor.services.csv_import_service import CsvRowStatus, HoldingsCsvImportService
from jstock_advisor.services.portfolio_service import PortfolioService


def _write_csv(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "holdings.csv"
    path.write_text(content, encoding="utf-8-sig")
    return path


def test_valid_rows_are_imported(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    csv_path = _write_csv(
        tmp_path,
        "stock_code,stock_name,shares,purchase_price,purchase_date,account_type\n"
        "2914,日本たばこ産業,100,4200,2025-05-10,NISA\n",
    )
    summary = csv_import_service.import_file(csv_path)
    assert summary.total_rows == 1
    assert summary.success_count == 1
    assert summary.error_count == 0
    holding = portfolio_service.get_holding(DEFAULT_OWNER, "2914")
    assert holding is not None
    assert holding.shares == 100


def test_missing_required_column_raises(
    tmp_path: Path, csv_import_service: HoldingsCsvImportService
) -> None:
    csv_path = _write_csv(tmp_path, "stock_code,shares\n2914,100\n")
    try:
        csv_import_service.import_file(csv_path)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "purchase_price" in str(e)


def test_invalid_rows_are_reported_without_blocking_others(
    tmp_path: Path, csv_import_service: HoldingsCsvImportService
) -> None:
    csv_path = _write_csv(
        tmp_path,
        "stock_code,shares,purchase_price,account_type\n"
        "2914,100,4200,NISA\n"  # valid
        "7203,-5,3000,NISA\n"  # invalid shares
        "6501,100,abc,NISA\n"  # invalid price
        "12345,100,3000,NISA\n",  # invalid stock code length
    )
    summary = csv_import_service.import_file(csv_path)
    assert summary.total_rows == 4
    assert summary.success_count == 1
    assert summary.error_count == 3
    statuses = {r.row_number: r.status for r in summary.results}
    assert statuses[2] == CsvRowStatus.SUCCESS
    assert statuses[3] == CsvRowStatus.ERROR
    assert statuses[4] == CsvRowStatus.ERROR
    assert statuses[5] == CsvRowStatus.ERROR


def test_duplicate_row_in_same_csv_is_flagged_as_warning(
    tmp_path: Path, csv_import_service: HoldingsCsvImportService
) -> None:
    csv_path = _write_csv(
        tmp_path,
        "stock_code,shares,purchase_price,purchase_date,account_type\n"
        "2914,100,4200,2025-05-10,NISA\n"
        "2914,100,4200,2025-05-10,NISA\n",
    )
    summary = csv_import_service.import_file(csv_path)
    assert summary.results[1].status == CsvRowStatus.WARNING
    assert "重複" in summary.results[1].message


def test_missing_account_type_defaults_to_general_with_warning(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    csv_path = _write_csv(tmp_path, "stock_code,shares,purchase_price\n2914,100,4200\n")
    summary = csv_import_service.import_file(csv_path)
    assert summary.results[0].status == CsvRowStatus.WARNING
    holding = portfolio_service.get_holding(DEFAULT_OWNER, "2914")
    assert holding is not None
    assert holding.account_type.value == "GENERAL"


def test_existing_holding_additional_purchase_accumulates(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    portfolio_service.register_purchase(
        owner=DEFAULT_OWNER,
        stock_code="2914",
        stock_name="日本たばこ産業",
        shares=100,
        purchase_price=Decimal("4000"),
        purchase_date=dt.date(2025, 1, 1),
        account_type=AccountType.NISA,
    )
    csv_path = _write_csv(
        tmp_path, "stock_code,shares,purchase_price,account_type\n2914,100,4400,NISA\n"
    )
    summary = csv_import_service.import_file(csv_path, on_duplicate="additional_purchase")
    assert summary.results[0].status == CsvRowStatus.WARNING
    holding = portfolio_service.get_holding(DEFAULT_OWNER, "2914")
    assert holding is not None
    assert holding.shares == 200


def test_existing_holding_overwrite_replaces_lots(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    portfolio_service.register_purchase(
        owner=DEFAULT_OWNER,
        stock_code="2914",
        stock_name="日本たばこ産業",
        shares=100,
        purchase_price=Decimal("4000"),
        purchase_date=dt.date(2025, 1, 1),
        account_type=AccountType.NISA,
    )
    csv_path = _write_csv(
        tmp_path, "stock_code,shares,purchase_price,account_type\n2914,50,4400,NISA\n"
    )
    summary = csv_import_service.import_file(csv_path, on_duplicate="overwrite")
    assert summary.results[0].status == CsvRowStatus.WARNING
    holding = portfolio_service.get_holding(DEFAULT_OWNER, "2914")
    assert holding is not None
    assert holding.shares == 50
    assert holding.average_purchase_price == Decimal("4400")
