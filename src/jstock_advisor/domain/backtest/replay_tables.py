"""最小セル数の規則と、表の作成(Issue #880 R1a の a-6)。

純粋関数。★ 最小セル数は引数(事前登録の値。既定なし)★。セルの件数が最小に満たないとき、
割合・平均・中央値を出さず、件数だけを報告する(過剰解釈の防止。統計的有意は主張しない)。

『測れないものを合格基準に入れない』(AC-5)の実装: 値を作れない(件数が小さい・分母が 0)ときは、
空欄ではなく『n が小さい』という状態(`suppressed = True`)を明示して返す。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from jstock_advisor.domain.backtest.errors import ReplayInputError, require_int


@dataclass(frozen=True)
class CellSummary:
    """数値のセルの要約。suppressed のとき、平均・中央値は None(件数だけ)。"""

    n: int
    suppressed: bool
    mean: Decimal | None = None
    median: Decimal | None = None


def summarize(values: Sequence[Decimal], *, min_cell: int) -> CellSummary:
    """件数が min_cell 以上のときだけ、平均と中央値を出す。"""
    require_int(min_cell, "min_cell", minimum=1)
    if not all(isinstance(v, Decimal) and v.is_finite() for v in values):
        raise ReplayInputError("values must be finite Decimal")
    n = len(values)
    if n < min_cell:
        return CellSummary(n=n, suppressed=True)
    ordered = sorted(values)
    middle = n // 2
    median = ordered[middle] if n % 2 else (ordered[middle - 1] + ordered[middle]) / Decimal(2)
    return CellSummary(
        n=n, suppressed=False, mean=sum(values, Decimal(0)) / Decimal(n), median=median
    )


@dataclass(frozen=True)
class RateCell:
    """件数のセル。rate は件数が最小以上で分母が 0 でないときだけ(None = n が小さい / 分母 0)。"""

    label: str
    n: int
    suppressed: bool
    rate: Fraction | None = None


def rate_table(counts: Mapping[str, int], *, min_cell: int) -> tuple[RateCell, ...]:
    """分類ごとの件数から、割合を出す(ラベルの昇順)。

    割合は『そのセルの件数が min_cell 以上』のときだけ出す(セルの件数 ≦ 合計なので、合計が
    小さいときも出ない)。それ以外は件数のみ(`suppressed = True`)。
    """
    require_int(min_cell, "min_cell", minimum=1)
    for label, count in counts.items():
        if not isinstance(label, str) or not label:
            raise ReplayInputError("labels must be non-empty strings")
        require_int(count, "count", minimum=0)
    total = sum(counts.values())
    cells: list[RateCell] = []
    for label in sorted(counts):
        n = counts[label]
        show = n >= min_cell  # n <= total のため、合計が最小未満なら必ず出さない
        cells.append(
            RateCell(
                label=label, n=n, suppressed=not show, rate=Fraction(n, total) if show else None
            )
        )
    return tuple(cells)
