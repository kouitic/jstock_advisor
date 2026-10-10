"""三値の判定(値 / UNDETERMINED)。値を捏造しないための型(Issue #878 PR-1 = C0)。

#601 / #602(Expected Return / RAER)のように未実装・未承認の評価軸は UNDETERMINED とし、
**根拠にも否定の根拠にも数えない**。そのため真偽値への変換は禁止する
(`if determination:` が UNDETERMINED を False として扱うことを型の側で防ぐ)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from jstock_advisor.domain.exit_architecture.vocabulary import UndeterminedReason


class UndeterminedError(ValueError):
    """UNDETERMINED から値を取り出そうとした。"""


@dataclass(frozen=True)
class Determination[T]:
    """確定した値、または UNDETERMINED(理由つき)のどちらか一方。"""

    value: T | None = None
    reason: UndeterminedReason | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.reason is None and self.value is None:
            raise ValueError("確定した値か UNDETERMINED の理由のどちらかが要る")
        if self.reason is not None and self.value is not None:
            raise ValueError("値と UNDETERMINED の理由は同時に持てない")

    @classmethod
    def of(cls, value: T) -> Determination[T]:
        if value is None:
            raise ValueError("None は確定した値ではない。undetermined() を使う")
        return cls(value=value)

    @classmethod
    def undetermined(cls, reason: UndeterminedReason, detail: str = "") -> Determination[T]:
        return cls(reason=reason, detail=detail)

    @property
    def is_determined(self) -> bool:
        return self.reason is None

    def unwrap(self) -> T:
        if self.value is None:
            raise UndeterminedError(f"UNDETERMINED({self.reason}): 値は無い")
        return self.value

    def __bool__(self) -> NoReturn:
        # UNDETERMINED を False と読ませない。is_determined / unwrap() で明示的に扱う。
        raise TypeError("Determination は真偽値に変換できない(is_determined を使う)")
