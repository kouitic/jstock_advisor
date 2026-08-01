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

from jstock_advisor.config.models import StagedRolloutConfig
from jstock_advisor.infrastructure.local_repository.holding_repository import HoldingRepository
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
)
from jstock_advisor.interfaces.candidate_universe import (
    CandidateUniverseProvider,
    CandidateUniverseResult,
)
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
    # --- 候補ユニバース本格対応(2026-08)で追加。段階導入(15節)で絞り込んだ後の
    # 件数を、絞り込み前のuniverse_countと区別して監査・ログへ残せるようにする。
    staged_rollout_excluded_count: int = 0


def _apply_staged_rollout_filter(
    universe: CandidateUniverseResult, staged_rollout: StagedRolloutConfig
) -> tuple[list[str], int]:
    """段階導入設定(15節)を候補リストへ適用する。

    market_segment_filterが設定されている場合はまず市場区分で絞り込み、
    その後candidate_limitで件数上限を適用する(出現順の先頭からcandidate_limit件)。
    段階的な実測作業(100→500→プライムのみ→全件)専用の設定であり、本番の
    スクリーニング結果の優劣付けには使わない(絞り込み順序に評価上の意味は無い)。
    """
    items = universe.items
    if staged_rollout.market_segment_filter is not None:
        allowed = set(staged_rollout.market_segment_filter)
        items = [item for item in items if item.market_segment in allowed]
    if staged_rollout.candidate_limit is not None:
        items = items[: staged_rollout.candidate_limit]
    excluded_count = len(universe.items) - len(items)
    return [item.stock_code for item in items], excluded_count


class WatchlistCandidateCollector:
    def __init__(
        self,
        universe_provider: CandidateUniverseProvider,
        screening_data_provider: ScreeningDataProvider,
        holding_repository: HoldingRepository | None = None,
        watchlist_repository: WatchlistRepository | None = None,
        staged_rollout: StagedRolloutConfig | None = None,
    ) -> None:
        self._universe_provider = universe_provider
        self._screening_data_provider = screening_data_provider
        self._holdings = holding_repository or HoldingRepository()
        self._watchlist = watchlist_repository or WatchlistRepository()
        self._staged_rollout = staged_rollout

    def collect_target_codes(self) -> CollectorResult:
        universe = self._universe_provider.get_candidate_universe()
        held_codes = {h.stock_code for h in self._holdings.list_all()}
        watchlisted_codes = {w.stock_code for w in self._watchlist.list_all()}

        if self._staged_rollout is not None:
            candidate_codes, staged_rollout_excluded = _apply_staged_rollout_filter(
                universe, self._staged_rollout
            )
        else:
            candidate_codes, staged_rollout_excluded = universe.stock_codes, 0

        targets: list[str] = []
        holding_excluded = 0
        watchlist_excluded = 0
        for code in candidate_codes:
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
            staged_rollout_excluded_count=staged_rollout_excluded,
        )

    def fetch_screening_data(self, stock_code: str, now: dt.datetime) -> ScreeningDataResult:
        return self._screening_data_provider.get_screening_input(stock_code, now)
