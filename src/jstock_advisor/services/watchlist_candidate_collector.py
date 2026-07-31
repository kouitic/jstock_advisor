"""ウォッチリスト自動追加: 候補銘柄の収集(ユニバース取得+事前除外)。

CandidateUniverseProviderから対象銘柄コード一覧を取得し、保有銘柄・既存
ウォッチリスト銘柄を事前除外する。各銘柄のスクリーニング用データ取得は
ScreeningDataProviderへ委譲する(合否判定はWatchlistScreeningService/
ScreeningPolicyの責務であり、ここでは一切行わない)。

Lambda fan-out時は、dispatch側がcollect_target_codes()で対象コード一覧のみを
取得してワーカーへ配布し、各ワーカーがfetch_screening_data()で自分の1銘柄分
だけデータを取得する(全銘柄分を一括取得してfan-outの意味を失わせない)。
CLI(dry-run/通常実行とも単一プロセス同期実行)は両メソッドを順に呼べばよい。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from jstock_advisor.infrastructure.local_repository.holding_repository import HoldingRepository
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
)
from jstock_advisor.interfaces.candidate_universe import CandidateUniverseProvider
from jstock_advisor.services.screening_data_provider import (
    ScreeningDataProvider,
    ScreeningDataResult,
)


@dataclass(frozen=True)
class CollectorResult:
    stock_codes: list[str]
    universe_count: int
    duplicate_count: int
    invalid_code_count: int
    holding_excluded_count: int
    watchlist_excluded_count: int


class WatchlistCandidateCollector:
    def __init__(
        self,
        universe_provider: CandidateUniverseProvider,
        screening_data_provider: ScreeningDataProvider,
        holding_repository: HoldingRepository | None = None,
        watchlist_repository: WatchlistRepository | None = None,
    ) -> None:
        self._universe_provider = universe_provider
        self._screening_data_provider = screening_data_provider
        self._holdings = holding_repository or HoldingRepository()
        self._watchlist = watchlist_repository or WatchlistRepository()

    def collect_target_codes(self) -> CollectorResult:
        universe = self._universe_provider.get_candidate_universe()
        held_codes = {h.stock_code for h in self._holdings.list_all()}
        watchlisted_codes = {w.stock_code for w in self._watchlist.list_all()}

        targets: list[str] = []
        holding_excluded = 0
        watchlist_excluded = 0
        for code in universe.stock_codes:
            if code in held_codes:
                holding_excluded += 1
                continue
            if code in watchlisted_codes:
                watchlist_excluded += 1
                continue
            targets.append(code)

        return CollectorResult(
            stock_codes=targets,
            universe_count=len(universe.stock_codes),
            duplicate_count=universe.duplicate_count,
            invalid_code_count=universe.invalid_code_count,
            holding_excluded_count=holding_excluded,
            watchlist_excluded_count=watchlist_excluded,
        )

    def fetch_screening_data(self, stock_code: str, now: dt.datetime) -> ScreeningDataResult:
        return self._screening_data_provider.get_screening_input(stock_code, now)
