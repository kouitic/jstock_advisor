"""資金入替の観測の『読取の結果』の型(Issue #604 P-b2。#128 R2。dormant)。

reader(infrastructure)と入力の組み立て(services)が共有する、読取の結果の値の型。
**標準ライブラリと domain だけ**(時計・I/O・AWS に触れない)。どこからも import されない(dormant)。
設計: #604 P-b 設計 rev6(…6169320796)。★ 識別子・値・例外の文面を持たない ★。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from jstock_advisor.domain.signals.rotation_source_eligibility import (
    HoldingEvaluationFacts,
    RecommendationFacts,
)


class RotationInputsContractError(ValueError):
    """ports の戻り値・引数が契約に反する(組み立て側の誤り)。値を含めない。"""


def _fail(problem: str) -> RotationInputsContractError:
    return RotationInputsContractError(problem)


class RecordStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    FAILED = "FAILED"


@dataclass(frozen=True)
class RecordRead:
    """保有 1 件の評価記録の読取の結果。FOUND だけが事実を持つ。"""

    status: RecordStatus
    facts: HoldingEvaluationFacts | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, RecordStatus):
            raise _fail("status must be a RecordStatus")
        if (self.status is RecordStatus.FOUND) != (self.facts is not None):
            raise _fail("facts exist exactly when the record was found")
        if self.facts is not None and not isinstance(self.facts, HoldingEvaluationFacts):
            raise _fail("facts must be HoldingEvaluationFacts")


@dataclass(frozen=True)
class RecommendationsRead:
    """Recommendation の読取の結果。`found` = 引けて事実になったもの / `failed` = 読めなかった id。

    どちらにも無い id は『存在しない』(P-a が RECOMMENDATION_MISSING にする)。
    """

    found: Mapping[str, RecommendationFacts] = field(default_factory=dict)
    failed: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.found, Mapping) or not isinstance(self.failed, frozenset):
            raise _fail("found must be a mapping and failed a frozenset")
        if set(self.found) & self.failed:
            raise _fail("an id cannot be both found and failed")
        object.__setattr__(self, "found", MappingProxyType(dict(self.found)))
