"""E2(利確判定)の判定の経過(ExitTrace)を Arbiter の入力へ写す adapter(#878 PR-3b-1。dormant)。

どこからも import されない(配線なし・保存なし・flag なし・AWS なし)。現行の判定・通知・閾値・
売却比率は変えない。設計: #878 の rev3(…6096309230)・rev4(…6173296977 §2)・rev5(…6191547618)・
rev6(…6195907794)。判定を再計算しない: trace の記録を、対応表で写すだけ。

## ExitTrace は import しない(typing の Protocol で受ける)

`profit_taking.py` は config・valuation など domain の外側まで import するため、許可 import に足すと
config 層へ届く(C0 の検査は直接の import しか見ないので静かに通る)。そこで adapter は
`ExitTraceLike` / `TraceCandidateLike`(read-only property で宣言した Protocol)で受ける。
本物の `ExitTrace` は構造が一致するため、そのまま渡せる(型の一致は `tests/factories.py` の 1 行を
`mypy tests/factories.py` が検査する)。Protocol は属性の有無までしか見ないため、★ 値の検査は
adapter が値で行う ★(未知の値は fail-closed で `unmapped` へ。例外にしない)。

## 写し方(対応表。契約テストが 1:1 を固定)

  path -> TriggerKind        名前の一致。USER_TARGET_PRICE / USER_TARGET_RATE は proposals に出さず
                             pass_through へ(通知のみ。Arbiter の入力にしない)
  path -> 帰属 layer・class  PRICE_UPSIDE_MATRIX / FAIR_VALUE_STRONG / FAIR_VALUE_PARTIAL_GATE =
                             L2・VALUE_EXIT / PROFIT_PROTECTION_STRONG = L3・PROFIT_PROTECTION /
                             FULL_STRONG_CRITICAL = L1・RISK_EXIT / PARTIAL_CONDITIONS・
                             FULL_MODERATE_CONDITIONS = 票が全て価格由来なら L3・PROFIT_PROTECTION、
                             それ以外は L2・VALUE_EXIT
  claimed_strength           trace の level(1〜3)。root の数からは算出しない(PR-3d)
  source                     ★ LEGACY_ENGINE ★(現行エンジンの経路。ゲートの扱いは変えない)
  input_groups・evidence     いずれも空(L0 を使わない・root は PR-3d)

## 守ること(契約テストで固定)

* ★ `build_candidates` のゲート(層の確定性)は変えない ★: adapter は `source = LEGACY_ENGINE` を
  付けるだけ。『LEGACY_ENGINE は層の確定性ゲートを通さない』(売却判断の意味論)は、配線する PR で
  USER の 1 行確認を受けるまで入れない(arbiter.py は変更しない)
* 入力の並びに依存しない(出力は固定の順序)/ 判定を再計算しない / services・infrastructure・
  config・domain.signals を import しない(C0 が固定)
* 読む field は ExitTrace の 6 つ + TraceCandidate の 4 つ。★ 全てを値で検査する ★
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from jstock_advisor.domain.exit_architecture.arbiter import (
    TRIGGER_TIE_ORDER,
    CandidateProposal,
    ProposalSource,
    SofteningFacts,
)
from jstock_advisor.domain.exit_architecture.decision import ContractViolationError
from jstock_advisor.domain.exit_architecture.vocabulary import (
    ExitClass,
    ExitLayer,
    Strength,
    TriggerKind,
)

# --- 入力の型(Protocol。profit_taking を import しない) -------------------------------------------


class TraceCandidateLike(Protocol):
    """`profit_taking.TraceCandidate` と構造が一致する read-only の宣言。"""

    @property
    def path(self) -> str: ...

    @property
    def level(self) -> int: ...

    @property
    def origin(self) -> str: ...

    @property
    def votes(self) -> Sequence[str]: ...


class ExitTraceLike(Protocol):
    """`profit_taking.ExitTrace` と構造が一致する read-only の宣言。"""

    @property
    def candidates(self) -> Sequence[TraceCandidateLike]: ...

    @property
    def raw_level(self) -> int: ...

    @property
    def origin(self) -> str: ...

    @property
    def mitigation_steps(self) -> int: ...

    @property
    def uptrend(self) -> bool: ...

    @property
    def hard_overvalued(self) -> bool: ...


# --- 出力の型 ------------------------------------------------------------------------------------


class PassThroughKind(StrEnum):
    """Arbiter の入力にしない(通知のみ)ユーザー設定の目標到達の種別。"""

    USER_TARGET_PRICE = "USER_TARGET_PRICE"
    USER_TARGET_RATE = "USER_TARGET_RATE"


@dataclass(frozen=True)
class PassThroughNotice:
    """Decision の外へ出す『通知のみ』の印。種別だけで、通知の文面は持たない。

    E2 の理由は併合した文字列で、経路との対応を文字列の解析なしには作れない(解析は禁止)。
    """

    kind: PassThroughKind


@dataclass(frozen=True)
class ProfitTakingAdaptation:
    """adapter の出力。

    proposals    Arbiter へ渡す候補(固定の順序)
    softening    降格 (b) の事実。trace の値が不正で作れないときは None(fail-closed)
    pass_through 通知のみの印(種別の固定の順序・重複なし)
    unmapped     写せなかったものの理由符号(固定の語彙。値・識別子を含まない。昇順)
    """

    proposals: tuple[CandidateProposal, ...]
    softening: SofteningFacts | None
    pass_through: tuple[PassThroughNotice, ...]
    unmapped: tuple[str, ...]


# --- 対応表(契約テストが 1:1 を固定) ---------------------------------------------------------

#: 価格由来の票(総合利回りは分母が現在値のため価格由来)。文字列で持つ(profit_taking の VoteKind を
#: import しない)。契約テストが、価格由来 ∪ 非価格 = VoteKind の全値・互いに素であることを固定する
PRICE_DERIVED_VOTES: Final[frozenset[str]] = frozenset(
    {
        "TREND_WORSENING",
        "STRONG_TREND_WORSENING",
        "PROFIT_PROTECTION_CANDIDATE",
        "LOW_YIELD",
        "VERY_LOW_YIELD",
    }
)
NON_PRICE_VOTES: Final[frozenset[str]] = frozenset(
    {
        "FAIR_VALUE_WEAK",
        "GROWTH_SLOWDOWN",
        "GROWTH_COLLAPSE",
        "CONCENTRATION",
        "EARNINGS_EVENT_RISK_REDUCTION",
    }
)

#: 件数条件の経路(票の由来で帰属 layer と class が決まる)
_COUNT_CONDITION_PATHS: Final[frozenset[TriggerKind]] = frozenset(
    {TriggerKind.PARTIAL_CONDITIONS, TriggerKind.FULL_MODERATE_CONDITIONS}
)

#: 件数条件以外の経路の (帰属 layer, class)
_FIXED_ATTRIBUTION: Final[dict[TriggerKind, tuple[ExitLayer, ExitClass]]] = {
    TriggerKind.PRICE_UPSIDE_MATRIX: (ExitLayer.L2_EXPECTED_RETURN, ExitClass.VALUE_EXIT),
    TriggerKind.FAIR_VALUE_STRONG: (ExitLayer.L2_EXPECTED_RETURN, ExitClass.VALUE_EXIT),
    TriggerKind.FAIR_VALUE_PARTIAL_GATE: (ExitLayer.L2_EXPECTED_RETURN, ExitClass.VALUE_EXIT),
    TriggerKind.PROFIT_PROTECTION_STRONG: (
        ExitLayer.L3_PRICE_REGIME,
        ExitClass.PROFIT_PROTECTION,
    ),
    TriggerKind.FULL_STRONG_CRITICAL: (ExitLayer.L1_THESIS, ExitClass.RISK_EXIT),
}

_PASS_THROUGH_BY_PATH: Final[dict[str, PassThroughKind]] = {
    kind.value: kind for kind in PassThroughKind
}
_TRIGGER_BY_PATH: Final[dict[str, TriggerKind]] = {
    kind.value: kind for kind in TriggerKind if kind.value not in _PASS_THROUGH_BY_PATH
}
_TIE_INDEX: Final[dict[TriggerKind, int]] = {k: i for i, k in enumerate(TRIGGER_TIE_ORDER)}
_PASS_THROUGH_ORDER: Final[tuple[PassThroughKind, ...]] = tuple(PassThroughKind)

# unmapped の理由符号(固定の語彙)
TRACE_SHAPE: Final = "TRACE_SHAPE"
TRACE_CANDIDATES: Final = "TRACE_CANDIDATES"
TRACE_RAW_LEVEL: Final = "TRACE_RAW_LEVEL"
TRACE_ORIGIN: Final = "TRACE_ORIGIN"
TRACE_MITIGATION_STEPS: Final = "TRACE_MITIGATION_STEPS"
TRACE_UPTREND: Final = "TRACE_UPTREND"
TRACE_HARD_OVERVALUED: Final = "TRACE_HARD_OVERVALUED"
CANDIDATE_SHAPE: Final = "CANDIDATE_SHAPE"
CANDIDATE_PATH: Final = "CANDIDATE_PATH"
CANDIDATE_PATH_UNKNOWN: Final = "CANDIDATE_PATH_UNKNOWN"
CANDIDATE_LEVEL: Final = "CANDIDATE_LEVEL"
CANDIDATE_ORIGIN: Final = "CANDIDATE_ORIGIN"
CANDIDATE_VOTES: Final = "CANDIDATE_VOTES"
CANDIDATE_VOTE_UNKNOWN: Final = "CANDIDATE_VOTE_UNKNOWN"
CANDIDATE_VOTES_EMPTY: Final = "CANDIDATE_VOTES_EMPTY"
CANDIDATE_REJECTED: Final = "CANDIDATE_REJECTED"

_MISSING: Final = object()

#: trace の level が取りうる範囲(1 = WATCH 〜 3 = FULL。0 = HOLD は候補ではない)
_LEVEL_RANGE: Final = range(1, 4)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_name(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _is_sequence(value: object) -> bool:
    return isinstance(value, (tuple, list))


def _attr(obj: object, name: str) -> Any:
    return getattr(obj, name, _MISSING)


# --- 検査と写像 ----------------------------------------------------------------------------------


def _softening(trace: object, unmapped: set[str]) -> SofteningFacts | None:
    steps = _attr(trace, "mitigation_steps")
    uptrend = _attr(trace, "uptrend")
    hard = _attr(trace, "hard_overvalued")
    ok = True
    if not _is_int(steps) or steps < 0:
        unmapped.add(TRACE_MITIGATION_STEPS)
        ok = False
    if not isinstance(uptrend, bool):
        unmapped.add(TRACE_UPTREND)
        ok = False
    if not isinstance(hard, bool):
        unmapped.add(TRACE_HARD_OVERVALUED)
        ok = False
    return SofteningFacts(steps, uptrend, hard) if ok else None


def _validated_votes(candidate: object, unmapped: set[str]) -> tuple[str, ...] | None:
    votes = _attr(candidate, "votes")
    if not _is_sequence(votes) or not all(isinstance(v, str) for v in votes):
        unmapped.add(CANDIDATE_VOTES)
        return None
    if not all(v in PRICE_DERIVED_VOTES or v in NON_PRICE_VOTES for v in votes):
        unmapped.add(CANDIDATE_VOTE_UNKNOWN)
        return None
    return tuple(votes)


def _attribution(kind: TriggerKind, votes: tuple[str, ...]) -> tuple[ExitLayer, ExitClass]:
    if kind in _COUNT_CONDITION_PATHS:
        if all(v in PRICE_DERIVED_VOTES for v in votes):
            return ExitLayer.L3_PRICE_REGIME, ExitClass.PROFIT_PROTECTION
        return ExitLayer.L2_EXPECTED_RETURN, ExitClass.VALUE_EXIT
    return _FIXED_ATTRIBUTION[kind]


def _adapt_candidate(
    candidate: object,
    proposals: list[CandidateProposal],
    pass_through: set[PassThroughKind],
    unmapped: set[str],
) -> None:
    path = _attr(candidate, "path")
    level = _attr(candidate, "level")
    origin = _attr(candidate, "origin")
    if path is _MISSING or level is _MISSING or origin is _MISSING:
        unmapped.add(CANDIDATE_SHAPE)
        return
    valid = True
    if not _is_name(path):
        unmapped.add(CANDIDATE_PATH)
        valid = False
    if not _is_int(level) or level not in _LEVEL_RANGE:
        unmapped.add(CANDIDATE_LEVEL)
        valid = False
    if not _is_name(origin):
        unmapped.add(CANDIDATE_ORIGIN)
        valid = False
    votes = _validated_votes(candidate, unmapped)
    if not valid or votes is None:
        return
    if path in _PASS_THROUGH_BY_PATH:
        pass_through.add(_PASS_THROUGH_BY_PATH[path])
        return
    kind = _TRIGGER_BY_PATH.get(path)
    if kind is None:
        unmapped.add(CANDIDATE_PATH_UNKNOWN)
        return
    if kind in _COUNT_CONDITION_PATHS and not votes:
        unmapped.add(CANDIDATE_VOTES_EMPTY)
        return
    layer, exit_class = _attribution(kind, votes)
    try:
        proposals.append(
            CandidateProposal(
                kind,
                exit_class,
                Strength(level),
                layer,
                (),
                frozenset(),
                ProposalSource.LEGACY_ENGINE,
            )
        )
    except ContractViolationError:
        unmapped.add(CANDIDATE_REJECTED)  # 現行のエンジンが到達できない強さ・class など


def _proposal_order(proposal: CandidateProposal) -> tuple[int, int, str, str]:
    return (
        _TIE_INDEX[proposal.trigger_kind],
        int(proposal.claimed_strength),
        proposal.layer.value,
        proposal.exit_class.value,
    )


def adapt_profit_taking_trace(trace: ExitTraceLike) -> ProfitTakingAdaptation:
    """ExitTrace(の形をしたもの)を Arbiter の入力へ写す。例外を送出しない(不正は unmapped へ)。"""
    unmapped: set[str] = set()
    candidates = _attr(trace, "candidates")
    raw_level = _attr(trace, "raw_level")
    origin = _attr(trace, "origin")
    if candidates is _MISSING or raw_level is _MISSING or origin is _MISSING:
        unmapped.add(TRACE_SHAPE)
    if candidates is not _MISSING and not _is_sequence(candidates):
        unmapped.add(TRACE_CANDIDATES)
    if raw_level is not _MISSING and (not _is_int(raw_level) or raw_level not in range(0, 4)):
        unmapped.add(TRACE_RAW_LEVEL)
    if origin is not _MISSING and not _is_name(origin):
        unmapped.add(TRACE_ORIGIN)
    softening = _softening(trace, unmapped)

    proposals: list[CandidateProposal] = []
    pass_through: set[PassThroughKind] = set()
    if _is_sequence(candidates):
        for candidate in candidates:
            _adapt_candidate(candidate, proposals, pass_through, unmapped)
    return ProfitTakingAdaptation(
        proposals=tuple(sorted(proposals, key=_proposal_order)),
        softening=softening,
        pass_through=tuple(
            PassThroughNotice(kind) for kind in _PASS_THROUGH_ORDER if kind in pass_through
        ),
        unmapped=tuple(sorted(unmapped)),
    )


__all__ = [
    "NON_PRICE_VOTES",
    "PRICE_DERIVED_VOTES",
    "ExitTraceLike",
    "PassThroughKind",
    "PassThroughNotice",
    "ProfitTakingAdaptation",
    "TraceCandidateLike",
    "adapt_profit_taking_trace",
]
