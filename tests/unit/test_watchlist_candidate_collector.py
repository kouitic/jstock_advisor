import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.infrastructure.local_repository.holding_repository import HoldingRepository
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
)
from jstock_advisor.interfaces.candidate_universe import (
    CandidateUniverseItem,
    CandidateUniverseResult,
)
from jstock_advisor.services.screening_data_provider import (
    ScreeningDataResult,
    ScreeningDataStatus,
)
from jstock_advisor.services.watchlist_candidate_collector import WatchlistCandidateCollector

_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)


class _FakeUniverseProvider:
    def __init__(self, result: CandidateUniverseResult) -> None:
        self._result = result

    def get_candidate_universe(self) -> CandidateUniverseResult:
        return self._result


class _FakeScreeningDataProvider:
    def __init__(self) -> None:
        self.requested_codes: list[str] = []

    def get_screening_input(self, stock_code: str, now: dt.datetime) -> ScreeningDataResult:
        self.requested_codes.append(stock_code)
        return ScreeningDataResult(
            status=ScreeningDataStatus.NOT_FOUND, input=None, missing_fields=[], error_message=None
        )


def _holding(stock_code: str) -> Holding:
    return Holding(
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        shares=100,
        average_purchase_price=Decimal("1000"),
        total_purchase_amount=Decimal("100000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _watchlist_item(stock_code: str) -> WatchlistItem:
    return WatchlistItem(stock_code=stock_code, created_at=_NOW, updated_at=_NOW)


def _universe(
    stock_codes: list[str],
    *,
    raw_row_count: int,
    duplicate_count: int = 0,
    invalid_code_count: int = 0,
) -> CandidateUniverseResult:
    items = [CandidateUniverseItem(stock_code=code) for code in stock_codes]
    return CandidateUniverseResult(
        items=items,
        raw_row_count=raw_row_count,
        duplicate_count=duplicate_count,
        invalid_code_count=invalid_code_count,
        selected_count=len(items),
    )


def test_collect_target_codes_excludes_held_and_watchlisted(tmp_path: Path) -> None:
    store_dir = tmp_path / "local_store"
    holding_repo = HoldingRepository(store_dir=store_dir)
    watchlist_repo = WatchlistRepository(store_dir=store_dir)
    holding_repo.upsert(_holding("1111"))
    watchlist_repo.upsert(_watchlist_item("2222"))

    universe = _universe(["1111", "2222", "3333"], raw_row_count=3)
    collector = WatchlistCandidateCollector(
        _FakeUniverseProvider(universe),
        _FakeScreeningDataProvider(),
        holding_repository=holding_repo,
        watchlist_repository=watchlist_repo,
    )

    result = collector.collect_target_codes()

    assert result.stock_codes == ["3333"]
    assert result.universe_count == 3
    assert result.holding_excluded_count == 1
    assert result.watchlist_excluded_count == 1


def test_collect_target_codes_passes_through_universe_diagnostics(tmp_path: Path) -> None:
    store_dir = tmp_path / "local_store"
    universe = _universe(["1111"], raw_row_count=5, duplicate_count=3, invalid_code_count=1)
    collector = WatchlistCandidateCollector(
        _FakeUniverseProvider(universe),
        _FakeScreeningDataProvider(),
        holding_repository=HoldingRepository(store_dir=store_dir),
        watchlist_repository=WatchlistRepository(store_dir=store_dir),
    )

    result = collector.collect_target_codes()

    assert result.duplicate_count == 3
    assert result.invalid_code_count == 1


def test_collect_target_codes_with_no_exclusions_returns_full_universe(tmp_path: Path) -> None:
    store_dir = tmp_path / "local_store"
    universe = _universe(["1111", "2222"], raw_row_count=2)
    collector = WatchlistCandidateCollector(
        _FakeUniverseProvider(universe),
        _FakeScreeningDataProvider(),
        holding_repository=HoldingRepository(store_dir=store_dir),
        watchlist_repository=WatchlistRepository(store_dir=store_dir),
    )

    result = collector.collect_target_codes()

    assert result.stock_codes == ["1111", "2222"]
    assert result.holding_excluded_count == 0
    assert result.watchlist_excluded_count == 0


def test_staged_rollout_candidate_limit_truncates_target_list(tmp_path: Path) -> None:
    """15節: candidate_limit設定時、先頭N件のみを評価対象とすること。"""
    from jstock_advisor.config.models import StagedRolloutConfig

    store_dir = tmp_path / "local_store"
    universe = _universe(["1111", "2222", "3333"], raw_row_count=3)
    collector = WatchlistCandidateCollector(
        _FakeUniverseProvider(universe),
        _FakeScreeningDataProvider(),
        holding_repository=HoldingRepository(store_dir=store_dir),
        watchlist_repository=WatchlistRepository(store_dir=store_dir),
        staged_rollout=StagedRolloutConfig(candidate_limit=2, market_segment_filter=None),
    )

    result = collector.collect_target_codes()

    assert result.stock_codes == ["1111", "2222"]
    assert result.staged_rollout_excluded_count == 1
    assert result.universe_count == 3  # 絞り込み前の件数は変わらない


def test_staged_rollout_market_segment_filter_excludes_other_segments(tmp_path: Path) -> None:
    from jstock_advisor.config.models import StagedRolloutConfig

    store_dir = tmp_path / "local_store"
    items = [
        CandidateUniverseItem(stock_code="1111", market_segment="プライム（内国株式）"),
        CandidateUniverseItem(stock_code="2222", market_segment="スタンダード（内国株式）"),
    ]
    universe = CandidateUniverseResult(
        items=items, raw_row_count=2, duplicate_count=0, invalid_code_count=0, selected_count=2
    )
    collector = WatchlistCandidateCollector(
        _FakeUniverseProvider(universe),
        _FakeScreeningDataProvider(),
        holding_repository=HoldingRepository(store_dir=store_dir),
        watchlist_repository=WatchlistRepository(store_dir=store_dir),
        staged_rollout=StagedRolloutConfig(
            candidate_limit=None, market_segment_filter=["プライム（内国株式）"]
        ),
    )

    result = collector.collect_target_codes()

    assert result.stock_codes == ["1111"]
    assert result.staged_rollout_excluded_count == 1


def test_staged_rollout_none_applies_no_filtering(tmp_path: Path) -> None:
    store_dir = tmp_path / "local_store"
    universe = _universe(["1111", "2222"], raw_row_count=2)
    collector = WatchlistCandidateCollector(
        _FakeUniverseProvider(universe),
        _FakeScreeningDataProvider(),
        holding_repository=HoldingRepository(store_dir=store_dir),
        watchlist_repository=WatchlistRepository(store_dir=store_dir),
        staged_rollout=None,
    )

    result = collector.collect_target_codes()

    assert result.stock_codes == ["1111", "2222"]
    assert result.staged_rollout_excluded_count == 0


def test_fetch_screening_data_delegates_to_injected_provider(tmp_path: Path) -> None:
    store_dir = tmp_path / "local_store"
    universe = _universe([], raw_row_count=0)
    screening_data_provider = _FakeScreeningDataProvider()
    collector = WatchlistCandidateCollector(
        _FakeUniverseProvider(universe),
        screening_data_provider,
        holding_repository=HoldingRepository(store_dir=store_dir),
        watchlist_repository=WatchlistRepository(store_dir=store_dir),
    )

    collector.fetch_screening_data("1234", _NOW)

    assert screening_data_provider.requested_codes == ["1234"]
