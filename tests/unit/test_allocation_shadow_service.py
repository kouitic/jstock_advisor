"""購入側 Shadow(Q')の入口 `observe_allocation_shadow` の契約テスト(Issue #603)。

必須条件(#128 …6100461428 の Q' の最終設計)を、入口の単位で固定する。

  (1) OFF は最初の判定で戻る(I/O・AuditLog・AvailableCash・計算・時計のどれにも触れない)
  (2) 例外を送出しない(計算・記録・残り時間の取得のどれが壊れても)
  (3) 残り時間の境界(119.9 / 120.0 / 120.1 秒)・不明のとき
  (4) 協調的な期限(30 秒)と、外部 I/O の打ち切り(5 秒)
  (5) 記録は決定的な audit_id の条件付き追記のみ(重複しない)
  (6) 記録する値に owner の実名・金額・株数・例外の文言を含めない

時計は注入する(wall clock・freezegun を使わない)。条件付き書込の DynamoDB 側の契約は
`test_allocation_shadow_conditional_write.py` で moto に対して固定する。
"""

from __future__ import annotations

import ast
import datetime as dt
import logging
import threading
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from jstock_advisor.domain.entities.available_cash import AvailableCash
from jstock_advisor.domain.entities.enums import (
    AvailableCashUpdateType,
    ExecutionMode,
    NotificationMode,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, log_ref
from jstock_advisor.domain.signals.allocation_shadow_config import (
    AllocationShadowConfig,
    AllocationShadowMode,
)
from jstock_advisor.services import allocation_shadow_service as svc
from jstock_advisor.services.allocation_shadow_service import (
    DECISION_TYPE,
    SKIP_ID_PREFIX,
    CashReadStatus,
    Deadline,
    IoTimeoutError,
    ShadowOutcome,
    ShadowResult,
    ShadowRun,
    SkipReason,
    call_with_timeout,
    compute_not_implemented,
    observe_allocation_shadow,
    read_available_cash,
    result_audit_id,
    skip_audit_id,
)

_NOW = dt.datetime(2026, 10, 12, 8, 0, tzinfo=dt.UTC)
_BATCH = "buy-candidates-2026-10-12"
_NORMAL = ExecutionContext.normal()
_SHADOW = AllocationShadowConfig(mode=AllocationShadowMode.SHADOW)
_OFF = AllocationShadowConfig(mode=AllocationShadowMode.OFF)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVICE_PATH = _REPO_ROOT / "src" / "jstock_advisor" / "services" / "allocation_shadow_service.py"


def _ms(seconds: float) -> Callable[[], int]:
    return lambda: round(seconds * 1000)


_PLENTY_OF_TIME = _ms(300)


class _Clock:
    """注入する monotonic。advance() で進めた分だけ進む。"""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _FakeAudit:
    """record_if_absent だけを持つ。同じ audit_id は None(= 既存)を返す。"""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.calls = 0
        self._ids: set[str] = set()

    def record_if_absent(self, audit_id: str, decision_type: str, **kwargs: Any) -> Any:
        self.calls += 1
        if audit_id in self._ids:
            return None
        self._ids.add(audit_id)
        self.records.append({"audit_id": audit_id, "decision_type": decision_type, **kwargs})
        return object()


class _TripwireAudit:
    """何かに触れたら失敗する(OFF が I/O に入らないことの証明)。"""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"audit must not be touched: {name}")


def _tripwire(*_a: object, **_kw: object) -> Any:
    raise AssertionError("must not be called")


def _observe(
    audit: Any,
    *,
    config: AllocationShadowConfig = _SHADOW,
    remaining_ms: Callable[[], int] | None = _PLENTY_OF_TIME,
    compute: Callable[[ShadowRun], ShadowResult] | None = None,
    owners: list[str] | None = None,
    clock: Callable[[], float] | None = None,
    execution_context: ExecutionContext = _NORMAL,
    batch_id: str | None = _BATCH,
) -> bool:
    return observe_allocation_shadow(
        batch_id=batch_id,
        now=_NOW,
        execution_context=execution_context,
        audit_service=audit,
        remaining_time_ms=remaining_ms,
        shadow_config=config,
        compute=compute,
        owners=owners,
        monotonic=clock or _Clock(),
    )


# --- (1) OFF は最初の判定で戻る --------------------------------------------------------


def test_off_touches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "_default_cash_getter", _tripwire)

    recorded = _observe(
        _TripwireAudit(),
        config=_OFF,
        remaining_ms=_tripwire,  # 残り時間も読まない
        compute=_tripwire,
        clock=_tripwire,  # 時計も読まない
    )

    assert recorded is False


def test_shipped_config_is_off_so_default_entry_touches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shadow_config を渡さない本番の呼び方 = 出荷 config(OFF)を読んで、何もせず戻る。"""
    monkeypatch.setattr(svc, "_default_cash_getter", _tripwire)

    recorded = observe_allocation_shadow(
        batch_id=_BATCH,
        now=_NOW,
        execution_context=_NORMAL,
        audit_service=_TripwireAudit(),  # type: ignore[arg-type]
        remaining_time_ms=_tripwire,
        compute=_tripwire,
        monotonic=_tripwire,
    )

    assert recorded is False


def test_off_writes_no_extra_audit_record() -> None:
    audit = _FakeAudit()

    _observe(audit, config=_OFF)

    assert audit.calls == 0 and audit.records == []


# --- 非通常実行・batch_id なし ----------------------------------------------------------


@pytest.mark.parametrize(
    "context",
    [
        ExecutionContext(mode=ExecutionMode.VALIDATION),
        ExecutionContext(mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.DRY_RUN),
    ],
)
def test_non_normal_execution_writes_nothing(context: ExecutionContext) -> None:
    audit = _FakeAudit()

    recorded = _observe(audit, execution_context=context, compute=_tripwire, remaining_ms=_tripwire)

    assert recorded is False
    assert audit.calls == 0


def test_missing_batch_id_writes_nothing() -> None:
    audit = _FakeAudit()

    assert _observe(audit, batch_id=None, compute=_tripwire) is False
    assert audit.calls == 0


# --- 既定の差し込み先 --------------------------------------------------------------------


def test_default_compute_records_not_implemented() -> None:
    audit = _FakeAudit()

    recorded = _observe(audit)

    assert recorded is True
    [record] = audit.records
    assert record["decision_type"] == DECISION_TYPE
    assert record["stock_code"] is None
    assert record["output_values"] == {
        "outcome": "SKIPPED",
        "reason": "COMPUTE_NOT_IMPLEMENTED",
    }
    assert compute_not_implemented(
        ShadowRun(_BATCH, DEFAULT_OWNER, Deadline(30.0, _Clock()), 5.0)
    ) == ShadowResult(ShadowOutcome.SKIPPED, SkipReason.COMPUTE_NOT_IMPLEMENTED)


def test_audit_ids_are_deterministic_separate_for_result_and_skip_and_hide_the_owner() -> None:
    skipped = _FakeAudit()
    computed = _FakeAudit()

    _observe(skipped, owners=[DEFAULT_OWNER])  # 既定の計算 = 未実装 = スキップ
    _observe(
        computed,
        owners=[DEFAULT_OWNER],
        compute=lambda run: ShadowResult(ShadowOutcome.COMPUTED),
    )

    [skip_record] = skipped.records
    [result_record] = computed.records
    assert skip_record["audit_id"] == f"{SKIP_ID_PREFIX}:{_BATCH}:{log_ref(DEFAULT_OWNER)}"
    assert result_record["audit_id"] == f"{DECISION_TYPE}:{_BATCH}:{log_ref(DEFAULT_OWNER)}"
    assert skip_record["audit_id"] == skip_audit_id(_BATCH, DEFAULT_OWNER)
    assert result_record["audit_id"] == result_audit_id(_BATCH, DEFAULT_OWNER)
    assert skip_record["audit_id"] != result_record["audit_id"]  # 別の鍵(Q-B)
    assert skip_record["decision_type"] == result_record["decision_type"] == DECISION_TYPE
    for record in (skip_record, result_record):
        assert DEFAULT_OWNER not in repr(record)  # 実名はどこにも出ない
        assert record["input_values"] == {"batch_id": _BATCH, "owner_ref": log_ref(DEFAULT_OWNER)}


def test_a_skip_and_a_later_result_coexist_and_each_is_recorded_once() -> None:
    """捨てたワーカーが後から成功した場合(Q-B): スキップが結果を塞がず、2 件が並ぶ。"""
    audit = _FakeAudit()

    skipped = _observe(audit, remaining_ms=_ms(119.9), compute=_tripwire)
    resulted = _observe(audit, compute=lambda run: ShadowResult(ShadowOutcome.COMPUTED))
    skipped_again = _observe(audit, remaining_ms=_ms(119.9), compute=_tripwire)
    resulted_again = _observe(audit, compute=lambda run: ShadowResult(ShadowOutcome.COMPUTED))

    assert (skipped, resulted, skipped_again, resulted_again) == (True, True, False, False)
    assert [r["audit_id"] for r in audit.records] == [
        skip_audit_id(_BATCH, DEFAULT_OWNER),
        result_audit_id(_BATCH, DEFAULT_OWNER),
    ]


def test_failed_outcome_uses_the_skip_id() -> None:
    audit = _FakeAudit()

    def boom(run: ShadowRun) -> ShadowResult:
        raise ValueError("x")

    _observe(audit, compute=boom)

    assert audit.records[0]["audit_id"] == skip_audit_id(_BATCH, DEFAULT_OWNER)


def test_same_batch_and_owner_records_once() -> None:
    audit = _FakeAudit()

    first = _observe(audit)
    second = _observe(audit)

    assert (first, second) == (True, False)
    assert len(audit.records) == 1


def test_one_record_per_owner_and_one_write_each() -> None:
    audit = _FakeAudit()

    _observe(audit, owners=["owner-a", "owner-b", "owner-c"])

    assert len(audit.records) == 3 and audit.calls == 3
    assert len({r["audit_id"] for r in audit.records}) == 3


# --- (3) 残り時間の境界 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("remaining_seconds", "computes"),
    [(119.9, False), (120.0, True), (120.1, True), (0.0, False), (900.0, True)],
)
def test_time_guard_boundary(remaining_seconds: float, computes: bool) -> None:
    audit = _FakeAudit()
    ran: list[int] = []

    def compute(run: ShadowRun) -> ShadowResult:
        ran.append(1)
        return ShadowResult(ShadowOutcome.COMPUTED)

    recorded = _observe(audit, remaining_ms=_ms(remaining_seconds), compute=compute)

    assert recorded is True  # どちらでも理由か結果を 1 件記録する
    assert (ran == [1]) is computes
    [record] = audit.records
    if computes:
        assert record["output_values"]["outcome"] == "COMPUTED"
    else:
        assert record["output_values"] == {"outcome": "SKIPPED", "reason": "TIME_BUDGET"}


def test_time_guard_follows_the_configured_minimum() -> None:
    audit = _FakeAudit()
    config = AllocationShadowConfig(mode=AllocationShadowMode.SHADOW, min_remaining_seconds=200)

    _observe(audit, config=config, remaining_ms=_ms(199.9), compute=_tripwire)

    assert audit.records[0]["output_values"]["reason"] == "TIME_BUDGET"


def test_unknown_remaining_time_skips_fail_closed() -> None:
    audit = _FakeAudit()

    _observe(audit, remaining_ms=None, compute=_tripwire)

    assert audit.records[0]["output_values"] == {"outcome": "SKIPPED", "reason": "TIME_BUDGET"}


def test_remaining_time_that_raises_skips_fail_closed() -> None:
    audit = _FakeAudit()

    def broken() -> int:
        raise RuntimeError("no context")

    _observe(audit, remaining_ms=broken, compute=_tripwire)

    assert audit.records[0]["output_values"] == {"outcome": "SKIPPED", "reason": "TIME_BUDGET"}


# --- (2) 例外を送出しない -----------------------------------------------------------------


def test_compute_exception_is_recorded_by_type_only() -> None:
    audit = _FakeAudit()

    def compute(run: ShadowRun) -> ShadowResult:
        raise RuntimeError("account 12345678 holds 9999 shares")

    recorded = _observe(audit, compute=compute)

    assert recorded is True
    [record] = audit.records
    assert record["output_values"] == {
        "outcome": "FAILED",
        "reason": "COMPUTATION_FAILED",
        "error_type": "RuntimeError",
    }
    assert "12345678" not in repr(record) and "9999" not in repr(record)


def test_audit_write_failure_is_isolated_and_logged_by_type_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Exploding:
        def record_if_absent(self, *_a: object, **_kw: object) -> Any:
            raise RuntimeError("secret table name")

    caplog.set_level(logging.WARNING, logger=svc.__name__)

    recorded = _observe(_Exploding())

    assert recorded is False
    messages = " ".join(r.getMessage() for r in caplog.records if r.name == svc.__name__)
    assert "RuntimeError" in messages
    assert "secret" not in messages


def test_unexpected_error_in_the_frame_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_kw: object) -> bool:
        raise RuntimeError("frame bug")

    monkeypatch.setattr(svc, "_observe", boom)

    assert _observe(_FakeAudit()) is False  # 例外にならない


def test_entry_never_raises_even_when_everything_is_broken() -> None:
    def broken_compute(run: ShadowRun) -> ShadowResult:
        raise ValueError("x")

    def broken_clock() -> float:
        raise OSError("clock")

    class _Exploding:
        def record_if_absent(self, *_a: object, **_kw: object) -> Any:
            raise OSError("down")

    assert (
        _observe(_Exploding(), compute=broken_compute, clock=broken_clock, remaining_ms=None)
        is False
    )


# --- (4) 協調的な期限(30 秒)と I/O の打ち切り -------------------------------------------------


def test_deadline_stops_the_remaining_owners(caplog: pytest.LogCaptureFixture) -> None:
    audit = _FakeAudit()
    clock = _Clock()
    seen: list[str] = []

    def compute(run: ShadowRun) -> ShadowResult:
        seen.append(run.owner)
        clock.advance(30.0)  # 1 人目で総予算(30 秒)を使い切る
        return ShadowResult(ShadowOutcome.COMPUTED)

    caplog.set_level(logging.WARNING, logger=svc.__name__)

    _observe(audit, owners=["owner-a", "owner-b", "owner-c"], compute=compute, clock=clock)

    assert seen == ["owner-a"]
    # 予算が尽きた時点の 1 件(結果)だけを書き、残りの owner は件数をログに出すのみ
    [record] = audit.records
    assert record["output_values"]["outcome"] == "COMPUTED"
    messages = " ".join(r.getMessage() for r in caplog.records if r.name == svc.__name__)
    assert "2 owner(s) not recorded" in messages


def test_total_budget_bounds_the_number_of_writes_across_owners() -> None:
    """Q-G: 30 秒は owner ごとでなく区間全体の総予算。各書込が 5 秒を消費する fake で、
    8 人の owner に対する書込が総予算で頭打ちになる(N x (5 + 5) 秒にならない)。"""
    clock = _Clock()

    class _FiveSecondAudit(_FakeAudit):
        def record_if_absent(self, audit_id: str, decision_type: str, **kwargs: Any) -> Any:
            clock.advance(5.0)
            return super().record_if_absent(audit_id, decision_type, **kwargs)

    audit = _FiveSecondAudit()

    _observe(audit, owners=[f"owner-{i}" for i in range(8)], clock=clock)

    # 6 回で 30 秒を使い切り、7 回目は『予算切れ』を示す TIME_LIMIT の 1 件だけ。8 人目は書かない
    assert audit.calls == 7
    assert audit.records[-1]["output_values"] == {"outcome": "SKIPPED", "reason": "TIME_LIMIT"}
    assert audit.records[-1]["audit_id"].startswith(SKIP_ID_PREFIX)


def test_each_write_waits_for_the_smaller_of_io_limit_and_remaining_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    waits: list[float] = []
    real = svc.call_with_timeout

    def spy(fn: Callable[[], Any], timeout_seconds: float) -> Any:
        waits.append(timeout_seconds)
        return real(fn, timeout_seconds)

    audit = _FakeAudit()

    def compute(run: ShadowRun) -> ShadowResult:
        clock.advance(27.0)  # 総予算の残り 3 秒(io の上限 5 秒より小さい)
        return ShadowResult(ShadowOutcome.COMPUTED)

    monkeypatch.setattr(svc, "call_with_timeout", spy)
    _observe(audit, compute=compute, clock=clock)

    assert waits == [pytest.approx(3.0)]


def test_remaining_time_is_read_for_every_owner_not_once() -> None:
    """Q-F: 1 人目は十分・2 人目は不足 -> 2 人目だけがスキップされる(使い回さない)。"""
    audit = _FakeAudit()
    readings = iter([300_000, 119_900])
    ran: list[str] = []

    def compute(run: ShadowRun) -> ShadowResult:
        ran.append(run.owner)
        return ShadowResult(ShadowOutcome.COMPUTED)

    _observe(
        audit,
        owners=["owner-a", "owner-b"],
        compute=compute,
        remaining_ms=lambda: next(readings),
    )

    assert ran == ["owner-a"]
    reasons = [r["output_values"]["reason"] for r in audit.records]
    assert reasons == [None, "TIME_BUDGET"]


def test_deadline_just_before_the_limit_still_runs_the_next_owner() -> None:
    audit = _FakeAudit()
    clock = _Clock()
    seen: list[str] = []

    def compute(run: ShadowRun) -> ShadowResult:
        seen.append(run.owner)
        clock.advance(29.9)
        return ShadowResult(ShadowOutcome.COMPUTED)

    _observe(audit, owners=["owner-a", "owner-b"], compute=compute, clock=clock)

    assert seen == ["owner-a", "owner-b"]  # 29.9 秒では打ち切らない


def test_deadline_object_is_cooperative_not_preemptive() -> None:
    clock = _Clock()
    deadline = Deadline(30.0, clock)

    assert deadline.expired() is False
    clock.advance(29.999)
    assert deadline.expired() is False
    clock.advance(0.001)
    assert deadline.expired() is True
    assert deadline.remaining() == 0.0
    clock.advance(100)
    assert deadline.remaining() == 0.0  # 負にならない


def test_call_with_timeout_returns_the_value_and_reraises_errors() -> None:
    assert call_with_timeout(lambda: 42, 1.0) == 42

    def boom() -> int:
        raise KeyError("k")

    with pytest.raises(KeyError):
        call_with_timeout(boom, 1.0)


def test_call_with_timeout_abandons_a_stuck_call() -> None:
    release = threading.Event()
    try:
        with pytest.raises(IoTimeoutError):
            call_with_timeout(lambda: release.wait(10), 0.05)
    finally:
        release.set()  # 後始末(daemon スレッドを解放する)


def test_stuck_audit_write_does_not_hang_the_entry() -> None:
    release = threading.Event()

    class _Stuck:
        def record_if_absent(self, *_a: object, **_kw: object) -> Any:
            release.wait(10)

    config = AllocationShadowConfig(mode=AllocationShadowMode.SHADOW, io_timeout_seconds=0.05)
    try:
        assert _observe(_Stuck(), config=config) is False  # 待ち切れず、例外にもならない
    finally:
        release.set()


# --- AvailableCash の読取(期限つき・状態の分類)--------------------------------------------


def _cash(amount: str) -> AvailableCash:
    return AvailableCash(
        owner=DEFAULT_OWNER,
        available_cash=Decimal(amount),
        updated_at=_NOW,
        last_update_type=AvailableCashUpdateType.USER_RECONCILIATION,
    )


def test_cash_read_available_and_zero_is_registered() -> None:
    result = read_available_cash(DEFAULT_OWNER, timeout_seconds=1.0, get=lambda o: _cash("0"))

    assert result.status is CashReadStatus.AVAILABLE
    assert result.amount == Decimal("0")  # 登録された 0 円は未登録ではない
    assert result.as_skip_reason() is None


def test_cash_read_none_is_not_registered_not_zero() -> None:
    result = read_available_cash(DEFAULT_OWNER, timeout_seconds=1.0, get=lambda o: None)

    assert result.status is CashReadStatus.NOT_REGISTERED
    assert result.amount is None
    assert result.as_skip_reason() is SkipReason.CASH_NOT_REGISTERED


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "GetItem")


@pytest.mark.parametrize("code", ["AccessDenied", "AccessDeniedException", "UnauthorizedOperation"])
def test_cash_read_access_denied_is_iam_missing(code: str) -> None:
    def get(owner: str) -> AvailableCash | None:
        raise _client_error(code)

    result = read_available_cash(DEFAULT_OWNER, timeout_seconds=1.0, get=get)

    assert result.status is CashReadStatus.IAM_MISSING
    assert result.as_skip_reason() is SkipReason.IAM_MISSING


def test_cash_read_other_errors_are_classified_without_raising() -> None:
    def throttled(owner: str) -> AvailableCash | None:
        raise _client_error("ProvisionedThroughputExceededException")

    def broken(owner: str) -> AvailableCash | None:
        raise ValueError("x")

    for getter in (throttled, broken):
        result = read_available_cash(DEFAULT_OWNER, timeout_seconds=1.0, get=getter)
        assert result.status is CashReadStatus.ERROR
        assert result.as_skip_reason() is SkipReason.COMPUTATION_FAILED


def test_cash_read_timeout_is_classified() -> None:
    release = threading.Event()
    try:
        result = read_available_cash(
            DEFAULT_OWNER, timeout_seconds=0.05, get=lambda o: release.wait(10) and None
        )
    finally:
        release.set()

    assert result.status is CashReadStatus.TIMEOUT
    assert result.as_skip_reason() is SkipReason.TIME_LIMIT


def test_run_cash_read_waits_for_the_smaller_of_io_limit_and_remaining_deadline() -> None:
    clock = _Clock()
    deadline = Deadline(30.0, clock)
    clock.advance(30.0)  # 期限切れ
    run = ShadowRun(_BATCH, DEFAULT_OWNER, deadline, io_timeout_seconds=5.0)

    result = run.read_available_cash(get=_tripwire)  # 呼ばずに TIMEOUT

    assert result.status is CashReadStatus.TIMEOUT


def test_run_cash_read_uses_the_getter_once() -> None:
    calls: list[str] = []

    def get(owner: str) -> AvailableCash | None:
        calls.append(owner)
        return _cash("100")

    run = ShadowRun(_BATCH, DEFAULT_OWNER, Deadline(30.0, _Clock()), io_timeout_seconds=5.0)

    assert run.read_available_cash(get=get).status is CashReadStatus.AVAILABLE
    assert calls == [DEFAULT_OWNER]


# --- (6) 記録する値 ------------------------------------------------------------------------


def test_shadow_result_requires_a_reason_unless_computed() -> None:
    with pytest.raises(ValueError, match="理由"):
        ShadowResult(ShadowOutcome.SKIPPED)
    with pytest.raises(ValueError, match="理由"):
        ShadowResult(ShadowOutcome.FAILED)
    with pytest.raises(ValueError, match="理由"):
        ShadowResult(ShadowOutcome.COMPUTED, SkipReason.TIME_LIMIT)
    ShadowResult(ShadowOutcome.COMPUTED)  # 例外にならない


def test_shadow_result_facts_must_be_short_scalars() -> None:
    ShadowResult(ShadowOutcome.COMPUTED, facts={"n": 3, "ok": True, "ratio": 0.5, "code": "A"})
    with pytest.raises(ValueError, match="scalar"):
        ShadowResult(ShadowOutcome.COMPUTED, facts={"x": [1]})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="scalar"):
        ShadowResult(ShadowOutcome.COMPUTED, facts={"x": Decimal("1")})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="長すぎる"):
        ShadowResult(ShadowOutcome.COMPUTED, facts={"x": "a" * 65})


def test_computed_facts_are_recorded_verbatim() -> None:
    audit = _FakeAudit()

    def compute(run: ShadowRun) -> ShadowResult:
        return ShadowResult(ShadowOutcome.COMPUTED, facts={"candidates": 3, "allocated": True})

    _observe(audit, compute=compute)

    assert audit.records[0]["output_values"] == {
        "outcome": "COMPUTED",
        "reason": None,
        "candidates": 3,
        "allocated": True,
    }


def test_one_failed_record_does_not_stop_the_other_owners() -> None:
    """記録の失敗は owner 単位で握る(1 人目の失敗で 2 人目以降の記録を失わない)。"""

    class _FirstFails(_FakeAudit):
        def record_if_absent(self, audit_id: str, decision_type: str, **kwargs: Any) -> Any:
            if log_ref("owner-a") in audit_id:
                raise RuntimeError("down")
            return super().record_if_absent(audit_id, decision_type, **kwargs)

    audit = _FirstFails()

    recorded = _observe(audit, owners=["owner-a", "owner-b"])

    assert recorded is True
    assert [r["audit_id"] for r in audit.records] == [skip_audit_id(_BATCH, "owner-b")]


def test_off_makes_zero_aws_or_socket_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Q-D ①: 入口の fake だけでなく、どの経路でも I/O が 0 回(#908 と同じ形)。"""
    import socket

    import boto3

    calls: list[str] = []

    def tripwire(name: str) -> Callable[..., Any]:
        def _raise(*_a: object, **_kw: object) -> Any:
            calls.append(name)
            raise AssertionError(f"I/O attempted while OFF: {name}")

        return _raise

    for target, attr in (
        (boto3, "client"),
        (boto3, "resource"),
        (boto3, "Session"),
        (socket.socket, "connect"),
        (socket, "create_connection"),
    ):
        monkeypatch.setattr(target, attr, tripwire(f"{getattr(target, '__name__', '')}.{attr}"))

    # shadow_config を渡さない = 本番の呼び方(出荷 config = OFF を読む)
    recorded = observe_allocation_shadow(
        batch_id=_BATCH,
        now=_NOW,
        execution_context=_NORMAL,
        audit_service=_TripwireAudit(),  # type: ignore[arg-type]
        remaining_time_ms=_tripwire,
        compute=_tripwire,
    )

    assert recorded is False
    assert calls == []


# --- 静的な契約(AST)-----------------------------------------------------------------------


def _service_tree() -> ast.Module:
    return ast.parse(_SERVICE_PATH.read_text(encoding="utf-8"))


def test_service_has_no_wall_clock_or_unconditional_write() -> None:
    attributes = {
        node.attr for node in ast.walk(_service_tree()) if isinstance(node, ast.Attribute)
    }

    # 時計は注入された monotonic だけ(wall clock を読まない。引数名 now は時計の読取ではない)
    assert not attributes & {"now", "utcnow", "today", "time", "time_ns", "perf_counter", "sleep"}
    # 記録は条件付き追記のみ。無条件の書込・読取・走査の API を呼ばない
    assert "record_if_absent" in attributes
    assert not attributes & {"record", "save", "put_item", "update_item", "delete_item"}
    assert not attributes & {"scan", "query", "get_item", "batch_get_item", "transact_write_items"}


def test_service_does_not_import_boto_clients_or_other_domains() -> None:
    imported: set[str] = set()
    for node in ast.walk(_service_tree()):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert "boto3" not in imported
    assert not any(m.startswith("jstock_advisor.domain.exit_architecture") for m in imported)
    assert not any(m.startswith("jstock_advisor.lambda_handlers") for m in imported)


def _first_statements(fn: ast.FunctionDef) -> list[ast.stmt]:
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # docstring
    return body


def test_off_decision_is_the_first_statement_after_loading_the_config() -> None:
    """Q-D ②: OFF の判定は、設定を読む代入の直後の第 2 文(I/O・計算・記録より前)。"""
    [fn] = [
        n
        for n in ast.walk(_service_tree())
        if isinstance(n, ast.FunctionDef) and n.name == "observe_allocation_shadow"
    ]
    first, second, *rest = _first_statements(fn)

    assert isinstance(first, ast.Assign)
    assert [t.id for t in first.targets if isinstance(t, ast.Name)] == ["config"]
    assert isinstance(second, ast.If)
    assert ast.unparse(second.test) == "not config.enabled"
    assert len(second.body) == 1 and isinstance(second.body[0], ast.Return)
    assert not second.orelse
    assert rest  # OFF の判定の後ろに本体がある(判定が末尾へ動かされていない)


def test_worker_thread_is_written_so_that_returning_never_waits_for_it() -> None:
    """Q-C: 『捨てて戻る』が崩れる書き方を、module 全走査で禁止する(時間の assert に頼らない)。"""
    tree = _service_tree()
    forbidden_imports: list[str] = []
    forbidden_attrs: list[str] = []
    joins: list[ast.Call] = []
    threads: list[ast.Call] = []
    with_calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            forbidden_imports += [a.name for a in node.names if "concurrent" in a.name]
        elif isinstance(node, ast.ImportFrom):
            if node.module and "concurrent" in node.module:
                forbidden_imports.append(node.module)
            forbidden_imports += [a.name for a in node.names if "Executor" in a.name]
        elif isinstance(node, ast.Attribute):
            if node.attr in {"shutdown", "result", "submit", "map"}:
                forbidden_attrs.append(node.attr)
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "join":
                joins.append(node)
            elif name == "Thread":
                threads.append(node)
        elif isinstance(node, ast.With):
            with_calls += [ast.unparse(item.context_expr) for item in node.items]

    assert forbidden_imports == []  # ThreadPoolExecutor / concurrent.futures を使わない
    assert forbidden_attrs == []  # shutdown(wait=True) / timeout なしの result() の余地を残さない
    assert joins, "join(timeout) が見つからない(待ち方が変わった)"
    assert all(len(j.args) + len(j.keywords) >= 1 for j in joins)  # timeout なしの join は無い
    assert threads, "daemon スレッドが見つからない"
    for call in threads:
        daemon = [k for k in call.keywords if k.arg == "daemon"]
        assert len(daemon) == 1
        assert isinstance(daemon[0].value, ast.Constant) and daemon[0].value.value is True
    # 待ちを伴いうる with(スレッド・executor の context manager)を使わない
    assert not [w for w in with_calls if "Thread" in w or "Executor" in w or "Pool" in w]
