"""根拠(evidence)の型と、独立性の規則の純粋関数(Issue #878 PR-1 = C0)。

独立性の規則(#846 の R-A〜R-D)のうち、型の側で表せるものだけを置く。
  R-A 同じ root の根拠は何件あっても 1 と数える(distinct_roots)
  R-D 同じ事実(fact_key)は 1 件に統合する(dedupe_by_fact_key。E1 と E3 の共通根拠など)
Arbiter 本体(強さの判定・cap・降格)は #878 の PR-2 であり、ここには置かない。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from jstock_advisor.domain.exit_architecture.vocabulary import RootFactor


class EvidenceStatus(StrEnum):
    """根拠の確からしさ。推定のみ(SUSPECTED)は、単独では独立根拠に数えない。"""

    TRIGGERED = "TRIGGERED"  # 評価済みで成立
    SUSPECTED = "SUSPECTED"  # 推定のみ(一次情報で未確認)
    NOT_EVALUATED = "NOT_EVALUATED"  # 評価できなかった(根拠にも否定にもならない)


@dataclass(frozen=True)
class Evidence:
    """1 つの根拠。root_factor は必須(付け忘れは型エラー)。"""

    root_factor: RootFactor
    source: str  # 出典(item / category / reason_code / rule_id など)
    fact_key: str  # 正規化した事実の識別子。同じ事実は同じ fact_key(R-D)
    status: EvidenceStatus = EvidenceStatus.TRIGGERED
    primary_source_confirmed: bool = False
    event_id: str | None = None  # 複数の層に影響する経済的事象の識別子(R-D)

    def __post_init__(self) -> None:
        if not isinstance(self.root_factor, RootFactor):
            raise TypeError("root_factor は RootFactor でなければならない")
        if not self.source.strip():
            raise ValueError("source は空にできない")
        if not self.fact_key.strip():
            raise ValueError("fact_key は空にできない")

    @property
    def counts_as_independent(self) -> bool:
        """独立根拠として数えられるか。推定のみ・未評価は単独では数えない。"""
        return self.status is EvidenceStatus.TRIGGERED


def distinct_roots(evidence: Iterable[Evidence]) -> frozenset[RootFactor]:
    """独立根拠として数えられる根拠の root の集合(R-A。同じ root は 1)。"""
    return frozenset(e.root_factor for e in evidence if e.counts_as_independent)


def dedupe_by_fact_key(evidence: Iterable[Evidence]) -> tuple[Evidence, ...]:
    """同じ事実(fact_key)の根拠を 1 件に統合する(R-D)。

    同じ fact_key のうち、確からしさが最も強いもの(TRIGGERED > SUSPECTED > NOT_EVALUATED)、
    同じなら一次情報で確認済みのもの、同じなら先に現れたものを残す。順序は初出順を保つ。
    """
    rank = {
        EvidenceStatus.TRIGGERED: 2,
        EvidenceStatus.SUSPECTED: 1,
        EvidenceStatus.NOT_EVALUATED: 0,
    }
    chosen: dict[str, Evidence] = {}
    for item in evidence:
        current = chosen.get(item.fact_key)
        if current is None or _key(item, rank) > _key(current, rank):
            chosen[item.fact_key] = item
    return tuple(chosen.values())


def _key(item: Evidence, rank: dict[EvidenceStatus, int]) -> tuple[int, int]:
    return (rank[item.status], int(item.primary_source_confirmed))
