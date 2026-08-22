import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
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
from jstock_advisor.services.watchlist_candidate_collector import (
    WatchlistCandidateCollector,
    select_rotation_window,
)

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
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
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
    """rotation_enabled=false(既定)の場合、従来どおり先頭N件のみを評価対象と
    する固定スライス方式へフォールバックする(移行時の安全弁)。ローテーション
    導入(2026-08)により、candidate_limitによる絞り込みはstaged_rollout_excluded_
    countには含めなくなった(この値は市場区分フィルタによる除外件数のみを表す
    よう意味が変わった。eligible_universe_countとstock_codesの差分が、
    rotation選択されなかった/legacy modeで切り捨てられた件数に相当する)。
    """
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
    assert result.staged_rollout_excluded_count == 0
    assert result.eligible_universe_count == 3
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


# --- select_rotation_window: 永続ラウンドロビン方式(計画Part A-3) -------------


def _items(
    *codes: str, market_segment: str = "プライム（内国株式）"
) -> list[CandidateUniverseItem]:
    return [CandidateUniverseItem(stock_code=c, market_segment=market_segment) for c in codes]


def test_select_rotation_window_first_run_starts_from_head_of_sorted_order() -> None:
    """cursor=None(初回)は安定ソート後の先頭からcandidate_limit件を選ぶ
    (ユニバースの出現順ではなくソート順に依存することを確認)。"""
    items = _items("3333", "1111", "2222")

    selection = select_rotation_window(items, candidate_limit=2, cursor=None)

    assert [i.stock_code for i in selection.items] == ["1111", "2222"]
    assert selection.wrapped is False
    assert selection.new_cursor == ("プライム（内国株式）", "2222")


def test_select_rotation_window_continues_from_cursor() -> None:
    """2回目以降はcursorの直後から継続する(重複なし、次のcandidate_limit件)。"""
    items = _items("1111", "2222", "3333", "4444", "5555")

    selection = select_rotation_window(
        items, candidate_limit=2, cursor=("プライム（内国株式）", "2222")
    )

    assert [i.stock_code for i in selection.items] == ["3333", "4444"]
    assert selection.wrapped is False


def test_select_rotation_window_wraps_at_end_of_universe() -> None:
    """末尾に達したら残り件数分だけ先頭へラップする(wrapped=True)。"""
    items = _items("1111", "2222", "3333", "4444", "5555")

    selection = select_rotation_window(
        items, candidate_limit=3, cursor=("プライム（内国株式）", "4444")
    )

    assert [i.stock_code for i in selection.items] == ["5555", "1111", "2222"]
    assert selection.wrapped is True
    assert selection.new_cursor == ("プライム（内国株式）", "2222")


def test_select_rotation_window_cursor_missing_from_universe_continues_after_it() -> None:
    """計画Part A-3実運用ケース: 前回選択した最後の銘柄(0300)が今回finalizeで
    ウォッチリストへ追加されeligible universeから消失していても、bisect_right
    により直後(0301)から正しく継続できることを確認する(固定回帰テスト)。"""
    items = _items("0100", "0200", "0301", "0400")  # 0300は既に除外され現存しない

    selection = select_rotation_window(
        items, candidate_limit=2, cursor=("プライム（内国株式）", "0300")
    )

    assert [i.stock_code for i in selection.items] == ["0301", "0400"]
    assert selection.wrapped is False


def test_select_rotation_window_universe_smaller_than_limit_selects_all_once() -> None:
    """除外後ユニバースがcandidate_limit未満でも、同一実行内で同一銘柄を
    2回選ぶことはない(停止条件の確認)。cursor=None(先頭開始)から全件を
    選び切った時点で停止条件が先に成立するため、この場合wrapped=Falseになる
    (ラップは「idxが末尾を超えてもまだ選択が必要」な場合のみ発生する)。"""
    items = _items("1111", "2222")

    selection = select_rotation_window(items, candidate_limit=5, cursor=None)

    assert sorted(i.stock_code for i in selection.items) == ["1111", "2222"]
    assert len(selection.items) == 2
    assert selection.wrapped is False

    # cursorが末尾側にある場合は、残り件数を得るために先頭へラップする。
    selection2 = select_rotation_window(
        items, candidate_limit=5, cursor=("プライム（内国株式）", "1111")
    )
    assert sorted(i.stock_code for i in selection2.items) == ["1111", "2222"]
    assert selection2.wrapped is True


def test_collector_rotation_enabled_uses_sorted_order_and_cursor(tmp_path: Path) -> None:
    """WatchlistCandidateCollector経由でrotation_enabled=Trueを指定した場合、
    出現順ではなく安定ソート+cursorに従って選択されることを確認する。"""
    from jstock_advisor.config.models import StagedRolloutConfig

    store_dir = tmp_path / "local_store"
    items = _items("3333", "1111", "2222", "4444")
    universe = CandidateUniverseResult(
        items=items, raw_row_count=4, duplicate_count=0, invalid_code_count=0, selected_count=4
    )
    collector = WatchlistCandidateCollector(
        _FakeUniverseProvider(universe),
        _FakeScreeningDataProvider(),
        holding_repository=HoldingRepository(store_dir=store_dir),
        watchlist_repository=WatchlistRepository(store_dir=store_dir),
        staged_rollout=StagedRolloutConfig(candidate_limit=2, market_segment_filter=None),
        rotation_enabled=True,
    )

    result = collector.collect_target_codes(rotation_cursor=None)

    assert result.stock_codes == ["1111", "2222"]
    assert result.rotation_cursor_after == ("プライム（内国株式）", "2222")
    assert result.rotation_wrapped is False
    assert result.eligible_universe_count == 4

    result2 = collector.collect_target_codes(rotation_cursor=result.rotation_cursor_after)

    assert result2.stock_codes == ["3333", "4444"]
