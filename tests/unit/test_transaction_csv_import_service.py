import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.local_repository.transaction_repository import (
    SkippedRecommendationRepository,
    TransactionRepository,
)
from jstock_advisor.services.transaction_csv_import_service import (
    CsvRowStatus,
    TransactionCsvImportService,
)
from jstock_advisor.services.transaction_history_service import TransactionHistoryService

_NOW = dt.datetime(2026, 7, 24, 8, 0, tzinfo=dt.UTC)

# Issue #61 F-A4: 取引履歴CSVでも owner を必須列にしたため、全ケースで owner を書く。
_HEADER = "owner,stock_code,transaction_type,execution_date,shares,execution_price"


@pytest.fixture
def history_service(tmp_path: Path) -> TransactionHistoryService:
    recommendation_repo = RecommendationRepository(store_dir=tmp_path)
    recommendation_repo.save(
        Recommendation(
            recommendation_id="rec-buy",
            stock_code="2914",
            stock_name="日本たばこ産業",
            recommended_at=_NOW,
            recommendation_type=RecommendationType.BUY,
            buy_prices=BuyPriceLevels(
                standard=PriceWithRationale(price=Decimal("3359"), rationale="x"),
            ),
            price_at_recommendation=Decimal("4200"),
            confidence=ConfidenceLevel.HIGH,
            rule_version="v1-mvp",
        )
    )
    return TransactionHistoryService(
        transaction_repository=TransactionRepository(store_dir=tmp_path),
        skipped_repository=SkippedRecommendationRepository(store_dir=tmp_path),
        recommendation_repository=recommendation_repo,
    )


@pytest.fixture
def import_service(
    history_service: TransactionHistoryService,
) -> TransactionCsvImportService:
    return TransactionCsvImportService(transaction_history_service=history_service)


def _write_csv(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "import.csv"
    path.write_text(content, encoding="utf-8")
    return path


def test_import_valid_row(import_service: TransactionCsvImportService, tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER}\n本人,2914,BUY,2026-07-20,100,3400\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.total_rows == 1
    assert summary.success_count == 1
    assert summary.results[0].status == CsvRowStatus.SUCCESS


def test_import_row_with_recommendation_computes_price_diff(
    import_service: TransactionCsvImportService,
    history_service: TransactionHistoryService,
    tmp_path: Path,
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER},recommendation_id\n本人,2914,BUY,2026-07-20,100,3400,rec-buy\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.success_count == 1
    transactions = history_service.list_transactions("2914")
    assert transactions[0].price_diff_from_recommendation == Decimal("41")


def test_import_missing_required_column_raises(
    import_service: TransactionCsvImportService, tmp_path: Path
) -> None:
    csv_path = _write_csv(tmp_path, "stock_code,transaction_type\n2914,BUY\n")
    with pytest.raises(ValueError, match="必須列"):
        import_service.import_file(csv_path)


def test_import_invalid_transaction_type_is_row_error(
    import_service: TransactionCsvImportService, tmp_path: Path
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER}\n本人,2914,INVALID,2026-07-20,100,3400\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.error_count == 1
    assert summary.results[0].status == CsvRowStatus.ERROR


def test_import_invalid_date_is_row_error(
    import_service: TransactionCsvImportService, tmp_path: Path
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER}\n本人,2914,BUY,2026-13-40,100,3400\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.error_count == 1


def test_import_slash_style_date_is_accepted(
    import_service: TransactionCsvImportService, tmp_path: Path
) -> None:
    """ExternalValueParser導入により、YYYY/MM/DD形式の日付も受理できるようになった
    (実装プラン外部データ正規化レイヤー・修正1)。"""
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER}\n本人,2914,BUY,2026/07/20,100,3400\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.error_count == 0
    assert summary.success_count == 1


def test_import_non_positive_shares_is_row_error(
    import_service: TransactionCsvImportService, tmp_path: Path
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER}\n本人,2914,BUY,2026-07-20,0,3400\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.error_count == 1


def test_import_non_positive_price_is_row_error(
    import_service: TransactionCsvImportService, tmp_path: Path
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER}\n本人,2914,BUY,2026-07-20,100,-3400\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.error_count == 1


def test_import_invalid_account_type_is_row_error(
    import_service: TransactionCsvImportService, tmp_path: Path
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER},account_type\n本人,2914,BUY,2026-07-20,100,3400,INVALID\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.error_count == 1


def test_import_unknown_recommendation_id_is_row_error(
    import_service: TransactionCsvImportService, tmp_path: Path
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER},recommendation_id\n本人,2914,BUY,2026-07-20,100,3400,does-not-exist\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.error_count == 1
    assert "見つかりません" in summary.results[0].message


def test_import_row_level_error_isolation(
    import_service: TransactionCsvImportService, tmp_path: Path
) -> None:
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER}\n本人,2914,BUY,2026-07-20,100,3400\n本人,8136,INVALID,2026-07-20,100,3000\n",
    )
    summary = import_service.import_file(csv_path)
    assert summary.total_rows == 2
    assert summary.success_count == 1
    assert summary.error_count == 1
