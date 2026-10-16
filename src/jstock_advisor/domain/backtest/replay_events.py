"""比較の単位(観測 → event → 保有)・母集団の勘定・同時発火率(Issue #880 R1a の a-1・a-2・a-5)。

純粋関数。時計・I/O なし。値の既定なし(event の切り方の間隔は引数 = 事前登録の値)。

## 3 層(混ぜない)

  観測(observation)  (保有, 評価日)の 1 行。件数・遷移・同時発火の分母。独立なサンプルとして扱わない
  event              同じ保有の同じ局面。統計・転帰の単位
  保有(holding)      クラスタ。母集団は保有数で、event 数が多くても保有数は小さい
                     (報告には event 数と保有数を必ず併記する = AC-11)

★ 『同じ局面』の厳密な境界は事前登録の対象(値ではなく定義)。この module は、呼び出し側が
登録された定義で作った局面の鍵(`phase_key`)と間隔(`max_gap`)を受けて、連続した観測を
1 つの event にまとめるだけ。
局面の鍵の作り方(peak・fact_key・ceiling 等)は持たない。

## 識別子

`holding_ref` は仮名化済みの参照(ソルト付きハッシュ)。実在の識別子・銘柄・金額・数量を持たない。
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from jstock_advisor.domain.backtest.errors import ReplayInputError, require_int, require_text


class ExclusionReason(StrEnum):
    """分析対象から外す理由(#846 rev2.3 の T1 の 4 区分 + データ不足)。分母から外して報告する。"""

    LEGACY = "LEGACY"  # a: 旧形式の行
    EVALUATED_AT_MISMATCH = "EVALUATED_AT_MISMATCH"  # b: evaluated_at が結合できない
    BEFORE_PLAN = "BEFORE_PLAN"  # c: plan の計算前(原因別には分けられない)
    BASELINE_INTEGRITY_ERROR = "BASELINE_INTEGRITY_ERROR"  # d: baseline の整合性エラー
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"


#: 勘定の表示の固定の順序(件数 0 の理由も省略しない)
EXCLUSION_ORDER: tuple[ExclusionReason, ...] = tuple(ExclusionReason)


@dataclass(frozen=True)
class Observation:
    """(保有, 評価日)の 1 行。

    holding_ref          仮名化済みの保有の参照
    day_index            営業日の索引(呼び出し側が渡す。時計・カレンダーをここでは使わない)
    phase_kind           局面の種別(登録された語彙の文字列)
    phase_key            『同じ局面』を表す鍵(登録された定義で作る)。None = 局面に属さない
    exclusion            分析対象から外す理由(None = 対象)
    is_first_evaluation  初回評価の flag(外さず、件数だけ別に数える)
    held_out             症例(held-out)。集計・閾値の決定に使わず、別枠で数える
    """

    holding_ref: str
    day_index: int
    phase_kind: str
    phase_key: str | None = None
    exclusion: ExclusionReason | None = None
    is_first_evaluation: bool = False
    held_out: bool = False

    def __post_init__(self) -> None:
        require_text(self.holding_ref, "holding_ref")
        require_int(self.day_index, "day_index", minimum=0)
        require_text(self.phase_kind, "phase_kind")
        if self.phase_key is not None:
            require_text(self.phase_key, "phase_key")
        if self.exclusion is not None and not isinstance(self.exclusion, ExclusionReason):
            raise ReplayInputError("exclusion must be an ExclusionReason or None")
        if not isinstance(self.is_first_evaluation, bool) or not isinstance(self.held_out, bool):
            raise ReplayInputError("flags must be bool")

    @property
    def analyzed(self) -> bool:
        """分析対象 = 除外されておらず、症例でもない。"""
        return self.exclusion is None and not self.held_out


# --- a-2 母集団の勘定 -------------------------------------------------------------------------


@dataclass(frozen=True)
class PopulationAccount:
    """全行 → 除外(理由別)・症例 → 分析対象。件数の合計が必ず一致する。"""

    total: int
    excluded: tuple[tuple[ExclusionReason, int], ...]  # 固定の順序・件数 0 も含む
    held_out: int
    analyzed: int
    first_evaluations: int  # 分析対象のうち初回評価(外さず数える)

    @property
    def excluded_total(self) -> int:
        return sum(count for _, count in self.excluded)


def account_population(rows: Iterable[Observation]) -> PopulationAccount:
    """『全行 → 除外(理由別)・症例 → 分析対象』を件数で示す。理由別の件数を省略しない。

    除外が症例より先(除外された症例は除外として数える)。total = excluded + held_out + analyzed。
    """
    total = 0
    excluded = dict.fromkeys(EXCLUSION_ORDER, 0)
    held_out = analyzed = first = 0
    for row in rows:
        if not isinstance(row, Observation):
            raise ReplayInputError("rows must be Observation")
        total += 1
        if row.exclusion is not None:
            excluded[row.exclusion] += 1
        elif row.held_out:
            held_out += 1
        else:
            analyzed += 1
            first += int(row.is_first_evaluation)
    return PopulationAccount(
        total=total,
        excluded=tuple((reason, excluded[reason]) for reason in EXCLUSION_ORDER),
        held_out=held_out,
        analyzed=analyzed,
        first_evaluations=first,
    )


# --- a-1 event ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """同じ保有の同じ局面が連続した期間 1 件。"""

    holding_ref: str
    phase_kind: str
    phase_key: str
    first_day_index: int
    last_day_index: int
    observations: int


def build_events(rows: Iterable[Observation], *, max_gap: int) -> tuple[Event, ...]:
    """分析対象の観測を、(保有, 局面の種別, 局面の鍵)ごとに、連続した期間へまとめる。

    max_gap  同じ局面の観測の間で『連続』と見なす営業日の最大の間隔(登録された値。既定なし)。
             間隔が max_gap を超えたら別の event(同じ局面の鍵でも、離れていれば切る)。
    局面の鍵が None の観測・除外された観測・症例は event にしない。出力は固定の順序。
    """
    require_int(max_gap, "max_gap", minimum=1)
    groups: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for row in rows:
        if not isinstance(row, Observation):
            raise ReplayInputError("rows must be Observation")
        if row.analyzed and row.phase_key is not None:
            groups[(row.holding_ref, row.phase_kind, row.phase_key)].append(row.day_index)
    events: list[Event] = []
    for (holding, kind, key), days in groups.items():
        run = sorted(days)
        start = previous = run[0]
        count = 1
        for day in run[1:]:
            if day - previous > max_gap:
                events.append(Event(holding, kind, key, start, previous, count))
                start, count = day, 0
            previous = day
            count += 1
        events.append(Event(holding, kind, key, start, previous, count))
    return tuple(
        sorted(events, key=lambda e: (e.holding_ref, e.phase_kind, e.first_day_index, e.phase_key))
    )


@dataclass(frozen=True)
class LayerCounts:
    """3 層の件数(AC-11: event 数と保有数を併記する)。"""

    observations: int
    events: int
    holdings: int


def layer_counts(rows: Iterable[Observation], events: Sequence[Event]) -> LayerCounts:
    """分析対象の観測数・event 数・保有数。events は同じ rows から作った `build_events` の結果。"""
    analyzed = [row for row in rows if isinstance(row, Observation) and row.analyzed]
    return LayerCounts(
        observations=len(analyzed),
        events=len(events),
        holdings=len({row.holding_ref for row in analyzed}),
    )


# --- a-5 同時発火率 ---------------------------------------------------------------------------


class Fire(StrEnum):
    """ある層がその日に悪化(発火)したか。UNDETERMINED は欠損として落とさない(C-7)。"""

    FIRED = "FIRED"
    NOT_FIRED = "NOT_FIRED"
    UNDETERMINED = "UNDETERMINED"


@dataclass(frozen=True)
class CoFireCounts:
    """2 つの層の同じ観測日の 2×2 件数 + どちらかが UNDETERMINED の件数(別に数える)。"""

    both: int
    only_first: int
    only_second: int
    neither: int
    undetermined: int

    @property
    def total(self) -> int:
        return self.both + self.only_first + self.only_second + self.neither + self.undetermined

    @property
    def determined(self) -> int:
        return self.total - self.undetermined


def co_fire_counts(pairs: Iterable[tuple[Fire, Fire]]) -> CoFireCounts:
    """同じ観測日に、2 つの層(例: L2 の悪化と L3 の悪化)が両方成立した件数の勘定。"""
    both = first = second = neither = undetermined = 0
    for pair in pairs:
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not all(isinstance(item, Fire) for item in pair)
        ):
            raise ReplayInputError("pairs must be (Fire, Fire)")
        a, b = pair
        if Fire.UNDETERMINED in pair:
            undetermined += 1
        elif a is Fire.FIRED and b is Fire.FIRED:
            both += 1
        elif a is Fire.FIRED:
            first += 1
        elif b is Fire.FIRED:
            second += 1
        else:
            neither += 1
    return CoFireCounts(both, first, second, neither, undetermined)


def phi_coefficient(counts: CoFireCounts) -> float | None:
    """2×2 の相関(phi)。いずれかの周辺が 0(分散なし)のときは None(値を作らない)。"""
    a, b, c, d = counts.both, counts.only_first, counts.only_second, counts.neither
    row1, row2, col1, col2 = a + b, c + d, a + c, b + d
    if 0 in (row1, row2, col1, col2):
        return None
    return (a * d - b * c) / math.sqrt(row1 * row2 * col1 * col2)
