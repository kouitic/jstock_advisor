from collections.abc import Iterator
from pathlib import Path

import pytest

from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.infrastructure.local_repository.watchlist_repository import WatchlistRepository
from jstock_advisor.services import jpx_industry_source as jpx_industry_source_module
from jstock_advisor.services.csv_import_ledger import CsvImportLedger
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
def csv_import_ledger(store_dir: Path) -> CsvImportLedger:
    """Issue #61 Phase B1: 取込済み台帳もtmp_pathへ隔離する
    (既定のAuditLogRepositoryを使うとテストが実データ領域へ書き込むため)。"""
    return CsvImportLedger(repository=AuditLogRepository(store_dir=store_dir))


@pytest.fixture
def csv_import_service(
    portfolio_service: PortfolioService, csv_import_ledger: CsvImportLedger
) -> HoldingsCsvImportService:
    return HoldingsCsvImportService(
        portfolio_service=portfolio_service, ledger=csv_import_ledger
    )


@pytest.fixture(autouse=True)
def _isolated_jpx_industry_source(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, JpxIndustryEntry]]:
    """JPX業種ソース(Issue #54 Phase B-1の観測用)をunit testから隔離する。

    `JpxIndustrySource` はプロセス内共有インスタンス(`_DEFAULT_SOURCE`)を持ち、
    その中に成功マップとnegative cacheのtimestampを保持する。テスト間で漏れると
    実行順序で結果が変わるため、本fixtureが **各テストの前後で必ずreset** する。
    resetは共有インスタンスそのものを破棄するため、成功マップとtimestampの
    双方が同時に消える(片方だけ残ることはない)。

    既定のローダは空マップ = 「一覧は読めたが当該銘柄が無い」(`NOT_FOUND`)。
    実キャッシュを読むと、開発者のローカルにdata_j.xlsが落ちているかどうかで
    テスト結果が変わり、`CandidateUniverseCacheIO` がキャッシュディレクトリを
    作る副作用も生じるため、実装関数ごと差し替える。

    JPXで解決できる状態を再現したいテストはyieldされるdictへ登録する。
    ローダ自体を検証するテストは、この差し替えを実装関数へ戻したうえで
    内側のキャッシュIOを差し替える(tests/unit/test_jpx_industry_source.py)。
    """
    entries: dict[str, JpxIndustryEntry] = {}
    monkeypatch.setattr(jpx_industry_source_module, "_load_jpx_industry_map", lambda: entries)
    reset_default_jpx_industry_source()
    yield entries
    reset_default_jpx_industry_source()
