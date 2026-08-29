from collections.abc import Iterator
from pathlib import Path

import pytest

from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.infrastructure.local_repository.watchlist_repository import WatchlistRepository
from jstock_advisor.services import jpx_industry_source as jpx_industry_source_module
from jstock_advisor.services.csv_import_service import HoldingsCsvImportService
from jstock_advisor.services.jpx_industry_source import (
    JpxIndustryEntry,
    reset_default_jpx_industry_source,
)
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


@pytest.fixture(autouse=True)
def _isolated_jpx_industry_source(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, JpxIndustryEntry]]:
    """JPX業種ソース(Issue #54 Phase B-1の観測用)をunit testから隔離する。

    既定は空マップ = 「JPXでは解決できない」。実キャッシュを読むと、開発者の
    ローカルにdata_j.xlsが落ちているかどうかでテスト結果が変わり、
    `CandidateUniverseCacheIO` がキャッシュディレクトリを作る副作用も生じる。

    JPXで解決できる状態を再現したいテストは、yieldされるdictへ登録するか、
    BuySignalServiceへ自前のsourceを注入する。
    """
    entries: dict[str, JpxIndustryEntry] = {}
    monkeypatch.setattr(
        jpx_industry_source_module, "_load_jpx_industry_map", lambda: entries
    )
    reset_default_jpx_industry_source()
    yield entries
    reset_default_jpx_industry_source()
