"""OLD と NEW の判定の分類(Issue #880 R1a の a-3)。

純粋関数。★ NEW の判定結果は『引数』で受ける ★(rev2.1。#877 / #878 / #882 の module を
import しない)。
入力の型(`Judgement`)は素の値の組で、NEW の `Decision` から作れることはテスト側が固定する
(rev2.2 の SHOULD-1。本 module は import しない)。

## 分類(一致 / 新のみ / 現行のみ / 強さの差 / class の差)

  MATCH          同じ action で、同じ class(HOLD どうしは class を見ない)
  NEW_ONLY       OLD は HOLD・NEW は売却(PARTIAL / FULL)
  OLD_ONLY       OLD は売却・NEW は HOLD
  STRENGTH_DIFF  どちらも売却だが action が違う(PARTIAL と FULL)
  CLASS_DIFF     同じ売却の action で、class が違う
  UNDETERMINED   NEW が評価できなかった(★ HOLD にも一致にも数えない ★。C-7)
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from jstock_advisor.domain.backtest.errors import ReplayInputError, require_text


class ReplayAction(StrEnum):
    """判定の action。NEW の `ExitAction` と同じ値(契約テストが突き合わせる)。"""

    HOLD = "HOLD"
    PARTIAL = "PARTIAL"
    FULL = "FULL"


class Difference(StrEnum):
    MATCH = "MATCH"
    NEW_ONLY = "NEW_ONLY"
    OLD_ONLY = "OLD_ONLY"
    STRENGTH_DIFF = "STRENGTH_DIFF"
    CLASS_DIFF = "CLASS_DIFF"
    UNDETERMINED = "UNDETERMINED"


@dataclass(frozen=True)
class Judgement:
    """1 件の判定(OLD は記録された判定・NEW は引数で渡された判定結果)。

    exit_class     class の名前(HOLD のときは None でよい)
    undetermined   評価できなかった(NEW だけが取りうる)。True のとき action は HOLD
    """

    action: ReplayAction
    exit_class: str | None = None
    undetermined: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.action, ReplayAction):
            raise ReplayInputError("action must be a ReplayAction")
        if self.exit_class is not None:
            require_text(self.exit_class, "exit_class")
        if not isinstance(self.undetermined, bool):
            raise ReplayInputError("undetermined must be bool")
        if self.undetermined and self.action is not ReplayAction.HOLD:
            raise ReplayInputError("an undetermined judgement carries no action")


def classify_pair(old: Judgement, new: Judgement) -> Difference:
    """OLD と NEW の 1 対を分類する。OLD の undetermined は契約違反(記録は確定している)。"""
    if not isinstance(old, Judgement) or not isinstance(new, Judgement):
        raise ReplayInputError("old and new must be Judgement")
    if old.undetermined:
        raise ReplayInputError("a recorded (old) judgement cannot be undetermined")
    if new.undetermined:
        return Difference.UNDETERMINED
    if old.action is new.action:
        if old.action is ReplayAction.HOLD or old.exit_class == new.exit_class:
            return Difference.MATCH
        return Difference.CLASS_DIFF
    if old.action is ReplayAction.HOLD:
        return Difference.NEW_ONLY
    if new.action is ReplayAction.HOLD:
        return Difference.OLD_ONLY
    return Difference.STRENGTH_DIFF


def transition_label(old: Judgement, new: Judgement) -> str:
    """action の遷移(例: FULL->PARTIAL)。NEW が undetermined のときは UNDETERMINED。"""
    if new.undetermined:
        return "UNDETERMINED"
    return f"{old.action.value}->{new.action.value}"


def difference_counts(
    pairs: Iterable[tuple[Judgement, Judgement]],
) -> tuple[tuple[Difference, int], ...]:
    """分類ごとの件数(固定の順序・件数 0 も省略しない)。"""
    counts = dict.fromkeys(Difference, 0)
    for old, new in pairs:
        counts[classify_pair(old, new)] += 1
    return tuple((kind, counts[kind]) for kind in Difference)


@dataclass(frozen=True)
class TransitionMatrix:
    """OLD action × NEW action の遷移行列(全 9 マス)+ NEW が UNDETERMINED の件数(別に数える)。"""

    cells: tuple[tuple[ReplayAction, ReplayAction, int], ...]
    undetermined: int

    @property
    def total(self) -> int:
        return sum(count for _, _, count in self.cells) + self.undetermined


def transition_matrix(pairs: Iterable[tuple[Judgement, Judgement]]) -> TransitionMatrix:
    """遷移行列。UNDETERMINED は HOLD の列へ入れず、別に数える(欠損として落とさない)。"""
    grid = {(o, n): 0 for o in ReplayAction for n in ReplayAction}
    undetermined = 0
    for old, new in pairs:
        classify_pair(old, new)  # 入力の検査
        if new.undetermined:
            undetermined += 1
        else:
            grid[(old.action, new.action)] += 1
    return TransitionMatrix(
        cells=tuple((o, n, grid[(o, n)]) for o in ReplayAction for n in ReplayAction),
        undetermined=undetermined,
    )


@dataclass(frozen=True)
class ReasonCounts:
    """理由コードごとの件数。登録された語彙にないコードは『認識できない』として数える(落とさない)。"""

    counts: tuple[tuple[str, int], ...]  # 登録された語彙の昇順・件数 0 も含む
    unrecognized: int


def reason_counts(codes: Iterable[str], vocabulary: frozenset[str]) -> ReasonCounts:
    """差の理由コードの件数。vocabulary は登録された理由コードの集合(引数。既定なし)。"""
    if not isinstance(vocabulary, frozenset) or not all(isinstance(v, str) for v in vocabulary):
        raise ReplayInputError("vocabulary must be a frozenset[str]")
    counts = dict.fromkeys(sorted(vocabulary), 0)
    unrecognized = 0
    for code in codes:
        if isinstance(code, str) and code in counts:
            counts[code] += 1
        else:
            unrecognized += 1
    return ReasonCounts(counts=tuple(counts.items()), unrecognized=unrecognized)
