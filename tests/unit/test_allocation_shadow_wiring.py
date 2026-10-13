"""購入側 Shadow(Q')の handler への配線の契約テスト(Issue #603)。

#128 …6100461428 の必須条件 7 のうち、**既存の BUY 判定・LINE 通知・完了処理を変えない**ことを、
handler の側から固定する。

  W1  呼び出しは finalize の完了記録(`mark_completion_finalize_completed`)の後で、実行権を取得した
      1 起動だけが呼ぶ(token なし・finalize 失敗・batch_id なしでは呼ばない)
  W2  既定(出荷 config = OFF)では、戻り値・保存・LINE 通知・監査記録が Shadow 無しと完全に同一
      (Shadow の入口を差し替えた場合とも同一)
  W3  Shadow が例外を出しても、戻り値・通知・完了処理は変わらない
  W4  SHADOW でも、本流の結果は OFF と同一で、差は Shadow の監査記録 1 件だけ。残り時間が 120 秒
      未満・非通常実行では計算せず、VALIDATION は何も書かない
  W5  Worker 経路(SQS)と handler の task 経路の両方が、Lambda の残り時間を渡す
  W6  AST: 呼び出しは `_process_single_candidate` の finalize 実行権の分岐の中で、完了記録の後に
      ある。finalize のみの再実行(recovery)の経路には置かない
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import (
    BuyAction,
    CandidateSource,
    ExecutionMode,
    NotificationMode,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.signals.allocation_shadow_config import (
    AllocationShadowConfig,
    AllocationShadowMode,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers import buy_candidate_worker_handler as worker_module
from jstock_advisor.lambda_handlers import buy_candidates_handler as handler_module
from jstock_advisor.services import allocation_shadow_service as svc
from jstock_advisor.services.allocation_shadow_service import DECISION_TYPE
from tests.unit.test_buy_candidates_handler import (
    _CONFIG,
    _NOW,
    _FakeContext,
    _FakeNotificationServiceForRanking,
    _issue31_completed_progress,
    _make_recommendation,
    _outcome,
    _patch_common,
    _patch_snapshot,
    _RecordingAuditService,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HANDLER_PATH = (
    _REPO_ROOT / "src" / "jstock_advisor" / "lambda_handlers" / "buy_candidates_handler.py"
)
_STOCK = "2914"
_BATCH = "batch-603"
_SHADOW = AllocationShadowConfig(mode=AllocationShadowMode.SHADOW)
_PLENTY_OF_TIME = 300_000
# 同じ test の中で差し替えた後に本物へ戻せるよう、先に控えておく
_REAL_ENTRY = handler_module.observe_allocation_shadow
_REAL_LOADER = svc.load_allocation_shadow_config


def _tripwire(*_a: object, **_kw: object) -> Any:
    raise AssertionError("must not be called")


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    acquire_results: list[str | None] | None = None,
    finalize_raises: bool = False,
    batch_id: str | None = _BATCH,
    shadow_entry: Any = None,
    shadow_config: AllocationShadowConfig | None = None,
    remaining_ms: Any = None,
    execution_context: ExecutionContext | None = None,
    audit: _RecordingAuditService | None = None,
) -> dict[str, Any]:
    """_process_single_candidate を acquire_results の回数だけ走らせ、外から観測できる結果を返す。

    shadow_entry     handler の `observe_allocation_shadow` を差し替える(None なら本物)
    shadow_config    本物の入口が読む設定を差し替える(None なら出荷 config = OFF)
    """
    _patch_snapshot(monkeypatch)
    audit = audit or _RecordingAuditService()
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: audit)
    recommendation = _make_recommendation(
        _STOCK, company_quality_score=72.5, recommendation_id="rec-603", buy_action=BuyAction.BUY
    )
    outcome = _outcome(recommendation, ranking_group="buy_candidate")
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)
    monkeypatch.setattr(
        handler_module, "record_result", lambda *a, **kw: _issue31_completed_progress()
    )

    events: list[str] = []

    def _finalize(*_a: object, **_kw: object) -> None:
        events.append("finalize")
        if finalize_raises:
            raise RuntimeError("finalize failed (simulated)")

    monkeypatch.setattr(handler_module, "_finalize_batch", _finalize)
    tokens = iter(acquire_results if acquire_results is not None else ["tok"])
    monkeypatch.setattr(
        handler_module, "try_acquire_completion_finalize", lambda b, n: next(tokens, None)
    )
    monkeypatch.setattr(
        handler_module,
        "mark_completion_finalize_completed",
        lambda b, t, n: events.append("mark") or True,
    )
    shadow_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(handler_module, "observe_allocation_shadow", _REAL_ENTRY)
    monkeypatch.setattr(
        svc,
        "load_allocation_shadow_config",
        _REAL_LOADER if shadow_config is None else (lambda *a, **kw: shadow_config),
    )
    if shadow_entry is not None:

        def _entry(**kwargs: Any) -> bool:
            events.append("shadow")
            shadow_calls.append(kwargs)
            return shadow_entry(**kwargs)

        monkeypatch.setattr(handler_module, "observe_allocation_shadow", _entry)

    repo = RecommendationRepository(store_dir=tmp_path)
    notification = _FakeNotificationServiceForRanking()
    kwargs: dict[str, Any] = {}
    if execution_context is not None:
        kwargs["execution_context"] = execution_context
    if remaining_ms is not None:
        kwargs["remaining_time_ms"] = remaining_ms
    results: list[Any] = []
    error: BaseException | None = None
    for _ in range(len(acquire_results) if acquire_results is not None else 1):
        try:
            results.append(
                handler_module._process_single_candidate(
                    _STOCK,
                    CandidateSource.WATCHLIST,
                    None,
                    None,
                    batch_id,
                    _NOW,
                    object(),
                    _CONFIG,
                    object(),
                    repo,
                    notification,
                    **kwargs,
                )
            )
        except RuntimeError as exc:
            error = exc
    return {
        "results": results,
        "error": error,
        "events": events,
        "shadow_calls": shadow_calls,
        "audit": audit,
        "saved": sorted(r.model_dump_json() for r in repo.list_all()),
        "notification": {k: repr(v) for k, v in vars(notification).items()},
        "audit_main": [r for r in audit.records if r["decision_type"] != DECISION_TYPE],
        "audit_shadow": [r for r in audit.records if r["decision_type"] == DECISION_TYPE],
    }


def _same_main_flow(a: dict[str, Any], b: dict[str, Any]) -> None:
    assert a["results"] == b["results"]
    assert a["saved"] == b["saved"]
    assert a["notification"] == b["notification"]
    assert a["audit_main"] == b["audit_main"]
    assert [e for e in a["events"] if e != "shadow"] == [e for e in b["events"] if e != "shadow"]


# --- W1 呼び出しの位置と、実行権を持つ 1 起動だけ ------------------------------------------


def test_shadow_runs_after_the_finalize_completion_mark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = lambda: 123_000  # noqa: E731
    run = _drive(monkeypatch, tmp_path, shadow_entry=lambda **kw: False, remaining_ms=probe)

    assert run["events"] == ["finalize", "mark", "shadow"]
    [call] = run["shadow_calls"]
    assert call["batch_id"] == _BATCH
    assert call["now"] == _NOW
    assert call["remaining_time_ms"] is probe  # handler が受け取った残り時間をそのまま渡す
    assert call["audit_service"] is run["audit"]
    assert call["execution_context"] == ExecutionContext.normal()


def test_shadow_is_called_once_under_duplicate_triggers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """実行権(token)を取得した 1 起動だけが呼ぶ。2 回目の完了トリガーは呼ばない。"""
    run = _drive(
        monkeypatch, tmp_path, acquire_results=["tok", None], shadow_entry=lambda **kw: False
    )

    assert run["events"] == ["finalize", "mark", "shadow"]
    assert len(run["shadow_calls"]) == 1


def test_shadow_is_not_called_without_the_finalize_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run = _drive(monkeypatch, tmp_path, acquire_results=[None], shadow_entry=_tripwire)

    assert run["events"] == []


def test_shadow_is_not_called_when_finalize_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(handler_module, "mark_completion_finalize_failed", lambda *a, **kw: True)

    run = _drive(monkeypatch, tmp_path, finalize_raises=True, shadow_entry=_tripwire)

    assert run["events"] == ["finalize"]  # mark も shadow も無い
    assert isinstance(run["error"], RuntimeError)  # 失敗は従来どおり伝播する


def test_shadow_is_not_called_without_a_batch_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run = _drive(monkeypatch, tmp_path, batch_id=None, shadow_entry=_tripwire)

    assert run["events"] == []


# --- W2 既定(OFF)では本流が完全に同一 ---------------------------------------------------------


def test_shipped_off_is_identical_to_having_no_shadow_at_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(svc, "_default_cash_getter", _tripwire)
    baseline = _drive(  # 入口が何もしない = Shadow が存在しない世界
        monkeypatch, tmp_path / "base", shadow_entry=lambda **kw: False
    )
    shipped = _drive(monkeypatch, tmp_path / "shipped")  # 出荷 config(OFF)を読む本物の入口

    _same_main_flow(shipped, baseline)
    assert shipped["audit_shadow"] == []  # OFF は監査記録を 1 件も足さない
    assert len(shipped["saved"]) >= 1  # 比較が空集合どうしの一致になっていないこと
    assert shipped["results"][0] is not None


def test_shipped_off_does_not_read_the_clock_or_remaining_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(svc, "_default_cash_getter", _tripwire)

    run = _drive(monkeypatch, tmp_path, remaining_ms=_tripwire)  # 読んだら失敗する

    assert run["audit_shadow"] == []
    assert run["events"] == ["finalize", "mark"]


# --- W3 Shadow の失敗は本流へ伝播しない ---------------------------------------------------------


def test_shadow_entry_raising_does_not_change_the_main_flow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(**_kw: Any) -> bool:
        raise RuntimeError("shadow exploded")

    baseline = _drive(monkeypatch, tmp_path / "base", shadow_entry=lambda **kw: False)
    failing = _drive(monkeypatch, tmp_path / "fail", shadow_entry=boom)

    assert failing["error"] is None
    _same_main_flow(failing, baseline)
    assert failing["events"] == ["finalize", "mark", "shadow"]  # 完了記録の後で失敗しただけ


def test_shadow_audit_failure_does_not_change_the_main_flow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _AuditThatFailsOnShadow(_RecordingAuditService):
        def record_if_absent(self, audit_id: str, decision_type: str, **kwargs: Any) -> Any:
            if decision_type == DECISION_TYPE:
                raise RuntimeError("shadow audit down")
            return super().record_if_absent(audit_id, decision_type, **kwargs)

    baseline = _drive(monkeypatch, tmp_path / "base", shadow_entry=lambda **kw: False)
    failing = _drive(
        monkeypatch,
        tmp_path / "fail",
        shadow_config=_SHADOW,
        remaining_ms=lambda: _PLENTY_OF_TIME,
        audit=_AuditThatFailsOnShadow(),
    )

    assert failing["error"] is None
    assert failing["audit_shadow"] == []  # 記録に失敗した
    _same_main_flow(failing, baseline)


# --- W4 SHADOW でも本流は同一。差は Shadow の監査記録 1 件だけ ------------------------


def test_shadow_adds_exactly_one_audit_record_and_changes_nothing_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    baseline = _drive(monkeypatch, tmp_path / "base", shadow_entry=lambda **kw: False)
    shadow = _drive(
        monkeypatch,
        tmp_path / "shadow",
        shadow_config=_SHADOW,
        remaining_ms=lambda: _PLENTY_OF_TIME,
    )

    _same_main_flow(shadow, baseline)
    [record] = shadow["audit_shadow"]
    assert record["output_values"]["reason"] == "COMPUTE_NOT_IMPLEMENTED"
    assert record["stock_code"] is None


def test_shadow_with_less_than_the_minimum_remaining_time_only_records_the_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    baseline = _drive(monkeypatch, tmp_path / "base", shadow_entry=lambda **kw: False)
    short = _drive(
        monkeypatch, tmp_path / "short", shadow_config=_SHADOW, remaining_ms=lambda: 119_900
    )

    _same_main_flow(short, baseline)
    [record] = short["audit_shadow"]
    assert record["output_values"] == {"outcome": "SKIPPED", "reason": "TIME_BUDGET"}


def test_shadow_without_remaining_time_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """既存の呼び出し(`remaining_time_ms` なし)では、SHADOW でも計算せずスキップを記録する。"""
    run = _drive(monkeypatch, tmp_path, shadow_config=_SHADOW)

    [record] = run["audit_shadow"]
    assert record["output_values"] == {"outcome": "SKIPPED", "reason": "TIME_BUDGET"}


@pytest.mark.parametrize(
    "context",
    [
        ExecutionContext(mode=ExecutionMode.VALIDATION),
        ExecutionContext(mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.DRY_RUN),
    ],
)
def test_validation_and_dry_run_write_no_shadow_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, context: ExecutionContext
) -> None:
    run = _drive(
        monkeypatch,
        tmp_path,
        shadow_config=_SHADOW,
        remaining_ms=lambda: _PLENTY_OF_TIME,
        execution_context=context,
    )

    assert run["audit_shadow"] == []


# --- W5 Worker 経路と handler の task 経路が残り時間を渡す --------------------------


class _LambdaContext:
    function_name = "jstock-advisor-buy-candidates"

    def __init__(self, ms: int) -> None:
        self._ms = ms

    def get_remaining_time_in_millis(self) -> int:
        return self._ms


def _capture_process_single_candidate(
    monkeypatch: pytest.MonkeyPatch, module: Any
) -> list[tuple[Any, ...]]:
    captured: list[tuple[Any, ...]] = []

    def _fake(*args: Any, **_kw: Any) -> dict[str, Any]:
        captured.append(args)
        return {"stock_code": args[0], "recommended": False, "notified": False}

    monkeypatch.setattr(module, "_process_single_candidate", _fake)
    return captured


def test_worker_path_passes_the_lambda_remaining_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.test_buy_candidate_worker_handler import _patch_common as _patch_worker

    _patch_worker(monkeypatch)
    captured = _capture_process_single_candidate(monkeypatch, worker_module)
    context = _LambdaContext(250_000)
    event = {
        "Records": [
            {
                "body": json.dumps(
                    {"task": "buy_candidate", "stock_code": "2914", "source": "WATCHLIST"}
                )
            }
        ]
    }

    assert worker_module.handler(event, context) == {"processed": 1}

    [args] = captured
    remaining = args[-1]
    assert callable(remaining) and remaining() == 250_000  # 呼ぶたびに Lambda の残りを読む
    assert worker_module._remaining_time_ms_var.get() is None  # 呼び出しの外へ漏らさない


def test_worker_path_without_a_context_method_passes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.test_buy_candidate_worker_handler import _patch_common as _patch_worker

    _patch_worker(monkeypatch)
    captured = _capture_process_single_candidate(monkeypatch, worker_module)
    event = {
        "Records": [
            {
                "body": json.dumps(
                    {"task": "buy_candidate", "stock_code": "2914", "source": "WATCHLIST"}
                )
            }
        ]
    }

    worker_module.handler(event, object())

    assert captured[0][-1] is None


def test_worker_path_resets_the_remaining_time_even_when_a_record_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(body: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("record failed")

    monkeypatch.setattr(worker_module, "_process_one", _boom)
    event = {"Records": [{"body": json.dumps({"stock_code": "2914"})}]}

    with pytest.raises(RuntimeError, match="record failed"):
        worker_module.handler(event, _LambdaContext(1))

    assert worker_module._remaining_time_ms_var.get() is None


def test_task_path_passes_the_lambda_remaining_time(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)
    captured = _capture_process_single_candidate(monkeypatch, handler_module)

    handler_module.handler(
        {"task": "buy_candidate", "stock_code": "2914", "source": "WATCHLIST"},
        _LambdaContext(240_000),
    )

    [args] = captured
    assert args[-1]() == 240_000


def test_task_path_with_a_context_that_has_no_remaining_time_passes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    captured = _capture_process_single_candidate(monkeypatch, handler_module)

    handler_module.handler(
        {"task": "buy_candidate", "stock_code": "2914", "source": "WATCHLIST"}, _FakeContext()
    )

    assert captured[0][-1] is None


# --- W6 AST: 呼び出しの位置 -------------------------------------------------------


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        c
        for c in ast.walk(node)
        if isinstance(c, ast.Call)
        and (
            (isinstance(c.func, ast.Name) and c.func.id == name)
            or (isinstance(c.func, ast.Attribute) and c.func.attr == name)
        )
    ]


@pytest.fixture(scope="module")
def handler_tree() -> ast.Module:
    return ast.parse(_HANDLER_PATH.read_text(encoding="utf-8"))


def test_shadow_call_follows_the_completion_mark_in_the_same_block(
    handler_tree: ast.Module,
) -> None:
    fn = _function(handler_tree, "_process_single_candidate")
    [mark] = _calls(fn, "mark_completion_finalize_completed")
    [shadow] = _calls(fn, "_observe_allocation_shadow_safely")

    assert shadow.lineno > mark.lineno
    # 同じ `else:`(finalize の実行権を取得した分岐)の本文の直下にあり、try の外にある
    owners = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.If)
        and any(isinstance(s, ast.Expr) and s.value is shadow for s in node.orelse)
    ]
    assert len(owners) == 1
    branch = owners[0].orelse
    positions = {id(s): i for i, s in enumerate(branch)}
    mark_stmt = next(s for s in branch if isinstance(s, ast.Expr) and s.value is mark)
    shadow_stmt = next(s for s in branch if isinstance(s, ast.Expr) and s.value is shadow)
    assert positions[id(shadow_stmt)] == positions[id(mark_stmt)] + 1  # 完了記録の直後
    keyword_names = {k.arg for k in shadow.keywords}
    assert {"batch_id", "now", "execution_context", "audit_service", "remaining_time_ms"} <= (
        keyword_names
    )


def test_shadow_is_not_placed_in_recovery_or_inside_finalize(handler_tree: ast.Module) -> None:
    assert (
        _calls(
            _function(handler_tree, "_run_finalize_only_recovery"),
            "_observe_allocation_shadow_safely",
        )
        == []
    )
    assert (
        _calls(_function(handler_tree, "_finalize_batch"), "_observe_allocation_shadow_safely")
        == []
    )
    assert _calls(_function(handler_tree, "_finalize_batch"), "observe_allocation_shadow") == []


def test_entry_is_referenced_only_through_the_isolating_wrapper(handler_tree: ast.Module) -> None:
    wrapper = _function(handler_tree, "_observe_allocation_shadow_safely")
    all_calls = _calls(handler_tree, "observe_allocation_shadow")

    assert len(all_calls) == 1  # handler 全体でこの 1 箇所だけ
    inside = _calls(wrapper, "observe_allocation_shadow")
    assert inside == all_calls
    # 入口を握る try / except Exception の中にある
    tries = [n for n in ast.walk(wrapper) if isinstance(n, ast.Try)]
    assert any(
        all_calls[0] in list(ast.walk(t)) and t.handlers and t.handlers[0].type is not None
        for t in tries
    )
