"""ウォッチリスト自動追加: 候補銘柄の収集(ユニバース取得+事前除外+ローテーション選択)。

CandidateUniverseProviderから対象銘柄コード一覧を取得し、保有銘柄・既存
ウォッチリスト銘柄を事前除外する。各銘柄のスクリーニング用データ取得は
ScreeningDataProviderへ委譲する(合否判定はWatchlistScreeningService/
ScreeningPolicyの責務であり、ここでは一切行わない)。

Lambda fan-out時は、dispatch側がcollect_target_codes()で対象コード一覧のみを
取得してワーカーへ配布し、各ワーカーがfetch_screening_data()で自分の1銘柄分
だけデータを取得する(全銘柄分を一括取得してfan-outの意味を失わせない)。
CLI(dry-run/通常実行とも単一プロセス同期実行)は両メソッドを順に呼べばよい。

ウォッチリスト自動運用の改善(永続ラウンドロビン方式、2026-08)で、除外順序と
候補選定方式を変更した:

- 除外順序: 従来は「市場区分フィルタ→candidate_limit件数スライス→保有/
  ウォッチリスト除外」の順だったため、300件の枠の一部が既存除外対象で
  無駄になっていた。「市場区分フィルタ→保有除外→ウォッチリスト除外→
  安定ソート→ローテーション選択(最大candidate_limit件)」の順へ変更し、
  毎回最大candidate_limit件の未評価候補を選べるようにした。
- 候補選定: rotation_enabled=Trueの場合、`select_rotation_window()`が
  永続カーソル(RotationState.last_stock_code、infrastructure/aws/
  watchlist_rotation_state.py)を起点に、除外後ユニバースを安定ソート
  (market_segment, stock_code)した上で巡回選択する。カーソルが現在の
  ユニバースに存在しなくても`bisect.bisect_right`で直後から継続でき、
  末尾に達したら先頭へラップする(保証範囲の詳細はdocs/functional_spec.md
  5.7節参照)。rotation_enabled=Falseの場合は従来の固定スライス方式へ
  フォールバックする(移行時の安全弁)。
"""

from __future__ import annotations

import bisect
import datetime as dt
from dataclasses import dataclass

from jstock_advisor.config.models import StagedRolloutConfig
from jstock_advisor.infrastructure.local_repository.holding_repository import HoldingRepository
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
)
from jstock_advisor.interfaces.candidate_universe import (
    CandidateUniverseItem,
    CandidateUniverseProvider,
)
from jstock_advisor.services.screening_data_provider import (
    ScreeningDataProvider,
    ScreeningDataResult,
)

RotationCursor = tuple[str, str]
"""(market_segment, stock_code)。market_segmentがNoneのitemは""として扱う(_sort_key参照)。"""


@dataclass(frozen=True)
class CollectorResult:
    stock_codes: list[str]
    universe_count: int
    duplicate_count: int
    invalid_code_count: int
    holding_excluded_count: int
    watchlist_excluded_count: int
    # --- 候補ユニバース本格対応(2026-08)で追加。ローテーション導入(2026-08、
    # 除外順序変更)により、市場区分フィルタによる除外件数のみを表す(以前は
    # candidate_limitスライスによる除外もここに合算されていたが、その概念は
    # rotation_eligible_excluded_count/rotation_windowへ分離した)。
    staged_rollout_excluded_count: int = 0

    # --- ウォッチリスト自動運用の改善(ローテーション、2026-08)で追加 ---
    # 市場区分フィルタ+保有除外+ウォッチリスト除外後、rotation選択の母集団と
    # なった件数(=このうち最大candidate_limit件がstock_codesとして選ばれる)。
    eligible_universe_count: int = 0
    rotation_cursor_before: RotationCursor | None = None
    rotation_cursor_after: RotationCursor | None = None
    rotation_wrapped: bool = False


@dataclass(frozen=True)
class RotationSelection:
    items: list[CandidateUniverseItem]
    new_cursor: RotationCursor | None
    wrapped: bool


def _sort_key(item: CandidateUniverseItem) -> RotationCursor:
    """安定ソートキー。market_segmentの意味づけは無く、決定的な全順序である
    ことのみが要件(計画Part A-2参照)。Noneとの比較エラーを避けるため""化する。
    """
    return (item.market_segment or "", item.stock_code)


def select_rotation_window(
    items: list[CandidateUniverseItem],
    candidate_limit: int,
    cursor: RotationCursor | None,
) -> RotationSelection:
    """永続ラウンドロビン方式による候補選択(計画Part A-3の疑似コードの実装)。

    `cursor`より後の位置(`bisect.bisect_right`)から最大`candidate_limit`件を
    選ぶ。末尾に達したら先頭へラップする。`items`が`candidate_limit`件未満でも
    同一呼び出し内で同じ銘柄を2回選ぶことはない(停止条件`len(selected) <
    len(sorted_items)`)。

    保証すること/しないことは計画Part A-3参照(重複ゼロは保証しない、
    永久未評価の回避と次サイクルまでの評価は保証する)。
    """
    sorted_items = sorted(items, key=_sort_key)
    keys = [_sort_key(i) for i in sorted_items]

    start_index = 0 if cursor is None else bisect.bisect_right(keys, cursor)

    selected: list[CandidateUniverseItem] = []
    idx = start_index
    wrapped = False
    while len(selected) < candidate_limit and len(selected) < len(sorted_items):
        if idx >= len(sorted_items):
            idx = 0
            wrapped = True
        selected.append(sorted_items[idx])
        idx += 1

    new_cursor = _sort_key(selected[-1]) if selected else cursor
    return RotationSelection(items=selected, new_cursor=new_cursor, wrapped=wrapped)


class WatchlistCandidateCollector:
    def __init__(
        self,
        universe_provider: CandidateUniverseProvider,
        screening_data_provider: ScreeningDataProvider,
        holding_repository: HoldingRepository | None = None,
        watchlist_repository: WatchlistRepository | None = None,
        staged_rollout: StagedRolloutConfig | None = None,
        rotation_enabled: bool = False,
    ) -> None:
        self._universe_provider = universe_provider
        self._screening_data_provider = screening_data_provider
        self._holdings = holding_repository or HoldingRepository()
        self._watchlist = watchlist_repository or WatchlistRepository()
        self._staged_rollout = staged_rollout
        self._rotation_enabled = rotation_enabled

    def collect_target_codes(
        self, rotation_cursor: RotationCursor | None = None
    ) -> CollectorResult:
        universe = self._universe_provider.get_candidate_universe()

        # 1. 市場区分フィルタ(段階導入設定、15節)。除外順序変更(2026-08)により
        # ここではcandidate_limitのスライスは行わない。
        items = universe.items
        market_segment_filter = (
            self._staged_rollout.market_segment_filter if self._staged_rollout else None
        )
        if market_segment_filter is not None:
            allowed = set(market_segment_filter)
            before_count = len(items)
            items = [item for item in items if item.market_segment in allowed]
            staged_rollout_excluded = before_count - len(items)
        else:
            staged_rollout_excluded = 0

        # 2. 保有銘柄・既存ウォッチリスト銘柄の除外(candidate_limit適用より前、
        # 2026-08の除外順序変更)。
        held_codes = {h.stock_code for h in self._holdings.list_all()}
        watchlisted_codes = {w.stock_code for w in self._watchlist.list_all()}
        eligible_items: list[CandidateUniverseItem] = []
        holding_excluded = 0
        watchlist_excluded = 0
        for item in items:
            if item.stock_code in held_codes:
                holding_excluded += 1
                continue
            if item.stock_code in watchlisted_codes:
                watchlist_excluded += 1
                continue
            eligible_items.append(item)

        candidate_limit = self._staged_rollout.candidate_limit if self._staged_rollout else None

        rotation_cursor_after: RotationCursor | None = None
        rotation_wrapped = False
        if self._rotation_enabled and candidate_limit is not None:
            selection = select_rotation_window(eligible_items, candidate_limit, rotation_cursor)
            target_items = selection.items
            rotation_cursor_after = selection.new_cursor
            rotation_wrapped = selection.wrapped
        elif candidate_limit is not None:
            # rotation.enabled=falseの場合の後方互換フォールバック(移行時の安全弁)。
            target_items = eligible_items[:candidate_limit]
        else:
            target_items = eligible_items

        return CollectorResult(
            stock_codes=[item.stock_code for item in target_items],
            universe_count=len(universe.stock_codes),
            duplicate_count=universe.duplicate_count,
            invalid_code_count=universe.invalid_code_count,
            holding_excluded_count=holding_excluded,
            watchlist_excluded_count=watchlist_excluded,
            staged_rollout_excluded_count=staged_rollout_excluded,
            eligible_universe_count=len(eligible_items),
            rotation_cursor_before=rotation_cursor,
            rotation_cursor_after=rotation_cursor_after,
            rotation_wrapped=rotation_wrapped,
        )

    def fetch_screening_data(self, stock_code: str, now: dt.datetime) -> ScreeningDataResult:
        return self._screening_data_provider.get_screening_input(stock_code, now)
