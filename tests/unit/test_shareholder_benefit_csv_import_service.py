import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import RecordDateUnknownReason, SourceType
from jstock_advisor.infrastructure.local_repository.shareholder_benefit_registry_repository import (
    ShareholderBenefitRegistryRepository,
)
from jstock_advisor.services.shareholder_benefit_csv_import_service import (
    CsvRowStatus,
    ShareholderBenefitCsvImportService,
)

_HEADER = (
    "stock_code,min_shares_required,frequency_per_year,category,description,min_shares_for_tier"
)


@pytest.fixture
def repository(tmp_path: Path) -> ShareholderBenefitRegistryRepository:
    return ShareholderBenefitRegistryRepository(store_dir=tmp_path)


@pytest.fixture
def import_service(
    repository: ShareholderBenefitRegistryRepository,
) -> ShareholderBenefitCsvImportService:
    return ShareholderBenefitCsvImportService(repository=repository)


def _write_csv(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "import.csv"
    path.write_text(content, encoding="utf-8")
    return path


def test_import_valid_single_row(
    import_service: ShareholderBenefitCsvImportService,
    repository: ShareholderBenefitRegistryRepository,
    tmp_path: Path,
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER}\n2914,100,1,CASH_EQUIVALENT,クオカード1000円分,100\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.success_count == 1
    saved = repository.get("2914")
    assert saved is not None
    assert len(saved.benefits) == 1


def test_import_groups_multiple_tiers_for_same_stock(
    import_service: ShareholderBenefitCsvImportService,
    repository: ShareholderBenefitRegistryRepository,
    tmp_path: Path,
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER}\n"
        "2914,100,1,CASH_EQUIVALENT,100株優待,100\n"
        "2914,100,1,CASH_EQUIVALENT,1000株優待,1000\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.success_count == 2
    saved = repository.get("2914")
    assert saved is not None
    assert len(saved.benefits) == 2
    assert saved.min_shares_required == 100  # 最初の行の値を採用


def test_import_optional_columns(
    import_service: ShareholderBenefitCsvImportService,
    repository: ShareholderBenefitRegistryRepository,
    tmp_path: Path,
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER},estimated_value,benefit_record_dates,is_abolished,change_note\n"
        "2914,100,1,CASH_EQUIVALENT,x,100,1000,2026-03-31;2026-09-30,true,廃止予定\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.success_count == 1
    saved = repository.get("2914")
    assert saved is not None
    assert saved.benefits[0].estimated_value == Decimal("1000")
    assert saved.benefit_record_dates[0].isoformat() == "2026-03-31"
    assert saved.is_abolished is True
    assert saved.change_note == "廃止予定"


def test_import_sets_source_type_and_ex_date_and_holding_requirement(
    import_service: ShareholderBenefitCsvImportService,
    repository: ShareholderBenefitRegistryRepository,
    tmp_path: Path,
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER},benefit_record_dates,benefit_ex_date,long_term_holding_requirement\n"
        "2914,100,1,CASH_EQUIVALENT,x,100,2026-03-31,2026-03-27,3年以上継続保有で優遇\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.success_count == 1
    saved = repository.get("2914")
    assert saved is not None
    assert saved.source.source_type == SourceType.MANUAL_REGISTRY
    assert saved.source.primary_source_flag is True
    assert saved.benefit_ex_date is not None
    assert saved.benefit_ex_date.isoformat() == "2026-03-27"
    assert saved.long_term_holding_requirement == "3年以上継続保有で優遇"
    assert saved.benefit_record_date_unknown_reason is None


def test_import_without_record_dates_sets_unknown_reason(
    import_service: ShareholderBenefitCsvImportService,
    repository: ShareholderBenefitRegistryRepository,
    tmp_path: Path,
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER}\n2914,100,1,CASH_EQUIVALENT,x,100\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.success_count == 1
    saved = repository.get("2914")
    assert saved is not None
    assert saved.benefit_record_date_unknown_reason == RecordDateUnknownReason.SOURCE_NOT_FOUND


def test_import_long_term_holding_condition_max_months_column(
    import_service: ShareholderBenefitCsvImportService,
    repository: ShareholderBenefitRegistryRepository,
    tmp_path: Path,
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER},long_term_holding_condition_months,long_term_holding_condition_max_months\n"
        "9432,100,1,VERSATILE_POINT,x,100,24,35\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.success_count == 1
    saved = repository.get("9432")
    assert saved is not None
    assert saved.benefits[0].long_term_holding_condition_months == 24
    assert saved.benefits[0].long_term_holding_condition_max_months == 35


def test_import_tier_group_column(
    import_service: ShareholderBenefitCsvImportService,
    repository: ShareholderBenefitRegistryRepository,
    tmp_path: Path,
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER},tier_group\n"
        "5139,100,2,CASH_EQUIVALENT,100株/6ヶ月,100,digital_gift\n"
        "5139,100,2,CASH_EQUIVALENT,1000株/6ヶ月,1000,digital_gift\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.success_count == 2
    saved = repository.get("5139")
    assert saved is not None
    assert [d.tier_group for d in saved.benefits] == ["digital_gift", "digital_gift"]


def test_import_record_date_recurrence_months_computes_next_date(
    import_service: ShareholderBenefitCsvImportService,
    repository: ShareholderBenefitRegistryRepository,
    tmp_path: Path,
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER},benefit_record_date_recurrence_months\n2914,100,1,CASH_EQUIVALENT,x,100,3;9\n",
    )
    summary = import_service.import_file(
        csv_path, now=dt.datetime(2026, 7, 30, tzinfo=dt.UTC)
    )
    assert summary.success_count == 1
    saved = repository.get("2914")
    assert saved is not None
    assert saved.benefit_record_date_recurrence_months == [3, 9]
    assert saved.next_benefit_record_date == dt.date(2026, 9, 30)


def test_import_record_date_recurrence_months_out_of_range_is_row_error(
    import_service: ShareholderBenefitCsvImportService, tmp_path: Path
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER},benefit_record_date_recurrence_months\n2914,100,1,CASH_EQUIVALENT,x,100,13\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.error_count == 1


def test_import_missing_required_column_raises(
    import_service: ShareholderBenefitCsvImportService, tmp_path: Path
) -> None:
    csv_path = _write_csv(tmp_path, "stock_code,category\n2914,CASH_EQUIVALENT\n")
    with pytest.raises(ValueError, match="必須列"):
        import_service.import_file(csv_path)


def test_import_invalid_category_is_row_error(
    import_service: ShareholderBenefitCsvImportService, tmp_path: Path
) -> None:
    csv_path = _write_csv(tmp_path, f"{_HEADER}\n2914,100,1,INVALID,x,100\n")
    summary = import_service.import_file(csv_path)
    assert summary.error_count == 1
    assert summary.results[0].status == CsvRowStatus.ERROR


def test_import_non_positive_min_shares_required_is_row_error(
    import_service: ShareholderBenefitCsvImportService, tmp_path: Path
) -> None:
    csv_path = _write_csv(tmp_path, f"{_HEADER}\n2914,0,1,CASH_EQUIVALENT,x,100\n")
    summary = import_service.import_file(csv_path)
    assert summary.error_count == 1


def test_import_missing_description_is_row_error(
    import_service: ShareholderBenefitCsvImportService, tmp_path: Path
) -> None:
    csv_path = _write_csv(tmp_path, f"{_HEADER}\n2914,100,1,CASH_EQUIVALENT,,100\n")
    summary = import_service.import_file(csv_path)
    assert summary.error_count == 1


def test_import_row_level_error_isolation(
    import_service: ShareholderBenefitCsvImportService,
    repository: ShareholderBenefitRegistryRepository,
    tmp_path: Path,
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER}\n2914,100,1,CASH_EQUIVALENT,valid,100\n8136,100,1,INVALID,x,100\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.total_rows == 2
    assert summary.success_count == 1
    assert summary.error_count == 1
    assert repository.get("2914") is not None
    assert repository.get("8136") is None
