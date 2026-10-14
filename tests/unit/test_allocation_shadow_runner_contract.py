"""購入側 Shadow の観測のループ `allocation_shadow_runner` の契約テスト(Issue #603 PR-F2-2b)。

設計 rev5・rev6(#603 …6160343374・…6160476713)と、USER の品質条件(#925 …6159386050)を固定する。

  1  新規の記録は kv1 だけ(audit_id・owner_ref・索引)。平文の owner・kv0 は出ない
  2  鍵が取れない日は owner 単位の記録 0 件 + 日単位の skip 1 件(鍵の失敗 6 分類を 6 通り)
  3  バッチ共通の入力が作れない日・残り時間が足りない日も、理由つきの日単位の skip(索引で代替しない)
  4  鍵は run 内で 1 回・期限内の成功だけ保持・最初の失敗で打ち切る
  5  索引は全 owner の記録の後。実際に記録が存在する owner だけを載せ、書けなかった分は理由別の件数
  6  16 を超える owner は記録対象にしない(OWNER_CAP)。打ち切った日の件数を二重に数えない
  7  例外・書込の失敗・総予算の超過のどれも呼び出し側へ出さない
  8  再実行は先着(書込は条件付き)。値・実名・保有 ID がログ・記録に出ない

CLI の画面モデルで読み戻し、書込側と読み手が噛み合うことも確かめる。
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.domain.entities.identifier_ref import (
    KeyedOwnerRef,
    KeyMaterial,
    KeyUnavailable,
    KeyUnavailableReason,
    keyed_owner_ref,
    parse_keyed_ref,
)
from jstock_advisor.domain.notification.allocation_shadow_cli_renderer import (
    render_json,
    render_table,
)
from jstock_advisor.domain.notification.allocation_shadow_cli_view import (
    DayInput,
    DayState,
    RecordInput,
    RecordState,
    build_screen,
)
from jstock_advisor.domain.signals.allocation_policy_config import load_allocation_policy
from jstock_advisor.domain.signals.allocation_shadow_record import (
    DAY_SKIP_REASONS,
    CashSnapshot,
    RecordMeta,
    build_header_record,
    day_skip_audit_id,
    index_audit_id,
    items_audit_id,
    validate_day_skip_record,
    validate_index_record,
    validate_items_record,
)
from jstock_advisor.services import allocation_shadow_runner as runner_module
from jstock_advisor.services.allocation_shadow_compute import BatchUnavailable, OwnerComputation
from jstock_advisor.services.allocation_shadow_runner import (
    DAY_SKIP_MARKER,
    INDEX_NOT_WRITTEN_MARKER,
    DeadlineBoundKeyProvider,
    make_runner,
)
from jstock_advisor.services.allocation_shadow_service import (
    Deadline,
    ShadowOutcome,
    ShadowResult,
    ShadowRunContext,
    ShadowSkipReason,
)

_REPO = Path(__file__).resolve().parents[2]
_RUNNER_PATH = _REPO / "src" / "jstock_advisor" / "services" / "allocation_shadow_runner.py"
_NOW = dt.datetime(2026, 10, 14, 0, 30, tzinfo=dt.UTC)  # JST 2026-10-14 09:30
_DATE = dt.date(2026, 10, 14)
_BATCH = "buy-candidates-20261014"
_KEY = KeyMaterial(key_version=1, key=b"t" * 32)  # テスト用の鍵(秘密ではない)
_SENTINEL = "owner-SENTINEL-plain-name-7c1d"  # owner の平文(どこにも出てはならない)
_OWNER_BASE = "owner-%02d"


# --- fake ------------------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Audit:
    """record_if_absent だけを持つ in-memory の AuditLog。同じ audit_id は None(= 既存)。"""

    def __init__(self, fail: Callable[[str, str], BaseException | None] | None = None) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.order: list[tuple[str, str]] = []  # (decision_type, audit_id)。到着順
        self.calls = 0
        self._fail = fail
        self._lock = threading.Lock()

    def record_if_absent(self, audit_id: str, decision_type: str, **kwargs: Any) -> Any:
        with self._lock:
            self.calls += 1
            if self._fail is not None:
                error = self._fail(decision_type, audit_id)
                if error is not None:
                    raise error
            if audit_id in self.records:
                return None
            self.records[audit_id] = {"decision_type": decision_type, **kwargs}
            self.order.append((decision_type, audit_id))
            return object()

    def of(self, decision_type: str) -> list[dict[str, Any]]:
        return [r for r in self.records.values() if r["decision_type"] == decision_type]


class _Provider:
    def __init__(self, result: Any = _KEY) -> None:
        self.result = result
        self.calls = 0

    def get(self) -> Any:
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _bundle() -> Any:
    return load_allocation_policy(minimum_trading_unit=100)


def _header(owner_ref: KeyedOwnerRef) -> dict[str, Any]:
    meta = RecordMeta(
        batch_id=_BATCH,
        business_date=_DATE,
        owner_ref=owner_ref,
        shadow_mode="SHADOW",
        raer_model_version="raer-combined-v1-provisional",
        coefficient_set_id="cset-test-0123456789abcdef-v1",
        elapsed_ms=None,
        read_states={"holdings": "OK", "recommendations": "OK", "cash": "NOT_REGISTERED"},
    )
    return build_header_record(
        meta=meta,
        bundle=_bundle(),
        cash=CashSnapshot(None, None, None),
        outcome="SKIPPED",
        skip_reason="CASH_NOT_REGISTERED",
    )


def _computed(owner_ref: KeyedOwnerRef, with_record: bool = True) -> OwnerComputation:
    result = ShadowResult(ShadowOutcome.SKIPPED, ShadowSkipReason.CASH_NOT_REGISTERED)
    return OwnerComputation(result=result, record=_header(owner_ref) if with_record else None)


class _Compute:
    def __init__(self, *, on_call: Callable[[str], Any] | None = None) -> None:
        self.owners: list[str] = []
        self._on_call = on_call

    def __call__(
        self,
        inputs: Any,
        owner: str,
        owner_ref: KeyedOwnerRef,
        deadline: Deadline,
        io_timeout_seconds: float,
        elapsed_ms: int | None,
    ) -> OwnerComputation:
        self.owners.append(owner)
        if self._on_call is not None:
            outcome = self._on_call(owner)
            if isinstance(outcome, OwnerComputation):
                return outcome
        return _computed(owner_ref)


def _owners(count: int) -> list[str]:
    return [_OWNER_BASE % n for n in range(count)]


def _prepare(owners: list[str]) -> Callable[[Deadline, float], Any]:
    return lambda deadline, io: SimpleNamespace(owners=tuple(sorted(owners)))


def _ctx(
    audit: Any,
    *,
    clock: Callable[[], float] | None = None,
    io_timeout: float = 1.0,
    limit: float = 30.0,
    precheck: Callable[[], ShadowSkipReason | None] | None = None,
    now: dt.datetime = _NOW,
) -> ShadowRunContext:
    return ShadowRunContext(
        batch_id=_BATCH,
        now=now,
        audit_service=audit,
        deadline=Deadline(limit, clock or _Clock()),
        io_timeout_seconds=io_timeout,
        shadow_mode="SHADOW",
        precheck=precheck or (lambda: None),
    )


def _run(
    audit: Any,
    owners: list[str],
    *,
    provider: Any = None,
    compute: Any = None,
    prepare: Any = None,
    **ctx_kwargs: Any,
) -> tuple[bool, _Compute, Any]:
    compute = compute or _Compute()
    provider = provider if provider is not None else _Provider()
    runner = make_runner(
        prepare=prepare or _prepare(owners), compute_owner=compute, key_provider=provider
    )
    return runner(_ctx(audit, **ctx_kwargs)), compute, provider


def _ref(owner: str) -> KeyedOwnerRef:
    return keyed_owner_ref(owner, _KEY)


def _read_back(audit: _Audit, refs: list[str] | None = None) -> Any:
    """保存された記録を、CLI の画面モデルで読み戻す(書込側と読み手が噛み合うことの確認)。"""

    def found(audit_id: str) -> RecordInput:
        entry = audit.records.get(audit_id)
        if entry is None:
            return RecordInput(RecordState.NOT_FOUND)
        return RecordInput(RecordState.FOUND, entry["output_values"])

    index = found(index_audit_id(_DATE))
    skip = found(day_skip_audit_id(_DATE))
    items: dict[str, RecordInput] = {}
    for ref in refs or []:
        items[ref] = found(items_audit_id(_DATE, ref))  # type: ignore[arg-type]
    return build_screen(DayInput(_DATE.isoformat(), index, skip, items))


# =================================================================================================
# 1・5 kv1 と索引(通常の日)
# =================================================================================================


def test_a_normal_day_writes_items_and_frame_per_owner_and_the_index_last() -> None:
    audit = _Audit()

    recorded, compute, provider = _run(audit, _owners(3))

    assert recorded is True
    assert compute.owners == _owners(3)
    kinds = [kind for kind, _ in audit.order]
    assert kinds.count("allocation_shadow_items") == 3
    assert kinds.count("allocation_shadow_skip") + kinds.count("allocation_shadow") == 3
    assert kinds[-1] == "allocation_shadow_index"  # ★ 索引は全 owner の記録の後 ★
    assert kinds.count("allocation_shadow_index") == 1
    assert "allocation_shadow_day_skip" not in kinds
    assert provider.calls == 1  # ★ 鍵は run 内で 1 回 ★


def test_the_index_lists_exactly_the_owners_that_have_an_items_record() -> None:
    audit = _Audit()

    _run(audit, _owners(3))

    [index] = [r["output_values"] for r in audit.of("allocation_shadow_index")]
    validate_index_record(index)
    stored_items = {
        r["output_values"]["owner_ref"]: r["output_values"]
        for r in audit.of("allocation_shadow_items")
    }
    assert index["owner_refs"] == sorted(stored_items)
    assert index["recorded_owner_count"] == 3
    assert index["unrecorded_owner_count"] == 0
    assert index["attempted_owner_count"] == 3
    assert index["key_version"] == 1
    for record in stored_items.values():
        validate_items_record(record)


def test_every_new_record_id_and_reference_is_kv1_and_the_plain_owner_never_appears(
    caplog: pytest.LogCaptureFixture,
) -> None:
    audit = _Audit()
    owners = [_SENTINEL, *_owners(2)]

    with caplog.at_level(logging.DEBUG):
        _run(audit, owners)

    refs = {_ref(owner) for owner in owners}
    for audit_id, entry in audit.records.items():
        assert _SENTINEL not in audit_id
        assert _SENTINEL not in json.dumps(entry, default=str)
        if entry["decision_type"] in (
            "allocation_shadow_items",
            "allocation_shadow",
            "allocation_shadow_skip",
        ):
            [ref] = [r for r in refs if r in audit_id]
            assert parse_keyed_ref(ref)[0] == 1  # kv1
            assert ":kv0:" not in audit_id and "kv0:" not in audit_id
    assert _SENTINEL not in caplog.text


def test_the_frame_record_id_is_keyed_by_the_batch_and_the_kv1_reference() -> None:
    audit = _Audit()

    _run(audit, ["owner-00"])

    ref = _ref("owner-00")
    skip_ids = [i for i in audit.records if i.startswith("allocation_shadow_skip:")]
    assert skip_ids == [f"allocation_shadow_skip:{_BATCH}:{ref}"]
    [items_id] = [i for i in audit.records if i.startswith("allocation_shadow_items:")]
    assert items_id == f"allocation_shadow_items:{_DATE.isoformat()}:{ref}"


def test_the_business_date_is_the_jst_day_even_when_utc_is_still_the_previous_day() -> None:
    audit = _Audit()
    just_after_jst_midnight = dt.datetime(2026, 10, 13, 16, 0, tzinfo=dt.UTC)  # JST 10-14 01:00

    _run(audit, ["owner-00"], now=just_after_jst_midnight)

    assert index_audit_id(_DATE) in audit.records
    assert all("2026-10-13" not in audit_id for audit_id in audit.records)


def test_the_stored_records_read_back_as_a_complete_day_in_the_cli() -> None:
    audit = _Audit()
    _run(audit, _owners(2))
    [index] = [r["output_values"] for r in audit.of("allocation_shadow_index")]

    screen = _read_back(audit, index["owner_refs"])

    assert screen.state is DayState.COMPLETE
    assert screen.complete is True
    assert screen.unparsed == 0
    assert len(screen.owners) == 2
    assert _SENTINEL not in render_table(screen) + render_json(screen)


# =================================================================================================
# 2 鍵が取れない日(fail-closed)
# =================================================================================================


@pytest.mark.parametrize("reason", list(KeyUnavailableReason))
def test_an_unavailable_key_writes_no_owner_record_and_one_day_skip(
    reason: KeyUnavailableReason,
) -> None:
    audit = _Audit()
    compute = _Compute()

    recorded, _, provider = _run(
        audit, _owners(3), provider=_Provider(KeyUnavailable(reason)), compute=compute
    )

    assert recorded is True  # 日単位の skip を書いた
    assert compute.owners == []  # 鍵が無ければ計算にも進まない
    assert [kind for kind, _ in audit.order] == ["allocation_shadow_day_skip"]
    [skip] = [r["output_values"] for r in audit.of("allocation_shadow_day_skip")]
    validate_day_skip_record(skip)
    assert skip["reason"] == "KEY_UNAVAILABLE"
    assert skip["key_unavailable_reason"] == reason.value  # ★ 6 通りとも記録される ★
    assert skip["unrecorded_owner_count"] == 3
    assert provider.calls == 1  # 最初の失敗で打ち切り(owner ごとに再試行しない)
    assert day_skip_audit_id(_DATE) in audit.records  # 業務日だけが key


@pytest.mark.parametrize("reason", list(KeyUnavailableReason))
def test_the_cli_shows_every_recorded_key_failure_reason(reason: KeyUnavailableReason) -> None:
    audit = _Audit()
    _run(audit, _owners(2), provider=_Provider(KeyUnavailable(reason)))

    screen = _read_back(audit)

    assert screen.state is DayState.NO_INDEX_DAY_SKIP  # ★ CLI が 6 通りとも表示できる ★
    assert screen.complete is False
    assert screen.unparsed == 0
    assert screen.skip is not None and screen.skip.key_unavailable_reason is not None
    assert screen.skip.key_unavailable_reason.code == reason.value
    assert reason.value in render_table(screen)


def test_a_key_failure_never_falls_back_to_a_fixed_or_unkeyed_reference() -> None:
    audit = _Audit()

    _run(
        audit,
        [_SENTINEL, "owner-00"],
        provider=_Provider(KeyUnavailable(KeyUnavailableReason.ERROR)),
    )

    text = json.dumps(list(audit.records.items()), default=str)
    assert "kv0" not in text
    assert "kv1" not in text  # 符号を 1 つも作らない
    assert _SENTINEL not in text
    assert all(kind == "allocation_shadow_day_skip" for kind, _ in audit.order)


@pytest.mark.parametrize(
    "result",
    [RuntimeError("boom"), "not-a-key", None, 7],
    ids=["raises", "string", "none", "int"],
)
def test_a_provider_that_breaks_its_contract_is_a_key_failure_not_an_exception(result: Any) -> None:
    audit = _Audit()

    recorded, _, _ = _run(audit, _owners(2), provider=_Provider(result))

    assert recorded is True
    [skip] = [r["output_values"] for r in audit.of("allocation_shadow_day_skip")]
    assert skip["key_unavailable_reason"] == "ERROR"


def test_a_key_that_does_not_arrive_within_the_io_limit_is_a_timeout() -> None:
    audit = _Audit()

    class _Slow:
        def get(self) -> Any:
            time.sleep(0.5)
            return _KEY

    recorded, compute, _ = _run(audit, _owners(2), provider=_Slow(), io_timeout=0.05)

    assert recorded is True
    assert compute.owners == []
    [skip] = [r["output_values"] for r in audit.of("allocation_shadow_day_skip")]
    assert skip["key_unavailable_reason"] == "TIMEOUT"


def test_a_key_that_arrives_after_the_deadline_is_not_kept() -> None:
    clock = _Clock()
    inner = _Provider(_KEY)
    bound = DeadlineBoundKeyProvider(inner, Deadline(30.0, clock), 1.0)
    clock.t += 31.0  # 総予算が尽きた

    result = bound.get()

    assert isinstance(result, KeyUnavailable) and result.reason is KeyUnavailableReason.TIMEOUT
    assert inner.calls == 0  # 期限後は内側を呼ばない・何も保持しない


def test_only_a_success_within_the_deadline_is_kept_and_a_failure_is_not() -> None:
    inner = _Provider(KeyUnavailable(KeyUnavailableReason.ACCESS_DENIED))
    bound = DeadlineBoundKeyProvider(inner, Deadline(30.0, _Clock()), 1.0)

    assert isinstance(bound.get(), KeyUnavailable)
    assert isinstance(bound.get(), KeyUnavailable)
    assert inner.calls == 2  # 失敗は保持しない(呼び出し側が打ち切る)

    inner.result = _KEY
    assert bound.get() is _KEY
    assert bound.get() is _KEY
    assert inner.calls == 3  # 成功は run 内で 1 回だけ


def test_the_bound_provider_does_not_show_anything_secret() -> None:
    bound = DeadlineBoundKeyProvider(_Provider(_KEY), Deadline(30.0, _Clock()), 1.0)
    bound.get()

    assert "t" * 32 not in repr(bound) + str(bound)
    assert repr(bound) == "DeadlineBoundKeyProvider()"


# =================================================================================================
# 3 バッチ共通の入力が作れない日・残り時間が足りない日
# =================================================================================================

_INPUT_FAILURES = [
    ShadowSkipReason.HOLDINGS_READ_FAILED,
    ShadowSkipReason.RECOMMENDATIONS_READ_FAILED,
    ShadowSkipReason.INPUT_LIMIT_EXCEEDED,
    ShadowSkipReason.COEFFICIENTS_UNAVAILABLE,
    ShadowSkipReason.TIME_LIMIT,
    ShadowSkipReason.COMPUTATION_FAILED,
]


@pytest.mark.parametrize("reason", _INPUT_FAILURES)
def test_an_input_failure_day_writes_one_day_skip_with_the_reason_and_nothing_else(
    reason: ShadowSkipReason, caplog: pytest.LogCaptureFixture
) -> None:
    audit = _Audit()
    provider = _Provider()
    compute = _Compute()

    with caplog.at_level(logging.WARNING):
        recorded, _, _ = _run(
            audit,
            _owners(2),
            provider=provider,
            compute=compute,
            prepare=lambda deadline, io: BatchUnavailable(reason, {}),
        )

    assert recorded is True
    assert [kind for kind, _ in audit.order] == ["allocation_shadow_day_skip"]
    [skip] = [r["output_values"] for r in audit.of("allocation_shadow_day_skip")]
    validate_day_skip_record(skip)
    assert skip["reason"] == reason.value
    assert skip["key_unavailable_reason"] is None  # 鍵の話ではない
    assert skip["unrecorded_owner_count"] is None  # owner が分からない
    assert provider.calls == 0 and compute.owners == []
    assert f"{DAY_SKIP_MARKER} reason={reason.value}" in caplog.text  # 理由コードつきのログ 1 行


def test_every_reason_the_batch_can_fail_with_can_be_written_as_a_day_skip() -> None:
    assert {r.value for r in _INPUT_FAILURES} <= set(DAY_SKIP_REASONS)
    assert "TIME_BUDGET" in DAY_SKIP_REASONS


@pytest.mark.parametrize("reason", _INPUT_FAILURES)
def test_the_cli_shows_an_input_failure_day_as_a_recorded_failure_not_zero_records(
    reason: ShadowSkipReason,
) -> None:
    audit = _Audit()
    _run(audit, _owners(2), prepare=lambda deadline, io: BatchUnavailable(reason, {}))

    screen = _read_back(audit)

    assert screen.state is DayState.NO_INDEX_DAY_SKIP
    assert screen.complete is False
    assert screen.skip is not None and screen.skip.reason is not None
    assert screen.skip.reason.code == reason.value
    assert screen.skip.reason.recognized is True
    assert "0 件" not in render_table(screen)


def test_an_insufficient_remaining_time_writes_a_time_budget_day_skip_before_any_read() -> None:
    audit = _Audit()
    provider = _Provider()

    def _forbidden(deadline: Deadline, io: float) -> Any:
        raise AssertionError("the batch inputs must not be read")

    recorded, compute, _ = _run(
        audit,
        _owners(2),
        provider=provider,
        prepare=_forbidden,
        precheck=lambda: ShadowSkipReason.TIME_BUDGET,
    )

    assert recorded is True
    [skip] = [r["output_values"] for r in audit.of("allocation_shadow_day_skip")]
    assert skip["reason"] == "TIME_BUDGET"
    assert provider.calls == 0 and compute.owners == []
    assert len(audit.records) == 1


def test_an_index_with_zero_owners_is_never_written_as_a_substitute_for_a_failure() -> None:
    """★『owner 0 件の索引』は『動いたが 0 件』と誤表示されるため、失敗の代わりに書かない。★"""
    for kwargs in (
        {"provider": _Provider(KeyUnavailable(KeyUnavailableReason.ERROR))},
        {"prepare": lambda deadline, io: BatchUnavailable(ShadowSkipReason.TIME_LIMIT, {})},
        {"precheck": lambda: ShadowSkipReason.TIME_BUDGET},
    ):
        audit = _Audit()
        _run(audit, _owners(2), **kwargs)
        assert audit.of("allocation_shadow_index") == []
        assert len(audit.of("allocation_shadow_day_skip")) == 1


def test_a_day_with_no_owner_at_all_writes_nothing() -> None:
    audit = _Audit()

    recorded, compute, provider = _run(audit, [])

    assert recorded is False
    assert audit.records == {}
    assert provider.calls == 0 and compute.owners == []


# =================================================================================================
# 6 OWNER_CAP
# =================================================================================================


def test_more_than_sixteen_owners_are_not_recorded_or_computed_and_are_counted_as_the_cap() -> None:
    audit = _Audit()

    _, compute, _ = _run(audit, _owners(18))

    assert len(compute.owners) == 16  # 17 人目以降は計算にも進まない
    [index] = [r["output_values"] for r in audit.of("allocation_shadow_index")]
    assert index["recorded_owner_count"] == 16
    assert index["unrecorded_reasons"] == {"OWNER_CAP": 2}
    assert index["owner_refs_truncated"] is True
    assert index["attempted_owner_count"] == 18
    assert len(audit.of("allocation_shadow_items")) == 16


def test_exactly_sixteen_owners_are_all_recorded_and_not_truncated() -> None:
    audit = _Audit()

    _run(audit, _owners(16))

    [index] = [r["output_values"] for r in audit.of("allocation_shadow_index")]
    assert index["recorded_owner_count"] == 16
    assert index["owner_refs_truncated"] is False
    assert index["unrecorded_reasons"] == {}


def test_a_key_failure_with_more_than_sixteen_owners_does_not_count_the_cap_twice() -> None:
    audit = _Audit()

    _run(audit, _owners(17), provider=_Provider(KeyUnavailable(KeyUnavailableReason.NOT_FOUND)))

    [skip] = [r["output_values"] for r in audit.of("allocation_shadow_day_skip")]
    assert skip["unrecorded_owner_count"] == 16  # OWNER_CAP の内側だけ(17 でも 16 + 1 でもない)


# =================================================================================================
# 5・7 書込の失敗・例外・予算
# =================================================================================================


def test_an_owner_whose_items_record_cannot_be_written_is_counted_not_listed() -> None:
    bad = _ref("owner-01")

    def fail(decision_type: str, audit_id: str) -> BaseException | None:
        if decision_type == "allocation_shadow_items" and audit_id.endswith(bad):
            return RuntimeError("write failed")
        return None

    audit = _Audit(fail)

    recorded, _, _ = _run(audit, _owners(3))

    assert recorded is True
    [index] = [r["output_values"] for r in audit.of("allocation_shadow_index")]
    assert bad not in index["owner_refs"]  # ★ 実際に記録が存在する owner だけ ★
    assert index["recorded_owner_count"] == 2
    assert index["unrecorded_reasons"] == {"WRITE_FAILED": 1}
    assert index["attempted_owner_count"] == 3


def test_the_cli_reports_an_unrecorded_owner_instead_of_a_complete_day() -> None:
    bad = _ref("owner-01")
    audit = _Audit(
        lambda kind, audit_id: (
            RuntimeError("x")
            if kind == "allocation_shadow_items" and audit_id.endswith(bad)
            else None
        )
    )
    _run(audit, _owners(3))
    [index] = [r["output_values"] for r in audit.of("allocation_shadow_index")]

    screen = _read_back(audit, index["owner_refs"])

    assert screen.state is DayState.PARTIAL
    assert screen.complete is False


def test_an_index_that_cannot_be_written_never_raises_and_is_logged_without_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    audit = _Audit(
        lambda kind, _id: RuntimeError(_SENTINEL) if kind == "allocation_shadow_index" else None
    )

    with caplog.at_level(logging.WARNING):
        recorded, _, _ = _run(audit, _owners(2))

    assert recorded is True  # 項目つきの記録は書けた
    assert audit.of("allocation_shadow_index") == []
    assert INDEX_NOT_WRITTEN_MARKER in caplog.text
    assert _SENTINEL not in caplog.text  # 例外の文面を出さない(型名だけ)


def test_when_every_write_fails_the_run_returns_false_and_never_raises() -> None:
    audit = _Audit(lambda kind, _id: RuntimeError("down"))

    recorded, _, _ = _run(audit, _owners(3))

    assert recorded is False
    assert audit.records == {}


@pytest.mark.parametrize("error", [RuntimeError("x"), ValueError("y")])
def test_a_compute_that_raises_is_isolated_and_counted_as_unrecorded(error: Exception) -> None:
    def boom(owner: str) -> Any:
        if owner == "owner-01":
            raise error

    audit = _Audit()

    recorded, _, _ = _run(audit, _owners(3), compute=_Compute(on_call=boom))

    assert recorded is True
    [index] = [r["output_values"] for r in audit.of("allocation_shadow_index")]
    assert index["recorded_owner_count"] == 2
    assert index["unrecorded_reasons"] == {"WRITE_FAILED": 1}


def test_an_owner_without_an_items_record_gets_only_the_frame_record_and_is_not_listed() -> None:
    audit = _Audit()

    def no_record(owner: str) -> Any:
        if owner == "owner-01":
            return _computed(_ref(owner), with_record=False)

    _run(audit, _owners(3), compute=_Compute(on_call=no_record))

    [index] = [r["output_values"] for r in audit.of("allocation_shadow_index")]
    assert index["recorded_owner_count"] == 2
    assert index["unrecorded_reasons"] == {"WRITE_FAILED": 1}
    assert len(audit.of("allocation_shadow_items")) == 2
    assert f"allocation_shadow_skip:{_BATCH}:{_ref('owner-01')}" in audit.records  # 枠は残る


def test_an_invalid_owner_is_refused_and_does_not_stop_the_others() -> None:
    audit = _Audit()

    _run(
        audit,
        ["", "owner-01"],
        prepare=lambda deadline, io: SimpleNamespace(owners=("", "owner-01")),
    )

    [index] = [r["output_values"] for r in audit.of("allocation_shadow_index")]
    assert index["recorded_owner_count"] == 1
    assert index["unrecorded_reasons"] == {"REFUSED": 1}


def test_an_exhausted_budget_allows_one_over_budget_owner_then_counts_the_rest() -> None:
    clock = _Clock()

    def spend(owner: str) -> Any:
        clock.t += 100.0  # 計算の間に総予算(30 秒)が尽きる

    audit = _Audit()

    recorded, compute, _ = _run(audit, _owners(4), compute=_Compute(on_call=spend), clock=clock)

    assert recorded is True
    assert compute.owners == ["owner-00"]  # 2 人目以降は計算にも進まない
    [index] = [r["output_values"] for r in audit.of("allocation_shadow_index")]
    assert index["recorded_owner_count"] == 1  # 予算超過でも 1 owner だけ記録する
    assert index["unrecorded_reasons"] == {"BUDGET_EXHAUSTED": 3}
    assert index["attempted_owner_count"] == 4
    # 予算が尽きた後の 1 owner は項目つきの記録だけ(枠の記録は最善努力で、予算が無ければ書かない)
    assert len(audit.of("allocation_shadow_items")) == 1
    assert not [i for i in audit.records if i.startswith("allocation_shadow_skip:")]


def test_a_precheck_that_starts_failing_mid_run_counts_the_remaining_owners() -> None:
    answers = iter([None, "stop", "stop", "stop"])

    def precheck() -> ShadowSkipReason | None:
        value = next(answers)
        return None if value is None else ShadowSkipReason.TIME_BUDGET

    audit = _Audit()

    _, compute, _ = _run(audit, _owners(3), precheck=precheck)

    assert compute.owners == []  # 入口の判定は通るが、最初の owner の前の判定で止まる
    # 1 回目 = 入口(通る)/ 2 回目 = 最初の owner の前(止まる)
    [index] = [r["output_values"] for r in audit.of("allocation_shadow_index")]
    assert index["recorded_owner_count"] == 0
    assert index["unrecorded_reasons"] == {"BUDGET_EXHAUSTED": 3}


def test_an_owner_whose_budget_ran_out_between_the_check_and_the_write_is_counted_not_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """予算が計算の後・書込の直前に尽きた(= 書かなかった)owner は索引へ載せず BUDGET_EXHAUSTED。"""
    original = runner_module._Loop._record_owner

    def starve(self: Any, ref: KeyedOwnerRef, computed: Any, *, allow_over_budget: bool) -> Any:
        if str(ref) == str(_ref("owner-01")):
            return False  # 書込の直前に予算が尽きた
        return original(self, ref, computed, allow_over_budget=allow_over_budget)

    monkeypatch.setattr(runner_module._Loop, "_record_owner", starve)
    audit = _Audit()

    _run(audit, _owners(3))

    [index] = [r["output_values"] for r in audit.of("allocation_shadow_index")]
    assert _ref("owner-01") not in index["owner_refs"]  # ★ 書いていない owner は載せない ★
    assert index["recorded_owner_count"] == 2
    assert index["unrecorded_reasons"] == {"BUDGET_EXHAUSTED": 1}


# =================================================================================================
# 8 再実行(先着・条件付き)
# =================================================================================================


def test_a_second_run_changes_nothing_and_reports_that_nothing_was_written() -> None:
    audit = _Audit()
    first, _, _ = _run(audit, _owners(2))
    before = {k: json.dumps(v, default=str) for k, v in audit.records.items()}

    second, _, _ = _run(audit, _owners(2))

    assert first is True and second is False  # 2 回目は新しい記録が無い
    assert {k: json.dumps(v, default=str) for k, v in audit.records.items()} == before


def test_concurrent_runs_write_each_record_exactly_once() -> None:
    audit = _Audit()
    barrier = threading.Barrier(4)
    results: list[bool] = []

    def worker() -> None:
        barrier.wait()
        recorded, _, _ = _run(audit, _owners(2))
        results.append(recorded)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert len(results) == 4
    assert len(audit.of("allocation_shadow_items")) == 2
    assert len(audit.of("allocation_shadow_index")) == 1


def test_the_day_skip_key_is_the_day_only_so_a_second_batch_of_the_same_day_is_first_wins() -> None:
    audit = _Audit()
    reason = ShadowSkipReason.HOLDINGS_READ_FAILED
    _run(audit, _owners(1), prepare=lambda deadline, io: BatchUnavailable(reason, {}))

    again = ShadowRunContext(
        batch_id="buy-candidates-other-batch",
        now=_NOW,
        audit_service=audit,
        deadline=Deadline(30.0, _Clock()),
        io_timeout_seconds=1.0,
        shadow_mode="SHADOW",
        precheck=lambda: None,
    )
    make_runner(
        prepare=lambda deadline, io: BatchUnavailable(ShadowSkipReason.TIME_LIMIT, {}),
        compute_owner=_Compute(),
        key_provider=_Provider(),
    )(again)

    [skip] = [r["output_values"] for r in audit.of("allocation_shadow_day_skip")]
    assert skip["batch_id"] == _BATCH  # 先着が残る(batch_id は本文)
    assert skip["reason"] == "HOLDINGS_READ_FAILED"


# =================================================================================================
# 静的な契約(AST)
# =================================================================================================


def _tree() -> ast.Module:
    return ast.parse(_RUNNER_PATH.read_text(encoding="utf-8"))


def test_the_runner_has_no_kv0_reference_and_no_default_owner() -> None:
    names = {node.id for node in ast.walk(_tree()) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(_tree()) if isinstance(node, ast.Attribute)}
    imported = {
        alias.name
        for node in ast.walk(_tree())
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    for forbidden in ("owner_ref", "require_persistable", "DEFAULT_OWNER", "log_ref"):
        assert forbidden not in names | attributes | imported, forbidden


def test_the_runner_writes_only_through_the_conditional_append() -> None:
    called = {
        node.func.attr
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "record_if_absent" in called
    assert not called & {"record", "save", "put_item", "update_item", "delete_item"}
    assert not called & {"scan", "query", "get_item", "batch_get_item", "list_all"}


def test_the_runner_reads_no_wall_clock_and_no_environment() -> None:
    called = {
        node.func.attr
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    attributes = {node.attr for node in ast.walk(_tree()) if isinstance(node, ast.Attribute)}

    # 時刻は引数(ctx.now)だけ。時計・環境変数を読まない(`now` は引数名で、読取の呼び出しではない)
    assert not called & {"now", "utcnow", "today", "time", "time_ns", "sleep", "getenv"}
    assert "environ" not in attributes


def test_the_index_is_built_only_from_the_recorded_references() -> None:
    """索引の builder へ渡す recorded は、記録が存在した owner の符号だけを積んだ list(AST)。"""
    source = _RUNNER_PATH.read_text(encoding="utf-8")

    assert source.count("recorded.append(") == 1
    assert "recorded=recorded" in source
    assert runner_module.INDEX_DECISION_TYPE == "allocation_shadow_index"
