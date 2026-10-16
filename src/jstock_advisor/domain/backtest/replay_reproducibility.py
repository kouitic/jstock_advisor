"""replay が現行の判定を再現するかの確認の判定部分(Issue #880 R1a の a-8。AC-1・C-2)。

純粋関数。★ 『同じ保存 facts から再計算した現行の判定』は呼び出し側が渡す ★(rev2.1 で確定: 現行の
module も NEW の module も import しない)。ここは『記録された判定』と『再計算した判定』の対を数え、
一致率と、先へ進めるかを判定するだけ。

不一致なら比較に進まない(C-2): 一致の必要な割合は登録された値(引数。既定なし)。
対が 0 件のときは『確認できていない』= 進めない(空を合格にしない)。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction

from jstock_advisor.domain.backtest.errors import ReplayInputError


@dataclass(frozen=True)
class Reproducibility:
    """再現性の勘定。mismatches = (記録, 再計算, 件数)の昇順。"""

    total: int
    matched: int
    mismatches: tuple[tuple[str, str, int], ...]

    @property
    def mismatched(self) -> int:
        return self.total - self.matched

    @property
    def rate(self) -> Fraction | None:
        """一致率。対が 0 件のとき None(値を作らない)。"""
        return Fraction(self.matched, self.total) if self.total else None


def reproducibility(pairs: Iterable[tuple[str, str]]) -> Reproducibility:
    """(記録された判定, 再計算した判定)の対を数える。判定は名前の文字列。"""
    total = matched = 0
    mismatches: Counter[tuple[str, str]] = Counter()
    for pair in pairs:
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not all(isinstance(item, str) and item for item in pair)
        ):
            raise ReplayInputError("pairs must be (recorded, recomputed) strings")
        total += 1
        if pair[0] == pair[1]:
            matched += 1
        else:
            mismatches[pair] += 1
    return Reproducibility(
        total=total,
        matched=matched,
        mismatches=tuple((a, b, n) for (a, b), n in sorted(mismatches.items())),
    )


def may_proceed(result: Reproducibility, *, required: Fraction) -> bool:
    """比較に進んでよいか。対が 0 件なら False。required は登録された一致率(0〜1。既定なし)。"""
    if not isinstance(required, Fraction) or not 0 <= required <= 1:
        raise ReplayInputError("required must be a Fraction in [0, 1]")
    rate = result.rate
    return rate is not None and rate >= required
