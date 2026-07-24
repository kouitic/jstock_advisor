from pathlib import Path

import pytest

from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.infrastructure.local_repository.watchlist_repository import WatchlistRepository
from jstock_advisor.services.csv_import_service import HoldingsCsvImportService
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.watchlist_service import WatchlistService


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    return tmp_path / "local_store"


@pytest.fixture
def portfolio_service(store_dir: Path) -> PortfolioService:
    return PortfolioService(
        holding_repository=HoldingRepository(store_dir=store_dir),
        lot_repository=PurchaseLotRepository(store_dir=store_dir),
    )


@pytest.fixture
def watchlist_service(store_dir: Path) -> WatchlistService:
    return WatchlistService(repository=WatchlistRepository(store_dir=store_dir))


@pytest.fixture
def csv_import_service(portfolio_service: PortfolioService) -> HoldingsCsvImportService:
    return HoldingsCsvImportService(portfolio_service=portfolio_service)
