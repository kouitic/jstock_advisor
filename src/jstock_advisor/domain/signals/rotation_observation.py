"""資金入替の観測(Issue #604 P-b1。#128 R2。dormant)の純粋部分: バッチ完了条件・窓・日の集計。

P(資金入替の評価を行う独立した処理。観測のみ)が 1 日に 1 回残す『日の集計』を作る
純粋関数と型を持つ。
設計: #604 P-b 設計 rev4(…6168001528)+ rev5(…6168319086)・設計レビュー PASS
(SUBU-20261014-047 …6168292046)。**時計・I/O・永続化・AWS・LINE・設定・環境変数に触れない**。
どこからも import されない(dormant。配線・IAM・Scheduler・Scan は P-b4 で、USER の承認の後)。

## 何を観測するか(rev4 差分 1・2)

* 観測するのは次の 3 点だけ: ★ 入替元の適格性の分布 ★(ELIGIBLE / BLOCKED_BY_EXIT / UNKNOWN と、
  P-a の理由コード 13 種ごとの件数)/ UNKNOWN の理由別の件数 / 対象バッチの完了時刻(窓の再検証)。
* ★ 記録は日の集計だけ ★(X)。owner・保有 ID・銘柄コード・数量・金額・Recommendation ID を持たない
  (入力の `HoldingInput` は識別子を持たない型で、集計の前に捨てる)。→ owner の符号の鍵が要らない
  (P のロールに鍵の IAM を足さない)。owner 別・保有別の記録は P-c(入替元の RAER の接続)で要る。
* ★ `evaluate_capital_rotation` を呼ばない ★(入替元の RAER が無く、提案は常に 0 件のため)。

## バッチ完了条件(`judge_batch_completion`。★ fail-closed ★)

```
COMPLETE      保有バッチの行に完了確認の時刻があり、開始時刻以降で、開始・完了とも対象日(JST 暦日)
NOT_COMPLETE  完了確認の時刻が無い(まだ終わっていない)
UNREADABLE    行が無い・読めない・開始時刻が無い・時刻の逆転・開始または完了が対象日でない
              → 『完了した』と推測しない
```

窓 = 開始時刻〜完了確認時刻(両端を含む。P-a の `EligibilityWindow`)。同日の手動実行の記録は窓の外 =
対象外になる(P-a の OUTSIDE_WINDOW)。

## 日の状態(`DayStatus`。閉じた語彙。0 や部分結果で続けない)

```
OBSERVED                        COMPLETE で保有を列挙でき、上限内。集計を持つ
BATCH_NOT_COMPLETED_IN_WINDOW   NOT_COMPLETE。集計は 0(観測していない)
INPUT_LIMIT_EXCEEDED            保有が `MAX_HOLDINGS` を超えた。集計は 0(部分結果にしない)
READ_FAILED                     バッチの行・保有の列挙を読めなかった。集計は 0
```

★ `OBSERVED` だけが『日の観測の記録』になりうる(日を固定する先勝ちの記録)。それ以外は一時的な状態の
可能性があるため、日を固定しない別の記録(gap)にする(`rotation_observation_record.audit_id_for`)。

## 保有 1 件の読取の失敗

保有の記録を読めなかった保有は `record_read_failed`、Recommendation を読めなかった保有は
`recommendation_read_failed` で数え(診断)、適格性は P-a が UNKNOWN(RECORD_MISSING /
RECOMMENDATION_MISSING など)にする。1 件の失敗を日全体へ伝えない。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from jstock_advisor.domain.jst import to_jst
from jstock_advisor.domain.signals.rotation_source_eligibility import (
    EligibilityReason,
    EligibilityWindow,
    HoldingEvaluationFacts,
    RecommendationFacts,
    derive_source_eligibility,
)

#: 保有件数の上限(処理の安全弁。投資判断の閾値ではない)。超えたら『観測しない』(部分結果にしない)
MAX_HOLDINGS: Final = 200

#: 適格性の 3 値(`SourceEligibility` の値と同じ。契約テストで固定。型は import しない)
ELIGIBILITY_ELIGIBLE: Final = "ELIGIBLE"
ELIGIBILITY_BLOCKED: Final = "BLOCKED_BY_EXIT"
ELIGIBILITY_UNKNOWN: Final = "UNKNOWN"
ELIGIBILITY_VALUES: Final = (ELIGIBILITY_ELIGIBLE, ELIGIBILITY_BLOCKED, ELIGIBILITY_UNKNOWN)

#: 理由コード → 適格性。P-a の導出の分類と同じ(契約テストで、導出の全経路と突き合わせる)
REASON_ELIGIBILITY: Final[Mapping[EligibilityReason, str]] = MappingProxyType(
    {
        EligibilityReason.RECORD_MISSING: ELIGIBILITY_UNKNOWN,
        EligibilityReason.OUTSIDE_WINDOW: ELIGIBILITY_UNKNOWN,
        EligibilityReason.CATEGORY_UNKNOWN: ELIGIBILITY_UNKNOWN,
        EligibilityReason.EVALUATION_NOT_COMPLETED: ELIGIBILITY_UNKNOWN,
        EligibilityReason.ENGINE_UNKNOWN: ELIGIBILITY_UNKNOWN,
        EligibilityReason.ENGINE_NOT_AUTHORITATIVE: ELIGIBILITY_UNKNOWN,
        EligibilityReason.RECOMMENDATION_MISSING: ELIGIBILITY_UNKNOWN,
        EligibilityReason.TYPE_UNCLASSIFIED: ELIGIBILITY_UNKNOWN,
        EligibilityReason.EXIT_RECOMMENDATION: ELIGIBILITY_BLOCKED,
        EligibilityReason.REVIEW_RECOMMENDATION: ELIGIBILITY_BLOCKED,
        EligibilityReason.ATTENTION: ELIGIBILITY_BLOCKED,
        EligibilityReason.NOTIFIED_OR_SUPPRESSED: ELIGIBILITY_BLOCKED,
        EligibilityReason.NO_EXIT_EVIDENCE: ELIGIBILITY_ELIGIBLE,
    }
)


class RotationObservationError(ValueError):
    """入力の型・値が契約に反する(組み立て側の誤り)。値を含めない。"""


def _fail(problem: str) -> RotationObservationError:
    return RotationObservationError(problem)


class BatchCompletionState(StrEnum):
    COMPLETE = "COMPLETE"
    NOT_COMPLETE = "NOT_COMPLETE"
    UNREADABLE = "UNREADABLE"


class DayStatus(StrEnum):
    OBSERVED = "OBSERVED"
    BATCH_NOT_COMPLETED_IN_WINDOW = "BATCH_NOT_COMPLETED_IN_WINDOW"
    INPUT_LIMIT_EXCEEDED = "INPUT_LIMIT_EXCEEDED"
    READ_FAILED = "READ_FAILED"


def _aware_or_none(name: str, value: object) -> dt.datetime | None:
    if value is None:
        return None
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise _fail(f"{name} must be a timezone-aware datetime or None")
    return value


# --- 入力の型(識別子を持たない) --------------------------------------------------------------


@dataclass(frozen=True)
class BatchFacts:
    """保有バッチの行から読んだ事実(開始時刻・完了確認の時刻。欠落は None)。"""

    started_at: dt.datetime | None
    completed_at: dt.datetime | None

    def __post_init__(self) -> None:
        _aware_or_none("started_at", self.started_at)
        _aware_or_none("completed_at", self.completed_at)


@dataclass(frozen=True)
class BatchJudgement:
    state: BatchCompletionState
    window: EligibilityWindow | None

    def __post_init__(self) -> None:
        if not isinstance(self.state, BatchCompletionState):
            raise _fail("state must be a BatchCompletionState")
        if (self.state is BatchCompletionState.COMPLETE) != (self.window is not None):
            raise _fail("a window exists exactly when the batch is complete")


@dataclass(frozen=True)
class HoldingInput:
    """保有 1 件の入力。★ owner・保有 ID・銘柄を持たない(集計の前に識別子を捨てる)★。"""

    facts: HoldingEvaluationFacts | None
    recommendations: Mapping[str, RecommendationFacts]
    record_read_failed: bool = False
    recommendation_read_failed: bool = False

    def __post_init__(self) -> None:
        if self.facts is not None and not isinstance(self.facts, HoldingEvaluationFacts):
            raise _fail("facts must be HoldingEvaluationFacts or None")
        if not isinstance(self.recommendations, Mapping):
            raise _fail("recommendations must be a mapping")
        for flag in (self.record_read_failed, self.recommendation_read_failed):
            if not isinstance(flag, bool):
                raise _fail("read-failure flags must be bool")
        if self.record_read_failed and self.facts is not None:
            raise _fail("a record that failed to read has no facts")


def judge_batch_completion(batch: BatchFacts | None, target_date: dt.date) -> BatchJudgement:
    """対象日の保有バッチが完了したか(★ fail-closed ★。推測で完了にしない)。"""
    if not isinstance(target_date, dt.date) or isinstance(target_date, dt.datetime):
        raise _fail("target_date must be a date")
    if batch is None or batch.started_at is None:
        return BatchJudgement(BatchCompletionState.UNREADABLE, None)
    started = batch.started_at
    if to_jst(started).date() != target_date:
        return BatchJudgement(BatchCompletionState.UNREADABLE, None)
    completed = batch.completed_at
    if completed is None:
        return BatchJudgement(BatchCompletionState.NOT_COMPLETE, None)
    if completed < started or to_jst(completed).date() != target_date:
        return BatchJudgement(BatchCompletionState.UNREADABLE, None)
    return BatchJudgement(BatchCompletionState.COMPLETE, EligibilityWindow(started, completed))


# --- 出力: 日の集計 ---------------------------------------------------------------------------

_REASON_VALUES: Final = tuple(reason.value for reason in EligibilityReason)


def _zero_counts(keys: Sequence[str]) -> dict[str, int]:
    return dict.fromkeys(keys, 0)


def _count(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _fail(f"{name} must be a non-negative int")
    return value


def _exact_counts(name: str, value: object, keys: Sequence[str]) -> Mapping[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise _fail(f"{name} must have exactly the closed keys")
    return MappingProxyType({key: _count(f"{name}.{key}", value[key]) for key in keys})


@dataclass(frozen=True)
class CapitalRotationObservation:
    """1 日の観測(★ 集計だけ ★)。整合しない値は作れない(件数の和・日の状態と時刻・語彙)。"""

    target_date: dt.date
    batch_state: BatchCompletionState
    batch_started_at: dt.datetime | None
    batch_completed_at: dt.datetime | None
    holdings_total: int
    eligibility: Mapping[str, int]
    reasons: Mapping[str, int]
    record_read_failed: int
    recommendation_read_failed: int
    day_status: DayStatus

    def __post_init__(self) -> None:
        if not isinstance(self.target_date, dt.date) or isinstance(self.target_date, dt.datetime):
            raise _fail("target_date must be a date")
        if not isinstance(self.batch_state, BatchCompletionState):
            raise _fail("batch_state must be a BatchCompletionState")
        if not isinstance(self.day_status, DayStatus):
            raise _fail("day_status must be a DayStatus")
        _aware_or_none("batch_started_at", self.batch_started_at)
        _aware_or_none("batch_completed_at", self.batch_completed_at)
        total = _count("holdings_total", self.holdings_total)
        eligibility = _exact_counts("eligibility", self.eligibility, ELIGIBILITY_VALUES)
        reasons = _exact_counts("reasons", self.reasons, _REASON_VALUES)
        record_failed = _count("record_read_failed", self.record_read_failed)
        recommendation_failed = _count(
            "recommendation_read_failed", self.recommendation_read_failed
        )
        object.__setattr__(self, "eligibility", eligibility)
        object.__setattr__(self, "reasons", reasons)
        self._validate_counts(total, eligibility, reasons, record_failed, recommendation_failed)
        self._validate_batch_and_status(total)

    @staticmethod
    def _validate_counts(
        total: int,
        eligibility: Mapping[str, int],
        reasons: Mapping[str, int],
        record_failed: int,
        recommendation_failed: int,
    ) -> None:
        if sum(eligibility.values()) != total or sum(reasons.values()) != total:
            raise _fail("the counts must add up to holdings_total")
        for value in ELIGIBILITY_VALUES:
            by_reason = sum(
                reasons[reason.value]
                for reason, group in REASON_ELIGIBILITY.items()
                if group == value
            )
            if eligibility[value] != by_reason:
                raise _fail("the eligibility counts must match the reason counts")
        if record_failed > total or recommendation_failed > total:
            raise _fail("read-failure counts must not exceed holdings_total")

    def _validate_batch_and_status(self, total: int) -> None:
        state, status = self.batch_state, self.day_status
        started, completed = self.batch_started_at, self.batch_completed_at
        if state is BatchCompletionState.COMPLETE:
            if started is None or completed is None or completed < started:
                raise _fail("a complete batch has ordered start and completion times")
        elif state is BatchCompletionState.NOT_COMPLETE:
            if started is None or completed is not None:
                raise _fail("an incomplete batch has a start time and no completion time")
        elif started is not None or completed is not None:
            raise _fail("an unreadable batch carries no times")
        empty = total == 0 and self.record_read_failed == 0 and self.recommendation_read_failed == 0
        if status is DayStatus.OBSERVED:
            if state is not BatchCompletionState.COMPLETE:
                raise _fail("an observed day needs a complete batch")
        elif status is DayStatus.BATCH_NOT_COMPLETED_IN_WINDOW:
            if state is not BatchCompletionState.NOT_COMPLETE or not empty:
                raise _fail("an incomplete-batch day has no counts")
        elif status is DayStatus.INPUT_LIMIT_EXCEEDED:
            if state is not BatchCompletionState.COMPLETE or not empty:
                raise _fail("an input-limit day has a complete batch and no counts")
        elif not empty:  # READ_FAILED
            raise _fail("a failed-read day has no counts")
        elif state is BatchCompletionState.NOT_COMPLETE:
            raise _fail("an incomplete batch is not a read failure")

    @property
    def window_seconds(self) -> int | None:
        """窓の長さ(秒)。完了した日だけ。"""
        if self.batch_started_at is None or self.batch_completed_at is None:
            return None
        return int((self.batch_completed_at - self.batch_started_at).total_seconds())


def _without_counts(
    target_date: dt.date,
    judgement: BatchJudgement,
    batch: BatchFacts | None,
    status: DayStatus,
) -> CapitalRotationObservation:
    state = judgement.state
    started = (
        batch.started_at
        if batch is not None and state is not BatchCompletionState.UNREADABLE
        else None
    )
    completed = (
        batch.completed_at if batch is not None and state is BatchCompletionState.COMPLETE else None
    )
    return CapitalRotationObservation(
        target_date=target_date,
        batch_state=state,
        batch_started_at=started,
        batch_completed_at=completed,
        holdings_total=0,
        eligibility=_zero_counts(ELIGIBILITY_VALUES),
        reasons=_zero_counts(_REASON_VALUES),
        record_read_failed=0,
        recommendation_read_failed=0,
        day_status=status,
    )


def observe_day(
    *,
    target_date: dt.date,
    batch: BatchFacts | None,
    holdings: Sequence[HoldingInput] | None,
) -> CapitalRotationObservation:
    """1 日の観測(純粋関数)。`holdings = None` は『保有の列挙に失敗した』。

    完了していない日・読めない日・上限超過の日は、集計を 0 にして日の状態で表す(部分結果にしない)。
    """
    judgement = judge_batch_completion(batch, target_date)
    if judgement.state is BatchCompletionState.UNREADABLE:
        return _without_counts(target_date, judgement, batch, DayStatus.READ_FAILED)
    if judgement.state is BatchCompletionState.NOT_COMPLETE:
        return _without_counts(
            target_date, judgement, batch, DayStatus.BATCH_NOT_COMPLETED_IN_WINDOW
        )
    window = judgement.window
    assert window is not None and batch is not None  # COMPLETE の不変条件
    if holdings is None:
        return _without_counts(target_date, judgement, batch, DayStatus.READ_FAILED)
    items = tuple(holdings)
    if len(items) > MAX_HOLDINGS:
        return _without_counts(target_date, judgement, batch, DayStatus.INPUT_LIMIT_EXCEEDED)
    for item in items:
        if not isinstance(item, HoldingInput):
            raise _fail("holdings must be HoldingInput values")
    eligibility = _zero_counts(ELIGIBILITY_VALUES)
    reasons = _zero_counts(_REASON_VALUES)
    record_failed = recommendation_failed = 0
    for item in items:
        result = derive_source_eligibility(item.facts, item.recommendations, window)
        eligibility[result.eligibility.value] += 1
        reasons[result.reason.value] += 1
        record_failed += item.record_read_failed
        recommendation_failed += item.recommendation_read_failed
    return CapitalRotationObservation(
        target_date=target_date,
        batch_state=BatchCompletionState.COMPLETE,
        batch_started_at=batch.started_at,
        batch_completed_at=batch.completed_at,
        holdings_total=len(items),
        eligibility=eligibility,
        reasons=reasons,
        record_read_failed=record_failed,
        recommendation_read_failed=recommendation_failed,
        day_status=DayStatus.OBSERVED,
    )
