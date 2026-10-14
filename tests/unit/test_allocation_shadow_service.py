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
from jstock_advisor.domain.signals.allocation_shadow_config import (
    AllocationShadowConfig,
    AllocationShadowMode,
)
from jstock_advisor.services import allocation_shadow_service as svc
from jstock_advisor.services.allocation_shadow_service import (
    CashReadStatus,
    Deadline,
    IoTimeoutError,
    ShadowOutcome,
    ShadowResult,
    ShadowRunContext,
    ShadowRunner,
    ShadowSkipReason,
    call_with_timeout,
    observe_allocation_shadow,
    read_available_cash,
)

_NOW = dt.datetime(2026, 10, 12, 8, 0, tzinfo=dt.UTC)
_BATCH = "buy-candidates-2026-10-12"
_OWNER = "owner-a"  # 架空の owner(買付余力の読取は owner の文字列をそのまま getter へ渡すだけ)


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
    wiring_factory: Callable[[], ShadowRunner] | None = None,
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
        wiring_factory=wiring_factory,
        monotonic=clock or _Clock(),
    )


# --- (1) OFF は最初の判定で戻る --------------------------------------------------------


def test_off_touches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "_default_cash_getter", _tripwire)

    recorded = _observe(
        _TripwireAudit(),
        config=_OFF,
        remaining_ms=_tripwire,  # 残り時間も読まない
        wiring_factory=_tripwire,  # 配線(repository・鍵の provider)も組み立てない
        clock=_tripwire,  # 時計も読まない
    )

    assert recorded is False


def test_default_entry_loads_the_config_and_an_off_config_touches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shadow_config を渡さない本番の呼び方は、config を読み込む入口を通る。出荷値が SHADOW に
    なった(R1)ため、読み込みを OFF の設定へ差し替え(= 出荷値に依存しない)、OFF なら何もせず
    戻ることを固定する。★ 出荷 config を読んだまま tripwire を置くと、tripwire の例外が入口の
    隔離 try/except に飲まれて False が返り『空振りで PASS』になる(サブちゃん #956 の MUST)。
    そのため読み込みの呼び出しも数える ★"""
    monkeypatch.setattr(svc, "_default_cash_getter", _tripwire)
    loads: list[int] = []

    def _load_off() -> AllocationShadowConfig:
        loads.append(1)
        return _OFF

    monkeypatch.setattr(svc, "load_allocation_shadow_config", _load_off)

    recorded = observe_allocation_shadow(
        batch_id=_BATCH,
        now=_NOW,
        execution_context=_NORMAL,
        audit_service=_TripwireAudit(),  # type: ignore[arg-type]
        remaining_time_ms=_tripwire,
        wiring_factory=_tripwire,
        monotonic=_tripwire,
    )

    assert recorded is False
    assert loads == [1]  # 出荷値に依らず、config の読み込みの入口を通った(空振りではない)


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

    recorded = _observe(
        audit, execution_context=context, wiring_factory=_tripwire, remaining_ms=_tripwire
    )

    assert recorded is False
    assert audit.calls == 0


def test_missing_batch_id_writes_nothing() -> None:
    audit = _FakeAudit()

    assert _observe(audit, batch_id=None, wiring_factory=_tripwire) is False
    assert audit.calls == 0


# --- 既定の差し込み先 --------------------------------------------------------------------


# --- (3) 残り時間の境界 ------------------------------------------------------------------


# --- (2) 例外を送出しない -----------------------------------------------------------------


# --- (4) 協調的な期限(30 秒)と I/O の打ち切り -------------------------------------------------


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


# --- AvailableCash の読取(期限つき・状態の分類)--------------------------------------------


def _cash(amount: str) -> AvailableCash:
    return AvailableCash(
        owner=_OWNER,
        available_cash=Decimal(amount),
        updated_at=_NOW,
        last_update_type=AvailableCashUpdateType.USER_RECONCILIATION,
    )


def test_cash_read_available_and_zero_is_registered() -> None:
    result = read_available_cash(_OWNER, timeout_seconds=1.0, get=lambda o: _cash("0"))

    assert result.status is CashReadStatus.AVAILABLE
    assert result.amount == Decimal("0")  # 登録された 0 円は未登録ではない
    assert result.as_skip_reason() is None


def test_cash_read_carries_the_reconciliation_and_update_times_for_the_record() -> None:
    """F2-2a: 項目つきの記録の買付余力(最終棚卸し・更新時刻)を、同じ 1 回の読取から得る。"""
    reconciled = _NOW - dt.timedelta(days=3)

    def get(owner: str) -> AvailableCash:
        record = _cash("5000")
        return record.model_copy(update={"last_reconciled_at": reconciled})

    result = read_available_cash(_OWNER, timeout_seconds=1.0, get=get)

    assert result.last_reconciled_at == reconciled
    assert result.updated_at == _NOW
    assert read_available_cash(_OWNER, timeout_seconds=1.0, get=lambda o: None).updated_at is None


def test_the_f22a_skip_reasons_are_codes_the_record_accepts() -> None:
    import re

    pattern = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
    for name in (
        "HOLDINGS_READ_FAILED",
        "RECOMMENDATIONS_READ_FAILED",
        "INPUT_LIMIT_EXCEEDED",
        "COEFFICIENTS_UNAVAILABLE",
    ):
        assert pattern.match(ShadowSkipReason[name].value)


def test_cash_read_none_is_not_registered_not_zero() -> None:
    result = read_available_cash(_OWNER, timeout_seconds=1.0, get=lambda o: None)

    assert result.status is CashReadStatus.NOT_REGISTERED
    assert result.amount is None
    assert result.as_skip_reason() is ShadowSkipReason.CASH_NOT_REGISTERED


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "GetItem")


@pytest.mark.parametrize("code", ["AccessDenied", "AccessDeniedException", "UnauthorizedOperation"])
def test_cash_read_access_denied_is_iam_missing(code: str) -> None:
    def get(owner: str) -> AvailableCash | None:
        raise _client_error(code)

    result = read_available_cash(_OWNER, timeout_seconds=1.0, get=get)

    assert result.status is CashReadStatus.IAM_MISSING
    assert result.as_skip_reason() is ShadowSkipReason.IAM_MISSING


def test_cash_read_other_errors_are_classified_without_raising() -> None:
    def throttled(owner: str) -> AvailableCash | None:
        raise _client_error("ProvisionedThroughputExceededException")

    def broken(owner: str) -> AvailableCash | None:
        raise ValueError("x")

    for getter in (throttled, broken):
        result = read_available_cash(_OWNER, timeout_seconds=1.0, get=getter)
        assert result.status is CashReadStatus.ERROR
        assert result.as_skip_reason() is ShadowSkipReason.COMPUTATION_FAILED


def test_cash_read_timeout_is_classified() -> None:
    release = threading.Event()
    try:
        result = read_available_cash(
            _OWNER, timeout_seconds=0.05, get=lambda o: release.wait(10) and None
        )
    finally:
        release.set()

    assert result.status is CashReadStatus.TIMEOUT
    assert result.as_skip_reason() is ShadowSkipReason.TIME_LIMIT


# --- (6) 記録する値 ------------------------------------------------------------------------


def test_shadow_result_requires_a_reason_unless_computed() -> None:
    with pytest.raises(ValueError, match="理由"):
        ShadowResult(ShadowOutcome.SKIPPED)
    with pytest.raises(ValueError, match="理由"):
        ShadowResult(ShadowOutcome.FAILED)
    with pytest.raises(ValueError, match="理由"):
        ShadowResult(ShadowOutcome.COMPUTED, ShadowSkipReason.TIME_LIMIT)
    ShadowResult(ShadowOutcome.COMPUTED)  # 例外にならない


def test_shadow_result_facts_must_be_short_scalars() -> None:
    ShadowResult(ShadowOutcome.COMPUTED, facts={"n": 3, "ok": True, "ratio": 0.5, "code": "A"})
    with pytest.raises(ValueError, match="scalar"):
        ShadowResult(ShadowOutcome.COMPUTED, facts={"x": [1]})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="scalar"):
        ShadowResult(ShadowOutcome.COMPUTED, facts={"x": Decimal("1")})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="長すぎる"):
        ShadowResult(ShadowOutcome.COMPUTED, facts={"x": "a" * 65})


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

    # R1(#128)で出荷 config は SHADOW。OFF の設定を明示して渡す(出荷 config を読むと、tripwire の
    # 例外が入口の隔離 try/except に飲まれて False が返り『空振りで PASS』になる = #956 の MUST)
    recorded = observe_allocation_shadow(
        batch_id=_BATCH,
        now=_NOW,
        execution_context=_NORMAL,
        audit_service=_TripwireAudit(),  # type: ignore[arg-type]
        remaining_time_ms=_tripwire,
        shadow_config=_OFF,
        wiring_factory=_tripwire,
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
    # 入口の module は書込を持たない(記録は `allocation_shadow_runner` の条件付き追記だけ。そちらの
    # 契約テストが固定する)。無条件の書込・読取・走査の API を呼ばない
    assert "record_if_absent" not in attributes
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


# --- kv0 の境界: 実在の owner を kv0(鍵なし)で永続化しない(USER 決定 #925 …6142405238 の 4)---


# --- ON のときの入口: 配線の factory とループへの文脈 ---------------------------------------


class _Capture:
    """『観測のループ』の fake。渡された文脈を控えて、決めた値を返す。"""

    def __init__(self, result: bool = True) -> None:
        self.contexts: list[ShadowRunContext] = []
        self.factory_calls = 0
        self.result = result

    def factory(self) -> ShadowRunner:
        self.factory_calls += 1

        def run(ctx: ShadowRunContext) -> bool:
            self.contexts.append(ctx)
            return self.result

        return run


def test_on_calls_the_factory_once_and_hands_the_run_a_context() -> None:
    capture = _Capture(result=True)
    audit = _FakeAudit()

    recorded = _observe(audit, wiring_factory=capture.factory)

    assert recorded is True  # ループの戻り値がそのまま返る
    assert capture.factory_calls == 1
    [ctx] = capture.contexts
    assert ctx.batch_id == _BATCH
    assert ctx.now == _NOW
    assert ctx.audit_service is audit
    assert ctx.shadow_mode == "SHADOW"
    assert ctx.io_timeout_seconds == _SHADOW.io_timeout_seconds
    assert ctx.deadline.remaining() == _SHADOW.time_limit_seconds  # 区間全体で 1 つの総予算


def test_on_without_a_wiring_does_nothing_and_does_not_raise() -> None:
    audit = _FakeAudit()

    assert _observe(audit, wiring_factory=None) is False
    assert audit.calls == 0


@pytest.mark.parametrize("where", ["factory", "run"])
def test_a_failure_in_the_factory_or_the_run_never_reaches_the_caller(where: str) -> None:
    def factory() -> ShadowRunner:
        if where == "factory":
            raise RuntimeError("wiring exploded")

        def run(ctx: ShadowRunContext) -> bool:
            raise RuntimeError("loop exploded")

        return run

    assert _observe(_FakeAudit(), wiring_factory=factory) is False


def test_the_precheck_in_the_context_is_read_every_time_not_once() -> None:
    """残り時間は、呼ばれるたびに読み直す(使い回さない。Q-F)。"""
    readings = iter([300_000, 119_900, 120_000])
    seen: list[ShadowSkipReason | None] = []

    def factory() -> ShadowRunner:
        def run(ctx: ShadowRunContext) -> bool:
            seen.extend(ctx.precheck() for _ in range(3))
            return False

        return run

    _observe(_FakeAudit(), wiring_factory=factory, remaining_ms=lambda: next(readings))

    assert seen == [None, ShadowSkipReason.TIME_BUDGET, None]  # 119.9 秒は不足・120.0 秒は足りる


def test_the_precheck_fails_closed_when_the_remaining_time_is_unknown_or_raises() -> None:
    def broken() -> int:
        raise RuntimeError("no context")

    for remaining in (None, broken):
        capture: list[ShadowSkipReason | None] = []

        def factory(sink: list[ShadowSkipReason | None] = capture) -> ShadowRunner:
            def run(ctx: ShadowRunContext) -> bool:
                sink.append(ctx.precheck())
                return False

            return run

        _observe(_FakeAudit(), wiring_factory=factory, remaining_ms=remaining)
        assert capture == [ShadowSkipReason.TIME_BUDGET]


def test_the_precheck_follows_the_configured_minimum() -> None:
    config = AllocationShadowConfig(mode=AllocationShadowMode.SHADOW, min_remaining_seconds=200.0)
    seen: list[ShadowSkipReason | None] = []

    def factory() -> ShadowRunner:
        def run(ctx: ShadowRunContext) -> bool:
            seen.append(ctx.precheck())
            return False

        return run

    _observe(_FakeAudit(), config=config, wiring_factory=factory, remaining_ms=_ms(199))

    assert seen == [ShadowSkipReason.TIME_BUDGET]


def test_the_module_no_longer_has_any_kv0_reference_or_default_owner() -> None:
    """新規の記録は kv1 だけ(#925)。入口の module は owner の符号を作らない・_OWNER を持たない。"""
    text = _SERVICE_PATH.read_text(encoding="utf-8")

    for forbidden in ("require_persistable", "DEFAULT_OWNER", "ShadowRun(", "owners="):
        assert forbidden not in text, forbidden
