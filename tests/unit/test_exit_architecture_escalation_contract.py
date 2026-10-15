"""通知の escalation(ratchet)の契約テスト(Issue #879 N3 PR-1。dormant)。

設計: #879 rev1〜rev4 + USER 承認 #122(U-6 / U-7 = 案 (a))。

  1  decide_escalation の格子(今日の状態 × 配信済みの最大の段階)が独立の oracle と一致する
  2  ★ 同じ段階の継続・下降では送る決定にならない / 上がったときだけ ★(系列のシミュレーション)
  3  ★ 決定と配信の二段 ★: 配信が DELIVERED のときだけ配信済みの段階が進む(遮られた悪化は消えない)
  4  ★ UNDETERMINED の日を挟んでも配信の集合が変わらない ★(metamorphic)
  5  検出の記録の不変条件と集計(detected = delivered + suppressed)
  6  通知用の理由が実在の enum へ 1 対 1 に対応する(DAILY_CAP 等の実在しない語を混ぜない)
  7  旧 identity は現行の `_compute_attention_event_identity` と同値 / 新 identity は段階を織り込む
  8  配信済みの段階の復元(新形式 → その段階 / 旧形式 → 今日の段階 = 移行日の再送 0 / 他 → None)
  9  新しい数を持たない(AST)・純粋(時計・I/O なし)・どこからも import されない(dormant)
"""

from __future__ import annotations

import ast
import datetime as dt
import random
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.domain.entities import enums
from jstock_advisor.domain.exit_architecture import escalation as esc
from jstock_advisor.domain.exit_architecture import vocabulary
from jstock_advisor.domain.exit_architecture.determination import Determination
from jstock_advisor.domain.exit_architecture.escalation import (
    DECISION_STAGE_REASONS,
    DELIVERED,
    DELIVERY_STAGE_REASONS,
    FIRST_DETECTED_RANK,
    MAX_RANK,
    NOT_ATTEMPTED,
    REASON_SOURCES,
    DeliveryKind,
    DeliveryOutcome,
    DetectionOutcome,
    DetectionRecord,
    EscalationContractError,
    EscalationDecision,
    EscalationKind,
    EscalationSuppressionReason,
    blocked,
    decide_escalation,
    next_max_delivered_rank,
    summarize,
    undecidable_past_log_decision,
)
from jstock_advisor.domain.exit_architecture.price_regime import REGIME_ORDER
from jstock_advisor.domain.exit_architecture.vocabulary import RegimeState, UndeterminedReason
from jstock_advisor.domain.notification import attention_escalation_identity as ident
from jstock_advisor.domain.notification.attention_escalation_identity import (
    EscalationAnchor,
    IdentityContractError,
    legacy_content_hash,
    restore_delivered_rank,
    severity_content_hash,
)
from jstock_advisor.services.line_notification_service import _compute_attention_event_identity
from tests.unit.test_rotation_observation_contract import _importers_of

_PACKAGE = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"
_ESC_PATH = _PACKAGE / "domain" / "exit_architecture" / "escalation.py"
_IDENT_PATH = _PACKAGE / "domain" / "notification" / "attention_escalation_identity.py"
_R = EscalationSuppressionReason
_RANKS = tuple(range(len(REGIME_ORDER)))
_DETECTED = tuple(range(FIRST_DETECTED_RANK, MAX_RANK + 1))
_DELIVERED_CHOICES: tuple[int | None, ...] = (None, *_DETECTED)
_DELIVERY_POOL = sorted(DELIVERY_STAGE_REASONS, key=str)


def _det(rank: int) -> Determination[RegimeState]:
    return Determination.of(REGIME_ORDER[rank])


def _und(reason: UndeterminedReason) -> Determination[RegimeState]:
    return Determination.undetermined(reason)


# --- 1 格子 ------------------------------------------------------------------------------------


def _oracle(today: Determination[RegimeState], delivered: int | None) -> tuple[Any, ...]:
    """仕様から直接書いた独立の oracle(kind, rank, reason)。"""
    if not today.is_determined:
        reason = (
            "RELIABILITY_CAP"
            if today.reason is UndeterminedReason.RELIABILITY_UNUSABLE
            else "UNDETERMINED_INPUT"
        )
        return ("SUPPRESSED", None, reason)
    rank = REGIME_ORDER.index(today.unwrap())
    if rank == 0:
        return ("NOT_A_DETECTION", 0, None)
    if delivered is None:
        return ("WOULD_SEND_FIRST", rank, None)
    if rank > delivered:
        return ("WOULD_SEND_ESCALATION", rank, None)
    if rank == delivered:
        return ("SUPPRESSED", rank, "DUPLICATE_SAME_SEVERITY")
    return ("SUPPRESSED", rank, "NOT_ESCALATED")


def _flat(d: EscalationDecision) -> tuple[Any, ...]:
    return (d.kind.value, d.rank, d.reason.value if d.reason is not None else None)


_TODAYS = [_det(r) for r in _RANKS] + [_und(u) for u in UndeterminedReason]


@pytest.mark.parametrize("delivered", _DELIVERED_CHOICES)
@pytest.mark.parametrize("today", _TODAYS, ids=lambda t: str(t.reason or t.value))
def test_decide_escalation_matches_the_independent_oracle(
    today: Determination[RegimeState], delivered: int | None
) -> None:
    assert _flat(decide_escalation(today=today, max_delivered_rank=delivered)) == _oracle(
        today, delivered
    )


def test_only_reliability_unusable_is_a_reliability_cap() -> None:
    for u in UndeterminedReason:
        d = decide_escalation(today=_und(u), max_delivered_rank=None)
        expected = _R.RELIABILITY_CAP if u is UndeterminedReason.RELIABILITY_UNUSABLE else None
        assert (d.reason is _R.RELIABILITY_CAP) == (expected is not None)
        if expected is None:
            assert d.reason is _R.UNDETERMINED_INPUT


def test_severity_order_is_the_regime_order_copy() -> None:
    assert len(REGIME_ORDER) - 1 == MAX_RANK
    assert FIRST_DETECTED_RANK == 1
    for rank, state in enumerate(REGIME_ORDER):
        d = decide_escalation(today=Determination.of(state), max_delivered_rank=None)
        assert d.rank == rank


def test_the_decision_does_not_use_the_previous_day() -> None:
    """★ 比較の対象は『配信された最大』★。昨日が重くても、配信されていなければ最初の通知。"""
    d = decide_escalation(today=_det(1), max_delivered_rank=None)
    assert d.kind is EscalationKind.WOULD_SEND_FIRST


@pytest.mark.parametrize("bad", [0, MAX_RANK + 1, -1, True, 1.0, "1"])
def test_max_delivered_rank_is_validated(bad: Any) -> None:
    with pytest.raises(EscalationContractError):
        decide_escalation(today=_det(1), max_delivered_rank=bad)


def test_today_must_be_a_determination() -> None:
    with pytest.raises(EscalationContractError):
        decide_escalation(today=RegimeState.BREAKDOWN, max_delivered_rank=None)  # type: ignore[arg-type]


def test_undecidable_past_log_sends_nothing_and_is_a_detection() -> None:
    d = undecidable_past_log_decision()
    assert not d.would_send and d.is_detection and d.reason is _R.UNDECIDABLE_PAST_LOG


# --- 2〜4 系列のシミュレーション -----------------------------------------------------------------


def _run(
    days: list[int | None],
    deliver: Any = lambda decision: DELIVERED,
) -> tuple[list[DetectionRecord], int | None, list[int]]:
    """1 局面の日次系列(rank, None = UNDETERMINED)を回す。

    返り値 = (検出の記録, 最終の配信済みの最大, 配信した段階)。
    """
    delivered_max: int | None = None
    records: list[DetectionRecord] = []
    sent: list[int] = []
    for day in days:
        today = _und(UndeterminedReason.INPUT_MISSING) if day is None else _det(day)
        decision = decide_escalation(today=today, max_delivered_rank=delivered_max)
        if not decision.is_detection:
            continue
        outcome = deliver(decision) if decision.would_send else NOT_ATTEMPTED
        record = DetectionRecord(decision, outcome)
        records.append(record)
        delivered_max = next_max_delivered_rank(delivered_max, record)
        if record.outcome is DetectionOutcome.SENT:
            sent.append(record.decision.rank)  # type: ignore[arg-type]
    return records, delivered_max, sent


def test_continuation_and_descent_never_resend_only_a_rise_does() -> None:
    _, _, sent = _run([1, 1, 1, 2, 2, 1, 2, 3, 3, 2, 1, 3])
    assert sent == [1, 2, 3]  # 各段階で 1 回だけ


def test_a_healthy_day_neither_sends_nor_resets_the_ratchet() -> None:
    records, delivered_max, sent = _run([2, 0, 2, 0, 1, 3])
    assert sent == [2, 3]
    assert delivered_max == 3
    assert all(r.decision.rank != 0 for r in records)  # 健全の日は検出の記録にならない


def test_the_delivered_max_never_decreases() -> None:
    rng = random.Random(879)
    for _ in range(200):
        days = [rng.choice([None, *_RANKS]) for _ in range(rng.randint(1, 15))]
        delivered_max: int | None = None
        for day in days:
            today = _und(UndeterminedReason.INPUT_MISSING) if day is None else _det(day)
            d = decide_escalation(today=today, max_delivered_rank=delivered_max)
            if not d.is_detection:
                continue
            record = DetectionRecord(d, DELIVERED if d.would_send else NOT_ATTEMPTED)
            new = next_max_delivered_rank(delivered_max, record)
            assert (delivered_max or 0) <= (new or 0)
            delivered_max = new


@pytest.mark.parametrize("reason", sorted(DELIVERY_STAGE_REASONS, key=str))
def test_a_blocked_delivery_does_not_advance_and_the_worsening_is_not_lost(
    reason: EscalationSuppressionReason,
) -> None:
    """★ 二段 ★: 遮られた日は進まず、翌日も rank が上なら再び『送る決定』。"""
    deliver_first = {"n": 0}

    def deliver(decision: EscalationDecision) -> DeliveryOutcome:
        deliver_first["n"] += 1
        return DELIVERED if deliver_first["n"] == 1 else blocked(reason)

    # 1 日目 rank1 配信 / 2 日目 rank2 遮られた / 3 日目 rank2 再び送る決定(遮られた)
    records, delivered_max, sent = _run([1, 2, 2], deliver)
    assert sent == [1]
    assert delivered_max == 1
    assert [r.decision.kind for r in records] == [
        EscalationKind.WOULD_SEND_FIRST,
        EscalationKind.WOULD_SEND_ESCALATION,
        EscalationKind.WOULD_SEND_ESCALATION,
    ]
    assert [r.reason for r in records] == [None, reason, reason]


def test_a_blocked_first_notification_is_retried_the_next_day() -> None:
    n = {"n": 0}

    def deliver(decision: EscalationDecision) -> DeliveryOutcome:
        n["n"] += 1
        return blocked(_R.KILL_SWITCH_SUPPRESSED) if n["n"] == 1 else DELIVERED

    records, delivered_max, sent = _run([1, 1], deliver)
    assert sent == [1] and delivered_max == 1
    assert records[0].decision.kind is EscalationKind.WOULD_SEND_FIRST
    assert records[1].decision.kind is EscalationKind.WOULD_SEND_FIRST


def test_undetermined_days_do_not_change_what_is_delivered() -> None:
    """★ metamorphic(T-3)★: UNDETERMINED の日をどこへ挟んでも、配信した段階の列は同じ。"""
    rng = random.Random(1015)
    for _ in range(300):
        base: list[int | None] = [rng.choice(_RANKS) for _ in range(rng.randint(1, 12))]
        _, max_a, sent_a = _run(base)
        mixed = list(base)
        for _ in range(rng.randint(1, 6)):
            mixed.insert(rng.randint(0, len(mixed)), None)
        _, max_b, sent_b = _run(mixed)
        assert (max_a, sent_a) == (max_b, sent_b)


def test_an_undetermined_day_is_a_suppressed_detection_that_never_reaches_delivery() -> None:
    records, delivered_max, _ = _run([2, None, 2])
    assert delivered_max == 2
    undetermined = records[1]
    assert undetermined.outcome is DetectionOutcome.SUPPRESSED
    assert undetermined.reason is _R.UNDETERMINED_INPUT
    assert undetermined.delivery == NOT_ATTEMPTED


# --- 5 記録の不変条件と集計 ---------------------------------------------------------------------


def test_record_invariants() -> None:
    send = decide_escalation(today=_det(1), max_delivered_rank=None)
    dup = decide_escalation(today=_det(1), max_delivered_rank=1)
    healthy = decide_escalation(today=_det(0), max_delivered_rank=None)
    DetectionRecord(send, DELIVERED)
    DetectionRecord(send, blocked(_R.LOW_PRIORITY))
    DetectionRecord(dup, NOT_ATTEMPTED)
    with pytest.raises(EscalationContractError):
        DetectionRecord(send, NOT_ATTEMPTED)  # 送る決定は配信か遮りのどちらか
    with pytest.raises(EscalationContractError):
        DetectionRecord(dup, DELIVERED)  # 抑止された決定は配信段へ入らない
    with pytest.raises(EscalationContractError):
        DetectionRecord(healthy, NOT_ATTEMPTED)  # 健全は検出ではない


def test_decision_and_delivery_constructors_reject_inconsistent_values() -> None:
    with pytest.raises(EscalationContractError):
        EscalationDecision(EscalationKind.SUPPRESSED, 1, None)
    with pytest.raises(EscalationContractError):
        EscalationDecision(EscalationKind.SUPPRESSED, 1, _R.LOW_PRIORITY)  # 配信段の理由は不可
    with pytest.raises(EscalationContractError):
        EscalationDecision(EscalationKind.WOULD_SEND_FIRST, 0)
    with pytest.raises(EscalationContractError):
        EscalationDecision(EscalationKind.WOULD_SEND_FIRST, 1, _R.NOT_ESCALATED)
    with pytest.raises(EscalationContractError):
        EscalationDecision(EscalationKind.NOT_A_DETECTION, 1)
    with pytest.raises(EscalationContractError):
        EscalationDecision(EscalationKind.WOULD_SEND_FIRST, MAX_RANK + 1)
    with pytest.raises(EscalationContractError):
        DeliveryOutcome(DeliveryKind.BLOCKED, _R.NOT_ESCALATED)  # 決定段の理由は不可
    with pytest.raises(EscalationContractError):
        DeliveryOutcome(DeliveryKind.BLOCKED)
    with pytest.raises(EscalationContractError):
        DeliveryOutcome(DeliveryKind.DELIVERED, _R.LOW_PRIORITY)


def test_summarize_never_drops_or_double_counts_a_detection() -> None:
    rng = random.Random(16)
    for _ in range(200):
        days = [rng.choice([None, *_RANKS]) for _ in range(rng.randint(0, 20))]

        def deliver(
            decision: EscalationDecision,
            rng: random.Random = rng,
            pool: list[EscalationSuppressionReason] = _DELIVERY_POOL,
        ) -> DeliveryOutcome:
            return DELIVERED if rng.random() < 0.5 else blocked(rng.choice(pool))

        records, _, sent = _run(days, deliver)
        counts = summarize(records)
        assert counts.detected == len(records)
        assert counts.delivered == len(sent)
        assert counts.detected == counts.delivered + counts.suppressed
        assert counts.decided_to_send == sum(1 for r in records if r.decision.would_send)
        assert sum(counts.suppressed_by_reason.values()) == counts.suppressed
        assert sum(counts.blocked_by_reason.values()) == (counts.decided_to_send - counts.delivered)


def test_summarize_empty_is_all_zero() -> None:
    c = summarize([])
    assert (c.detected, c.decided_to_send, c.delivered, c.suppressed) == (0, 0, 0, 0)
    assert dict(c.blocked_by_reason) == {} and dict(c.suppressed_by_reason) == {}


# --- 6 理由の語彙 -------------------------------------------------------------------------------


def test_reasons_split_into_decision_and_delivery_stage_without_overlap() -> None:
    assert DECISION_STAGE_REASONS.isdisjoint(DELIVERY_STAGE_REASONS)
    assert set(EscalationSuppressionReason) == DECISION_STAGE_REASONS | DELIVERY_STAGE_REASONS
    assert set(REASON_SOURCES) == set(EscalationSuppressionReason)


_ENUMS = {
    "SuppressionReason": vocabulary.SuppressionReason,
    "EligibilityBlockCategory": enums.EligibilityBlockCategory,
    "NotificationStatus": enums.NotificationStatus,
}


@pytest.mark.parametrize("reason", list(EscalationSuppressionReason))
def test_each_reason_maps_to_a_member_that_really_exists(
    reason: EscalationSuppressionReason,
) -> None:
    source = REASON_SOURCES[reason]
    if source is None:
        return
    enum_name, member = source
    assert enum_name in _ENUMS
    assert member in _ENUMS[enum_name].__members__  # 実在しない語(DAILY_CAP 等)を混ぜない


def test_the_c0_vocabulary_is_not_extended_and_has_no_unreal_words() -> None:
    assert "DAILY_CAP" not in {r.value for r in EscalationSuppressionReason}
    for r in EscalationSuppressionReason:
        assert r.value == r.name
    assert not hasattr(vocabulary.SuppressionReason, "DUPLICATE_SAME_SEVERITY")


# --- 7 identity ---------------------------------------------------------------------------------


_ANCHORS = [
    EscalationAnchor(dt.date(2026, 1, 5), dt.date(2026, 3, 2), Decimal("1500.5")),
    EscalationAnchor(None, dt.date(2026, 3, 2), Decimal("1500")),
    EscalationAnchor(dt.date(2026, 1, 5), None, Decimal("1500.50")),
    EscalationAnchor(dt.date(2026, 1, 5), dt.date(2026, 3, 2), None),
    EscalationAnchor(None, None, None),
]


@pytest.mark.parametrize("anchor", _ANCHORS)
def test_legacy_identity_equals_the_current_attention_event_identity(
    anchor: EscalationAnchor,
) -> None:
    """★ oracle(U-7)★: 現行の `_compute_attention_event_identity` と同値。"""
    recommendation = SimpleNamespace(
        profit_protection_basis_date=anchor.basis_date,
        profit_protection_peak_date=anchor.peak_date,
        profit_protection_peak_price=anchor.peak_price,
    )
    assert legacy_content_hash(anchor) == _compute_attention_event_identity(recommendation)  # type: ignore[arg-type]


def test_severity_identity_distinguishes_rank_and_anchor() -> None:
    anchor = _ANCHORS[0]
    hashes = {severity_content_hash(anchor, r) for r in _DETECTED}
    assert len(hashes) == len(_DETECTED)
    assert legacy_content_hash(anchor) not in hashes
    assert severity_content_hash(anchor, 1) != severity_content_hash(_ANCHORS[1], 1)
    # 旧形式と同じく、価格の表記が違えば別の局面(Decimal の str)
    assert severity_content_hash(_ANCHORS[0], 1) != severity_content_hash(
        EscalationAnchor(_ANCHORS[0].basis_date, _ANCHORS[0].peak_date, Decimal("1500.50")), 1
    )


def test_identity_is_deterministic_and_has_the_legacy_length() -> None:
    a = _ANCHORS[0]
    assert severity_content_hash(a, 2) == severity_content_hash(a, 2)
    assert len(legacy_content_hash(a)) == len(severity_content_hash(a, 2)) == ident.HASH_LENGTH


def test_identity_rejects_wrong_types() -> None:
    with pytest.raises(IdentityContractError):
        EscalationAnchor(dt.datetime(2026, 1, 1), None, None)
    with pytest.raises(IdentityContractError):
        EscalationAnchor(None, None, 1.5)  # type: ignore[arg-type]
    with pytest.raises(IdentityContractError):
        EscalationAnchor("2026-01-01", None, None)  # type: ignore[arg-type]
    with pytest.raises(IdentityContractError):
        severity_content_hash(_ANCHORS[0], True)
    with pytest.raises(IdentityContractError):
        severity_content_hash(_ANCHORS[0], "1")  # type: ignore[arg-type]


# --- 8 復元 -------------------------------------------------------------------------------------


@pytest.mark.parametrize("delivered", _DETECTED)
@pytest.mark.parametrize("today_rank", _DETECTED)
def test_restore_from_a_new_format_log_returns_the_delivered_rank(
    delivered: int, today_rank: int
) -> None:
    anchor = _ANCHORS[0]
    log = severity_content_hash(anchor, delivered)
    assert (
        restore_delivered_rank(log, anchor, today_rank=today_rank, candidate_ranks=_DETECTED)
        == delivered
    )


@pytest.mark.parametrize("today_rank", _DETECTED)
def test_restore_from_a_legacy_log_is_delivered_at_todays_rank_so_no_resend(
    today_rank: int,
) -> None:
    """★ U-7 案 (a) ★: 旧形式の log は移行日に今日の段階で配信済み → 移行日の再送 0。"""
    anchor = _ANCHORS[0]
    restored = restore_delivered_rank(
        legacy_content_hash(anchor),
        anchor,
        today_rank=today_rank,
        candidate_ranks=_DETECTED,
    )
    assert restored == today_rank
    decision = decide_escalation(today=_det(today_rank), max_delivered_rank=restored)
    assert not decision.would_send  # 移行日は送らない
    # 翌日以降に悪化したら送る
    if today_rank < MAX_RANK:
        assert decide_escalation(today=_det(today_rank + 1), max_delivered_rank=restored).would_send


def test_restore_returns_none_for_a_different_anchor_or_no_log() -> None:
    a, b = _ANCHORS[0], _ANCHORS[1]
    for log in (severity_content_hash(a, 2), legacy_content_hash(a), None, "0" * 16):
        got = restore_delivered_rank(log, b, today_rank=2, candidate_ranks=_DETECTED)
        assert got is None
    assert (
        restore_delivered_rank(None, a, today_rank=2, candidate_ranks=_DETECTED) is None
    )  # ログなし = 実績なし


def test_a_new_anchor_after_a_new_phase_starts_the_ratchet_over() -> None:
    """規則 7: anchor が変わった(= 新しい局面)ときは、実績なし(None)から最初の通知。"""
    old, new = _ANCHORS[0], _ANCHORS[2]
    last = severity_content_hash(old, 3)
    restored = restore_delivered_rank(last, new, today_rank=1, candidate_ranks=_DETECTED)
    assert restored is None
    assert decide_escalation(today=_det(1), max_delivered_rank=restored).kind is (
        EscalationKind.WOULD_SEND_FIRST
    )


def test_restore_prefers_the_new_format_when_candidates_overlap() -> None:
    anchor = _ANCHORS[0]
    log = severity_content_hash(anchor, 2)
    assert (
        restore_delivered_rank(log, anchor, today_rank=3, candidate_ranks=[2, 3, 1]) == 2
    )  # 旧形式の読み(=今日の 3)ではなく、新形式の 2


def test_restore_validates_the_rank_inputs() -> None:
    with pytest.raises(IdentityContractError):
        restore_delivered_rank("x", _ANCHORS[0], today_rank=True, candidate_ranks=[1])
    with pytest.raises(IdentityContractError):
        restore_delivered_rank("x", _ANCHORS[0], today_rank=1, candidate_ranks=["1"])  # type: ignore[list-item]


# --- 9 新しい数・純粋・dormant ------------------------------------------------------------------


def _constants(path: Path) -> list[Any]:
    return [
        n.value
        for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(n, ast.Constant) and isinstance(n.value, int | float | complex)
    ]


def test_escalation_has_no_new_numbers() -> None:
    """★ AC-6 ★: 小数・日数の定数を持たない。整数は 0 と 1(段階の始点と添字の補正)だけ。"""
    consts = _constants(_ESC_PATH)
    assert not [c for c in consts if isinstance(c, float | complex)]
    assert {c for c in consts if not isinstance(c, bool)} <= {0, 1}


def test_identity_has_no_decision_numbers() -> None:
    """identity の数は hash の長さ(旧形式の互換)だけ。"""
    consts = _constants(_IDENT_PATH)
    assert not [c for c in consts if isinstance(c, float | complex)]
    assert {c for c in consts if not isinstance(c, bool)} <= {ident.HASH_LENGTH}


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_identity_imports_only_pure_stdlib_and_nothing_from_the_engine() -> None:
    assert _imported_roots(_IDENT_PATH) <= {
        "__future__",
        "datetime",
        "hashlib",
        "collections",
        "dataclasses",
        "decimal",
        "typing",
    }


def test_neither_module_reads_a_clock_or_does_io() -> None:
    banned_attrs = {"now", "today", "utcnow", "open", "read_text", "getenv", "environ", "random"}
    banned_names = {"open", "print", "input", "exec", "eval"}
    for path in (_ESC_PATH, _IDENT_PATH):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute):
                assert node.attr not in banned_attrs, (path.name, node.attr)
            if isinstance(node, ast.Name):
                assert node.id not in banned_names, (path.name, node.id)


def test_nothing_in_src_imports_them_so_they_are_dormant() -> None:
    assert _importers_of("escalation", _ESC_PATH) == []
    assert _importers_of("attention_escalation_identity", _IDENT_PATH) == []


def test_module_exports_do_not_leak_a_clock_symbol() -> None:
    assert not hasattr(esc, "datetime") and not hasattr(esc, "time")


def test_next_max_delivered_rank_never_goes_down_even_for_a_lower_delivered_record() -> None:
    """決定からは作れない(送る決定は常に配信済みの最大より上)が、公開の関数としても減らさない。"""
    lower = DetectionRecord(EscalationDecision(EscalationKind.WOULD_SEND_FIRST, 1), DELIVERED)
    assert next_max_delivered_rank(MAX_RANK, lower) == MAX_RANK
    assert next_max_delivered_rank(None, lower) == 1
    assert next_max_delivered_rank(1, lower) == 1


def test_severity_identity_input_format_is_pinned() -> None:
    """新形式の入力(版・区切り・rank)を固定する。変えると既存の log の復元が壊れる。"""
    import hashlib

    anchor = _ANCHORS[0]
    raw = "v2|2026-01-05|2026-03-02|1500.5|rank=2"
    assert severity_content_hash(anchor, 2) == hashlib.sha256(raw.encode()).hexdigest()[:16]
