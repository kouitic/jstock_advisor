"""Issue #56: 復旧経路の job_type routing と finalizer の fail-close。

`WATCHLIST_MAINTENANCE` バッチが**新規追加用(`NEW_CANDIDATE_SCREENING`)の
finalizer で終端される**問題を防ぐ。

dispatcher と worker は `job_type` で finalizer を分岐していたが、
**terminal-failure handler と reconciler は `job_type` を読まず、常に ADD 用の
`maybe_finalize` を呼んでいた**。共有 primitive の `try_finalize_if_ready` にも
`job_type` 条件が無いため、メンテナンス業務(自動削除・連続非該当カウント更新・
監視スコア更新)が一切実行されないまま COMPLETED(終端)となり、
状態が終端であるため二度と実行されなくなっていた。

## このファイルが固定する2層

1. **routing**: terminal-failure handler / reconciler RUNNING rescue が
   `job_type` に応じた finalizer を呼ぶこと。未知値は fail-close
2. **defense in depth**: finalizer の public boundary が、
   **状態遷移(`try_finalize_if_ready`)より前**に job_type を検証すること

2 が必要なのは、呼び出し側の分岐だけを唯一の防御にすると、
復旧経路が増えるたびに同じ取り違えが再発するためである(実際に本Issueで発生した)。
finalizer 本体の冒頭で return する方式は採れない
(その時点で既に `FINALIZE_PREPARING` へ遷移済みで、バッチを中間状態へ取り残す)。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest.mock import patch

import pytest

from jstock_advisor.lambda_handlers import (
    watchlist_batch_reconciler_handler as reconciler_module,
)
from jstock_advisor.lambda_handlers import (
    watchlist_terminal_failure_handler as terminal_module,
)
from jstock_advisor.services import watchlist_batch_finalizer as finalizer_module

_NOW = dt.datetime(2026, 8, 30, 7, 0, tzinfo=dt.UTC)
_BATCH_ID = "watchlist-maint-20260830T070000-abcd1234"

_ADD = "NEW_CANDIDATE_SCREENING"
_MAINTENANCE = "WATCHLIST_MAINTENANCE"


# ============================================================================
# defense in depth: finalizer の public boundary
# ============================================================================


class _Spy:
    """finalize の内部呼び出し順を記録する。

    `try_finalize_if_ready` が呼ばれたかどうかで
    「状態遷移の前に拒否できたか」を判定する。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def record(self, name: str, result: bool = True):
        def _inner(*_a: Any, **_k: Any) -> bool:
            self.calls.append(name)
            return result

        return _inner


def _run_finalizer(batch_item: dict[str, Any], call):
    spy = _Spy()
    with (
        patch.object(finalizer_module, "get_watchlist_batch", lambda _b: batch_item),
        patch.object(
            finalizer_module, "try_finalize_if_ready", spy.record("try_finalize_if_ready")
        ),
        patch.object(finalizer_module, "try_retry_finalize", spy.record("try_retry_finalize")),
        patch.object(
            finalizer_module, "try_retry_notification", spy.record("try_retry_notification")
        ),
        patch.object(finalizer_module, "_finalize_completed", spy.record("_finalize_completed")),
        patch.object(
            finalizer_module,
            "_finalize_maintenance_completed",
            spy.record("_finalize_maintenance_completed"),
        ),
    ):
        returned = call()
    return returned, spy.calls


def test_maybe_finalize_accepts_new_candidate_screening() -> None:
    returned, calls = _run_finalizer(
        {"job_type": _ADD},
        lambda: finalizer_module.maybe_finalize(_BATCH_ID, _NOW, None, None, None),
    )

    assert returned is True
    assert calls == ["try_finalize_if_ready", "_finalize_completed"]


def test_maybe_finalize_rejects_maintenance_before_state_transition() -> None:
    """T7: ADD finalizer へ maintenance batch を渡しても状態遷移前に拒否する。"""
    returned, calls = _run_finalizer(
        {"job_type": _MAINTENANCE},
        lambda: finalizer_module.maybe_finalize(_BATCH_ID, _NOW, None, None, None),
    )

    assert returned is False
    assert calls == [], "try_finalize_if_ready より前に拒否していない"


def test_maybe_finalize_maintenance_accepts_maintenance() -> None:
    returned, calls = _run_finalizer(
        {"job_type": _MAINTENANCE},
        lambda: finalizer_module.maybe_finalize_maintenance(_BATCH_ID, _NOW, None),
    )

    assert returned is True
    assert calls == ["try_finalize_if_ready", "_finalize_maintenance_completed"]


def test_maybe_finalize_maintenance_rejects_add_before_state_transition() -> None:
    """T8: maintenance finalizer へ ADD batch を渡しても状態遷移前に拒否する。"""
    returned, calls = _run_finalizer(
        {"job_type": _ADD},
        lambda: finalizer_module.maybe_finalize_maintenance(_BATCH_ID, _NOW, None),
    )

    assert returned is False
    assert calls == []


@pytest.mark.parametrize(
    "call_name",
    ["maybe_finalize", "maybe_finalize_maintenance"],
)
def test_finalizer_rejects_unknown_job_type(call_name: str) -> None:
    """未知の job_type は暗黙にどちらかへ倒さず fail-close する。"""
    calls_by_name = {
        "maybe_finalize": lambda: finalizer_module.maybe_finalize(
            _BATCH_ID, _NOW, None, None, None
        ),
        "maybe_finalize_maintenance": lambda: finalizer_module.maybe_finalize_maintenance(
            _BATCH_ID, _NOW, None
        ),
    }

    returned, calls = _run_finalizer({"job_type": "BOGUS_JOB_TYPE"}, calls_by_name[call_name])

    assert returned is False
    assert calls == []


def test_maybe_finalize_accepts_legacy_record_without_job_type() -> None:
    """job_type キー自体が無い旧レコードは ADD として扱う(後方互換)。"""
    returned, calls = _run_finalizer(
        {},
        lambda: finalizer_module.maybe_finalize(_BATCH_ID, _NOW, None, None, None),
    )

    assert returned is True
    assert calls == ["try_finalize_if_ready", "_finalize_completed"]


@pytest.mark.parametrize(
    "call_name",
    ["retry_finalize", "retry_notification"],
)
def test_retry_paths_are_add_only(call_name: str) -> None:
    """`retry_finalize` / `retry_notification` は ADD-only contract。

    `_finalize_maintenance_completed()` は FINALIZE_FAILED /
    NOTIFICATION_FAILED を業務フローとして使わないため、maintenance batch が
    正常フローでここへ到達することはない。maintenance 用の retry 機能は作らず、
    **想定外に到達した場合に ADD 処理を行わない**ことだけを保証する。
    """
    calls_by_name = {
        "retry_finalize": lambda: finalizer_module.retry_finalize(
            _BATCH_ID, _NOW, None, None, None
        ),
        "retry_notification": lambda: finalizer_module.retry_notification(
            _BATCH_ID, _NOW, None, None, None
        ),
    }

    returned, calls = _run_finalizer({"job_type": _MAINTENANCE}, calls_by_name[call_name])

    assert returned is False
    assert calls == []


# ============================================================================
# routing: terminal-failure handler
# ============================================================================


def _run_terminal_handler(job_type: str | None) -> list[str]:
    body: dict[str, Any] = {"batch_id": _BATCH_ID, "stock_code": "9999"}
    if job_type is not None:
        body["job_type"] = job_type
    event = {"Records": [{"body": json.dumps(body)}]}
    routed: list[str] = []

    with (
        patch.object(terminal_module, "load_config", lambda: None),
        patch.object(terminal_module, "build_real_provider_bundle", lambda *a, **k: object()),
        patch.object(terminal_module, "build_cached_provider_bundle", lambda *a, **k: object()),
        patch.object(terminal_module, "_build_notification_service", lambda _c: object()),
        patch.object(terminal_module, "record_terminal_failure", lambda *a, **k: True),
        patch.object(
            terminal_module, "maybe_finalize", lambda *a, **k: routed.append("add") or True
        ),
        patch.object(
            terminal_module,
            "maybe_finalize_maintenance",
            lambda *a, **k: routed.append("maintenance") or True,
        ),
    ):
        result = terminal_module.handler(event, None)

    # terminal failure 自体の記録は job_type に関わらず必ず処理済みとして返る
    assert result["processed"] == [{"batch_id": _BATCH_ID, "stock_code": "9999"}]
    return routed


def test_terminal_failure_routes_maintenance_to_maintenance_finalizer() -> None:
    """T1: maintenance × terminal failure → maintenance finalizer。"""
    assert _run_terminal_handler(_MAINTENANCE) == ["maintenance"]


def test_terminal_failure_routes_add_to_add_finalizer() -> None:
    """T2: ADD × terminal failure → ADD finalizer(既存挙動の回帰)。"""
    assert _run_terminal_handler(_ADD) == ["add"]


def test_terminal_failure_unknown_job_type_fails_closed() -> None:
    """T3: 未知の job_type はどちらの finalizer も呼ばない。"""
    assert _run_terminal_handler("BOGUS_JOB_TYPE") == []


def test_terminal_failure_missing_job_type_defaults_to_add() -> None:
    """job_type キーが無い旧メッセージは ADD として扱う(後方互換)。"""
    assert _run_terminal_handler(None) == ["add"]


# ============================================================================
# routing: reconciler RUNNING rescue
# ============================================================================


def _run_reconciler_rescue(job_type: str | None) -> list[str]:
    batch_item: dict[str, Any] = {
        "batch_id": _BATCH_ID,
        "status": "RUNNING",
        "started_at": _NOW.isoformat(),
    }
    if job_type is not None:
        batch_item["job_type"] = job_type
    routed: list[str] = []

    with (
        patch.object(reconciler_module, "load_config", lambda: _fake_config()),
        patch.object(reconciler_module, "build_real_provider_bundle", lambda *a, **k: object()),
        patch.object(reconciler_module, "build_cached_provider_bundle", lambda *a, **k: object()),
        patch.object(reconciler_module, "_build_notification_service", lambda _c: object()),
        patch.object(
            reconciler_module,
            "list_watchlist_batches_by_status",
            lambda _statuses: [batch_item],
        ),
        patch.object(reconciler_module, "list_stale_maintenance_triggers", lambda *a, **k: []),
        patch.object(
            reconciler_module, "maybe_finalize", lambda *a, **k: routed.append("add") or True
        ),
        patch.object(
            reconciler_module,
            "maybe_finalize_maintenance",
            lambda *a, **k: routed.append("maintenance") or True,
        ),
    ):
        reconciler_module.handler({}, None)

    return routed


def _fake_config() -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        watchlist_screening=SimpleNamespace(
            batch_processing_timeout_hours=24,
            timeout_finalize_max_rows_per_run=100,
            notification_failure_retry_limit=3,
            finalize_stuck_minutes=30,
            candidate_universe=SimpleNamespace(provider="jpx"),
            auto_removal=SimpleNamespace(readd_cooldown_days=30),
            screening_policy="multi_style_monitoring",
        )
    )


def test_reconciler_rescue_routes_maintenance_to_maintenance_finalizer() -> None:
    """T4: maintenance × reconciler RUNNING rescue → maintenance finalizer。"""
    assert _run_reconciler_rescue(_MAINTENANCE) == ["maintenance"]


def test_reconciler_rescue_routes_add_to_add_finalizer() -> None:
    """T5: ADD × reconciler RUNNING rescue → ADD finalizer(既存挙動の回帰)。"""
    assert _run_reconciler_rescue(_ADD) == ["add"]


def test_reconciler_rescue_unknown_job_type_fails_closed() -> None:
    """T6: 未知の job_type はどちらの finalizer も呼ばない。"""
    assert _run_reconciler_rescue("BOGUS_JOB_TYPE") == []


# ============================================================================
# audit semantics / 終端状態: maintenance batch が ADD として記録されないこと
# ============================================================================


def test_maintenance_finalizer_audits_with_maintenance_universe_provider() -> None:
    """T9-a: maintenance 完了監査の `universe_provider` は ADD の候補ユニバースでない。

    ADD 用の `candidate_universe.provider` で記録されると、メンテナンス実行が
    「新規追加バッチ」として監査に残り、意味が食い違う。
    """
    audits: list[dict[str, Any]] = []

    with (
        patch.object(finalizer_module, "query_all_candidate_progress", lambda *a, **k: []),
        patch.object(finalizer_module, "WatchlistRepository", lambda *a, **k: object()),
        patch.object(
            finalizer_module, "WatchlistRemovalHistoryRepository", lambda *a, **k: object()
        ),
        patch.object(finalizer_module, "record_batch_audit", lambda **kw: audits.append(kw)),
        patch.object(finalizer_module, "mark_watchlist_batch_completed", lambda *a, **k: True),
    ):
        finalizer_module._finalize_maintenance_completed(_BATCH_ID, _NOW, _fake_config())

    assert [a["universe_provider"] for a in audits] == [
        finalizer_module.MAINTENANCE_UNIVERSE_PROVIDER
    ]


def _run_timeout_finalizing(job_type: str | None) -> str:
    from types import SimpleNamespace

    batch_item: dict[str, Any] = {"batch_id": _BATCH_ID, "started_at": _NOW.isoformat()}
    if job_type is not None:
        batch_item["job_type"] = job_type
    audits: list[dict[str, Any]] = []
    pass_result = SimpleNamespace(terminal_count=3, total=3, newly_failed_count=0, all_records=[])

    with (
        patch.object(
            reconciler_module, "run_timeout_finalization_pass", lambda *a, **k: pass_result
        ),
        patch.object(
            reconciler_module, "set_timeout_finalize_completed_count", lambda *a, **k: True
        ),
        patch.object(reconciler_module, "get_watchlist_batch", lambda _b: batch_item),
        patch.object(
            reconciler_module, "compute_batch_metrics", lambda _r: {"processed_count": 3}
        ),
        patch.object(reconciler_module, "record_batch_audit", lambda **kw: audits.append(kw)),
        patch.object(
            reconciler_module, "transition_timeout_finalizing_to_timed_out", lambda *a, **k: True
        ),
        patch.object(
            reconciler_module, "release_rotation_dispatch_lease", lambda *a, **k: True
        ),
    ):
        reconciler_module._process_timeout_finalizing(_BATCH_ID, _NOW, 100, _fake_config())

    assert len(audits) == 1
    return str(audits[0]["universe_provider"])


def test_timeout_finalizing_audits_maintenance_batch_as_maintenance() -> None:
    """T9-b: RUNNING 救済に失敗した maintenance batch も ADD の provider で記録しない。

    TIMED_OUT は 14 節どおり部分結果の登録・通知を行わないため誤りは監査の
    意味論に限られるが、`job_type` に追随させる。
    """
    assert _run_timeout_finalizing(_MAINTENANCE) == finalizer_module.MAINTENANCE_UNIVERSE_PROVIDER


def test_timeout_finalizing_audits_add_batch_with_candidate_universe_provider() -> None:
    """ADD batch は従来どおり候補ユニバースの provider で記録する(回帰)。"""
    assert _run_timeout_finalizing(_ADD) == "jpx"
    assert _run_timeout_finalizing(None) == "jpx"


def test_maintenance_batch_is_not_completed_through_add_finalizer() -> None:
    """T10: maintenance batch を ADD finalizer へ渡しても終端状態にしない。

    ADD finalizer が COMPLETED まで進めてしまうと、メンテナンス業務
    (自動削除・連続非該当カウント更新・監視スコア更新)が一切実行されないまま
    終端となり、状態が終端であるため二度と実行されない。
    FINALIZE_FAILED(異常終端)にもしないこと——正常な「担当外」だからである。
    """
    terminal: list[str] = []

    with (
        patch.object(
            finalizer_module, "get_watchlist_batch", lambda _b: {"job_type": _MAINTENANCE}
        ),
        patch.object(finalizer_module, "try_finalize_if_ready", lambda *a, **k: True),
        patch.object(
            finalizer_module,
            "mark_watchlist_batch_completed",
            lambda *a, **k: terminal.append("completed"),
        ),
        patch.object(
            finalizer_module,
            "mark_watchlist_finalize_failed",
            lambda *a, **k: terminal.append("finalize_failed"),
        ),
    ):
        returned = finalizer_module.maybe_finalize(_BATCH_ID, _NOW, None, None, None)

    assert returned is False
    assert terminal == []
