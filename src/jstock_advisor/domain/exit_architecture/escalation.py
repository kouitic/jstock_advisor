"""通知の escalation(ratchet)の決定・配信の二段・検出の記録(Issue #879 N3 PR-1。dormant)。

「同じ局面(anchor)の中で、配信された最も重い段階を超えたときだけ再通知する」ための純粋な部品。
設計: #879 rev1〜rev4(…6093419456・…6093607754・…6093792329・…6093851263)。**時計・I/O・乱数・設定に
触れない**。どこからも import されない(dormant。配線・保存・通知への接続は PR-2 以降)。

## 守ること(契約テストで固定する)

* ★ 比較の対象は『その局面で**配信された**最大の段階』★(前日の状態ではない)。段階が下がっても・同じ
  段階が続いても再通知しない。上がったときだけ送る決定になる
* ★ 決定と配信の二段 ★: 純粋関数 `decide_escalation` は『送る決定』までで、配信が DELIVERED に
  なったときだけ配信済みの段階が進む(`next_max_delivered_rank`)。下流(優先度・kill switch・
  決算直前・通信の失敗等)で遮られた日は進まず、翌日も rank が上なら再び『送る決定』になる
  (遮られた悪化は消えない)
* ★ UNDETERMINED の日は何も決めない ★(送らない・配信済みを変えない)。理由は信頼性 UNUSABLE なら
  RELIABILITY_CAP、それ以外は UNDETERMINED_INPUT
* 『検出』は rank >= 1 の日(健全は検出ではない)。検出ごとに『決定』と『配信』の二段の記録を
  1 件残し、detected = delivered + suppressed が常に成り立つ(取りこぼし・二重なし)
* ★ 新しい数を持たない ★(小数・日数の定数なし。AC-6)。段階の順序は #877 の `REGIME_ORDER` の
  写しで、二重に定義しない
* 通知用の理由(`EscalationSuppressionReason`)は C0 の `SuppressionReason` とは別の enum
  (C0 の語彙は変えない)

## この module が持たないもの

anchor の identity の hash・配信済みの段階の復元(`domain/notification/
attention_escalation_identity.py`。hashlib を C0 の import guard の外に置くため別 module)・
通知の保存・配信・AuditLog への記録(PR-2)。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from jstock_advisor.domain.exit_architecture.determination import Determination
from jstock_advisor.domain.exit_architecture.price_regime import REGIME_ORDER, severity_rank
from jstock_advisor.domain.exit_architecture.vocabulary import RegimeState, UndeterminedReason

#: 検出の対象になる最小の段階(0 = HEALTHY は検出ではない)
FIRST_DETECTED_RANK: Final = 1
#: 段階の最大(REGIME_ORDER の写し)
MAX_RANK: Final = len(REGIME_ORDER) - 1


class EscalationContractError(ValueError):
    """入力の型・値が契約に反する(組み立て側の誤り)。値を含めない。"""


class EscalationKind(StrEnum):
    NOT_A_DETECTION = "NOT_A_DETECTION"  # 健全(rank 0)。検出ではない
    WOULD_SEND_FIRST = "WOULD_SEND_FIRST"  # その局面で配信された実績がない
    WOULD_SEND_ESCALATION = "WOULD_SEND_ESCALATION"  # 段階が上がった
    SUPPRESSED = "SUPPRESSED"


class EscalationSuppressionReason(StrEnum):
    """通知用の理由。決定段と配信段に分かれる(下の集合)。C0 の SuppressionReason とは別の enum。"""

    # --- 決定段 ---
    DUPLICATE_SAME_SEVERITY = "DUPLICATE_SAME_SEVERITY"
    NOT_ESCALATED = "NOT_ESCALATED"
    UNDETERMINED_INPUT = "UNDETERMINED_INPUT"
    RELIABILITY_CAP = "RELIABILITY_CAP"
    UNDECIDABLE_PAST_LOG = "UNDECIDABLE_PAST_LOG"  # 過去の配信ログを読めず、決められない(送らない)
    # --- 配信段(下流の既存の抑止・失敗。実在の語彙への対応は REASON_SOURCES)---
    LOW_PRIORITY = "LOW_PRIORITY"
    DUPLICATE_STOCK_NOTIFICATION = "DUPLICATE_STOCK_NOTIFICATION"
    KILL_SWITCH_SUPPRESSED = "KILL_SWITCH_SUPPRESSED"
    TRADE_COOLDOWN = "TRADE_COOLDOWN"
    TRADE_DETECTION_IN_PROGRESS = "TRADE_DETECTION_IN_PROGRESS"
    DATA_QUALITY = "DATA_QUALITY"
    DUPLICATE_SUPPRESSED = "DUPLICATE_SUPPRESSED"  # claim による抑止(送信済み / 送信中)
    DELIVERY_FAILED = "DELIVERY_FAILED"  # push の例外


_R = EscalationSuppressionReason

DECISION_STAGE_REASONS: Final = frozenset(
    {
        _R.DUPLICATE_SAME_SEVERITY,
        _R.NOT_ESCALATED,
        _R.UNDETERMINED_INPUT,
        _R.RELIABILITY_CAP,
        _R.UNDECIDABLE_PAST_LOG,
    }
)
DELIVERY_STAGE_REASONS: Final = frozenset(
    {
        _R.LOW_PRIORITY,
        _R.DUPLICATE_STOCK_NOTIFICATION,
        _R.KILL_SWITCH_SUPPRESSED,
        _R.TRADE_COOLDOWN,
        _R.TRADE_DETECTION_IN_PROGRESS,
        _R.DATA_QUALITY,
        _R.DUPLICATE_SUPPRESSED,
        _R.DELIVERY_FAILED,
    }
)

#: 各理由が対応する、実在の語彙のメンバー(enum の名前, メンバー名)。None = 対応する語彙が無い
#: (この module が語彙の持ち主、または push の例外)。契約テストが、右辺が実在することを固定する
#: (実在しない語 = DAILY_CAP など は混ぜない)。
REASON_SOURCES: Final[Mapping[EscalationSuppressionReason, tuple[str, str] | None]] = {
    _R.DUPLICATE_SAME_SEVERITY: None,
    _R.NOT_ESCALATED: None,
    _R.UNDETERMINED_INPUT: ("SuppressionReason", "UNDETERMINED_INPUT"),
    _R.RELIABILITY_CAP: ("SuppressionReason", "RELIABILITY_CAP"),
    _R.UNDECIDABLE_PAST_LOG: ("NotificationStatus", "DATA_INSUFFICIENT"),
    _R.LOW_PRIORITY: ("EligibilityBlockCategory", "LOW_PRIORITY"),
    _R.DUPLICATE_STOCK_NOTIFICATION: ("EligibilityBlockCategory", "DUPLICATE_STOCK_NOTIFICATION"),
    _R.KILL_SWITCH_SUPPRESSED: ("NotificationStatus", "KILL_SWITCH_SUPPRESSED"),
    _R.TRADE_COOLDOWN: ("EligibilityBlockCategory", "TRADE_COOLDOWN"),
    _R.TRADE_DETECTION_IN_PROGRESS: ("EligibilityBlockCategory", "TRADE_DETECTION_IN_PROGRESS"),
    _R.DATA_QUALITY: ("EligibilityBlockCategory", "DATA_QUALITY"),
    _R.DUPLICATE_SUPPRESSED: ("NotificationStatus", "DUPLICATE_SUPPRESSED"),
    _R.DELIVERY_FAILED: None,
}


@dataclass(frozen=True)
class EscalationDecision:
    """決定段の結果。`rank` は今日の段階(UNDETERMINED のときは None)。"""

    kind: EscalationKind
    rank: int | None = None
    reason: EscalationSuppressionReason | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EscalationKind):
            raise EscalationContractError("kind must be an EscalationKind")
        if self.kind is EscalationKind.SUPPRESSED:
            if self.reason not in DECISION_STAGE_REASONS:
                raise EscalationContractError("a suppressed decision needs a decision-stage reason")
        elif self.reason is not None:
            raise EscalationContractError("only a suppressed decision carries a reason")
        if self.rank is not None and (
            isinstance(self.rank, bool) or not 0 <= self.rank <= MAX_RANK
        ):
            raise EscalationContractError("rank is outside the severity order")
        if self.would_send and (self.rank is None or self.rank < FIRST_DETECTED_RANK):
            raise EscalationContractError("a decision to send carries a detected rank")
        if self.kind is EscalationKind.NOT_A_DETECTION and self.rank != 0:
            raise EscalationContractError("a non-detection is rank 0")

    @property
    def would_send(self) -> bool:
        return self.kind in (EscalationKind.WOULD_SEND_FIRST, EscalationKind.WOULD_SEND_ESCALATION)

    @property
    def is_detection(self) -> bool:
        """検出 = 送る決定 または 決定段で抑止された日(健全の日は含まない)。"""
        return self.kind is not EscalationKind.NOT_A_DETECTION


def _check_max_delivered(max_delivered_rank: object) -> int | None:
    if max_delivered_rank is None:
        return None
    if (
        isinstance(max_delivered_rank, bool)
        or not isinstance(max_delivered_rank, int)
        or not FIRST_DETECTED_RANK <= max_delivered_rank <= MAX_RANK
    ):
        raise EscalationContractError("max_delivered_rank must be None or a detected rank")
    return max_delivered_rank


def decide_escalation(
    *, today: Determination[RegimeState], max_delivered_rank: int | None
) -> EscalationDecision:
    """今日の段階と『その局面で配信された最大の段階』から、送る決定かどうかを返す(決定段)。

    1 UNDETERMINED → 抑止(送らない・配信済みは変えない)/ 2 健全 → 検出ではない /
    3 配信の実績なし → 最初の通知 / 4 配信済みの最大より上 → escalation /
    5 同じ → 抑止(DUPLICATE_SAME_SEVERITY)/ 6 下 → 抑止(NOT_ESCALATED)
    anchor が変わった(規則 7)ときは、呼び出し側が `max_delivered_rank = None` で呼ぶ。
    """
    if not isinstance(today, Determination):
        raise EscalationContractError("today must be a Determination")
    delivered = _check_max_delivered(max_delivered_rank)
    if not today.is_determined:
        reason = (
            _R.RELIABILITY_CAP
            if today.reason is UndeterminedReason.RELIABILITY_UNUSABLE
            else _R.UNDETERMINED_INPUT
        )
        return EscalationDecision(EscalationKind.SUPPRESSED, None, reason)
    rank = severity_rank(today.unwrap())
    if rank < FIRST_DETECTED_RANK:
        return EscalationDecision(EscalationKind.NOT_A_DETECTION, rank)
    if delivered is None:
        return EscalationDecision(EscalationKind.WOULD_SEND_FIRST, rank)
    if rank > delivered:
        return EscalationDecision(EscalationKind.WOULD_SEND_ESCALATION, rank)
    reason = _R.DUPLICATE_SAME_SEVERITY if rank == delivered else _R.NOT_ESCALATED
    return EscalationDecision(EscalationKind.SUPPRESSED, rank, reason)


def undecidable_past_log_decision() -> EscalationDecision:
    """過去の配信ログを読めず決められない日(送らない・配信済みを変えない)。"""
    return EscalationDecision(EscalationKind.SUPPRESSED, None, _R.UNDECIDABLE_PAST_LOG)


class DeliveryKind(StrEnum):
    DELIVERED = "DELIVERED"  # 配信の記録(NotificationLog の保存)が残った
    BLOCKED = "BLOCKED"  # 下流で遮られた・通信が失敗した
    NOT_ATTEMPTED = "NOT_ATTEMPTED"  # 決定で抑止された日(配信段に入らない)


@dataclass(frozen=True)
class DeliveryOutcome:
    kind: DeliveryKind
    reason: EscalationSuppressionReason | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, DeliveryKind):
            raise EscalationContractError("kind must be a DeliveryKind")
        if self.kind is DeliveryKind.BLOCKED:
            if self.reason not in DELIVERY_STAGE_REASONS:
                raise EscalationContractError("a blocked delivery needs a delivery-stage reason")
        elif self.reason is not None:
            raise EscalationContractError("only a blocked delivery carries a reason")


DELIVERED: Final = DeliveryOutcome(DeliveryKind.DELIVERED)
NOT_ATTEMPTED: Final = DeliveryOutcome(DeliveryKind.NOT_ATTEMPTED)


def blocked(reason: EscalationSuppressionReason) -> DeliveryOutcome:
    return DeliveryOutcome(DeliveryKind.BLOCKED, reason)


class DetectionOutcome(StrEnum):
    SENT = "SENT"
    SUPPRESSED = "SUPPRESSED"


@dataclass(frozen=True)
class DetectionRecord:
    """検出 1 件(1 日・1 局面につき 1 件。rank >= 1 の日と決定段で抑止された日)の二段の記録。

    決定が『送る』なら配信は DELIVERED か BLOCKED、決定で抑止された日は NOT_ATTEMPTED だけ。
    """

    decision: EscalationDecision
    delivery: DeliveryOutcome

    def __post_init__(self) -> None:
        if not self.decision.is_detection:
            raise EscalationContractError("a healthy day is not a detection")
        if self.decision.would_send:
            if self.delivery.kind is DeliveryKind.NOT_ATTEMPTED:
                raise EscalationContractError("a decision to send is delivered or blocked")
        elif self.delivery.kind is not DeliveryKind.NOT_ATTEMPTED:
            raise EscalationContractError("a suppressed decision never reaches delivery")

    @property
    def outcome(self) -> DetectionOutcome:
        """SENT = 送る決定 かつ DELIVERED。それ以外は全て SUPPRESSED(理由は `reason`)。"""
        if self.decision.would_send and self.delivery.kind is DeliveryKind.DELIVERED:
            return DetectionOutcome.SENT
        return DetectionOutcome.SUPPRESSED

    @property
    def reason(self) -> EscalationSuppressionReason | None:
        """SUPPRESSED の理由(決定段または配信段)。SENT は None。"""
        if self.outcome is DetectionOutcome.SENT:
            return None
        return self.decision.reason if not self.decision.would_send else self.delivery.reason


def next_max_delivered_rank(max_delivered_rank: int | None, record: DetectionRecord) -> int | None:
    """配信済みの最大の段階の更新(★ 唯一の更新点 ★)。DELIVERED のときだけ進み、減らない。"""
    current = _check_max_delivered(max_delivered_rank)
    if record.delivery.kind is not DeliveryKind.DELIVERED:
        return current
    rank = record.decision.rank
    assert rank is not None  # DELIVERED は送る決定だけ(DetectionRecord の不変条件)
    return rank if current is None else max(current, rank)


@dataclass(frozen=True)
class EscalationCounts:
    """検出と配信の件数(#16 の評価項目。値は件数と理由別の件数だけ)。"""

    detected: int
    decided_to_send: int
    delivered: int
    suppressed: int
    blocked_by_reason: Mapping[EscalationSuppressionReason, int]
    suppressed_by_reason: Mapping[EscalationSuppressionReason, int]


def summarize(records: Iterable[DetectionRecord]) -> EscalationCounts:
    """検出の記録の集計。★ detected = delivered + suppressed ★(取りこぼし・二重なし)。"""
    items = tuple(records)
    suppressed = [r for r in items if r.outcome is DetectionOutcome.SUPPRESSED]
    return EscalationCounts(
        detected=len(items),
        decided_to_send=sum(1 for r in items if r.decision.would_send),
        delivered=sum(1 for r in items if r.outcome is DetectionOutcome.SENT),
        suppressed=len(suppressed),
        blocked_by_reason=dict(
            Counter(
                r.delivery.reason
                for r in suppressed
                if r.decision.would_send and r.delivery.reason is not None
            )
        ),
        suppressed_by_reason=dict(Counter(r.reason for r in suppressed if r.reason)),
    )
