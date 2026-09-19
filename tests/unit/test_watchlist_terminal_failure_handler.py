"""Issue #117 Phase B1b-4b: watchlist_terminal_failure_handlerのLINE client構築(認証情報)のテスト。

通知サービスを使うのはNEW_CANDIDATE_SCREENINGのfinalizeだけであり、認証情報の欠落は
**終端記録(状態変更)より前**に失敗させる。job_type欠損時の既定は本処理と同じ
NEW_CANDIDATE_SCREENING(workerの既定=Noneとは異なる)。

あわせて、workerで確認された「prescanと本処理が同じ判定になること」の退行防止(#425の
Phase 1条件F1・F2)を、terminal failure側の同型テストとworkerの回帰テストとして固定する。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from jstock_advisor.infrastructure.aws.batch_tracker import WatchlistJobType
from jstock_advisor.lambda_handlers import watchlist_terminal_failure_handler as handler_module

_BATCH_ID = "batch-1"


def _event(job_type: str | None, *, batch_id: str = _BATCH_ID) -> dict[str, Any]:
    body: dict[str, Any] = {"batch_id": batch_id, "stock_code": "9999"}
    if job_type is not None:
        body["job_type"] = job_type
    return {"Records": [{"body": json.dumps(body)}]}


def _patch_common(
    monkeypatch: pytest.MonkeyPatch, *, patch_notification_service: bool = True
) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"maybe_finalize": [], "maybe_finalize_maintenance": []}
    monkeypatch.setattr(handler_module, "load_config", lambda: None)
    monkeypatch.setattr(handler_module, "build_real_provider_bundle", lambda *a, **kw: object())
    monkeypatch.setattr(handler_module, "build_cached_provider_bundle", lambda *a, **kw: object())
    if patch_notification_service:
        monkeypatch.setattr(handler_module, "_build_notification_service", lambda _c: object())
    monkeypatch.setattr(handler_module, "record_terminal_failure", lambda *a, **kw: True)
    monkeypatch.setattr(
        handler_module, "maybe_finalize", lambda *a, **kw: calls["maybe_finalize"].append(a)
    )
    monkeypatch.setattr(
        handler_module,
        "maybe_finalize_maintenance",
        lambda *a, **kw: calls["maybe_finalize_maintenance"].append(a),
    )
    return calls


def _patch_order_recording(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    order: list[str] = []
    _patch_common(monkeypatch)

    def _build(config: Any) -> object:
        order.append("build_notification_service")
        return object()

    def _record(*a: Any, **kw: Any) -> bool:
        order.append("record_terminal_failure")
        return True

    monkeypatch.setattr(handler_module, "_build_notification_service", _build)
    monkeypatch.setattr(handler_module, "record_terminal_failure", _record)
    return order


# --- 構築の位置(状態変更より前)と要否 ----------------------------------------


def test_new_candidate_builds_the_service_before_recording_the_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _patch_order_recording(monkeypatch)

    handler_module.handler(_event("NEW_CANDIDATE_SCREENING"), None)

    assert order == ["build_notification_service", "record_terminal_failure"]


def test_maintenance_only_call_does_not_build_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _patch_order_recording(monkeypatch)

    handler_module.handler(_event("WATCHLIST_MAINTENANCE"), None)

    assert order == ["record_terminal_failure"]


def test_missing_job_type_builds_the_service_because_the_default_is_new_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """job_type欠損の旧メッセージは本処理でNEW_CANDIDATE_SCREENING扱い(finalizeが通知を使う)。
    prescanも同じ既定で判定し、状態変更の前に構築する。"""
    order = _patch_order_recording(monkeypatch)

    handler_module.handler(_event(None), None)

    assert order == ["build_notification_service", "record_terminal_failure"]


def test_unknown_job_type_does_not_build_the_service_and_skips_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未知値は本処理がfinalizeをskipする(通知を使わない)。構築も不要。"""
    order = _patch_order_recording(monkeypatch)

    result = handler_module.handler(_event("WATCHLIST_MAINTENENCE"), None)

    assert order == ["record_terminal_failure"]
    assert result["processed"] == [{"batch_id": _BATCH_ID, "stock_code": "9999"}]


# --- 認証情報(構築関数を差し替えない実分岐) ---------------------------------


def test_new_candidate_fails_before_state_change_when_line_credentials_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jstock_advisor.infrastructure.line.client import LineCredentialsMissingError

    _patch_common(monkeypatch, patch_notification_service=False)
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_USER_ID", raising=False)

    def _record_must_not_run(*a: Any, **kw: Any) -> bool:
        pytest.fail("record_terminal_failure must not run before the notification service is built")

    monkeypatch.setattr(handler_module, "record_terminal_failure", _record_must_not_run)

    with pytest.raises(LineCredentialsMissingError):
        handler_module.handler(_event("NEW_CANDIDATE_SCREENING"), None)


def test_maintenance_call_runs_without_line_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_common(monkeypatch, patch_notification_service=False)
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_USER_ID", raising=False)

    result = handler_module.handler(_event("WATCHLIST_MAINTENANCE"), None)

    assert len(calls["maybe_finalize_maintenance"]) == 1
    assert result["processed"] == [{"batch_id": _BATCH_ID, "stock_code": "9999"}]


def test_built_service_uses_a_live_client_when_credentials_are_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jstock_advisor.config.loader import load_config
    from jstock_advisor.infrastructure.line.client import LiveLineClient

    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("LINE_USER_ID", "test-user")

    service = handler_module._build_notification_service(load_config())

    assert isinstance(service._client, LiveLineClient)


# --- F1: prescanへ渡す既定値が本処理と同じであること -------------------------------


def test_f1_terminal_failure_passes_the_new_candidate_default_to_the_prescan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本処理の resolve_watchlist_job_type(default=NEW_CANDIDATE_SCREENING) と同じ既定を
    prescanへ渡すこと。既定値を変えるとこのテストが赤くなる(判定の二重化が崩れる経路)。"""
    _patch_common(monkeypatch)
    seen: list[WatchlistJobType | None] = []

    def _spy(event: dict[str, Any], *, missing_job_type_default: WatchlistJobType | None) -> bool:
        seen.append(missing_job_type_default)
        return True

    monkeypatch.setattr(handler_module, "sqs_records_require_notification_service", _spy)

    handler_module.handler(_event("NEW_CANDIDATE_SCREENING"), None)

    assert seen == [WatchlistJobType.NEW_CANDIDATE_SCREENING]


def test_f1_worker_passes_no_default_to_the_prescan(monkeypatch: pytest.MonkeyPatch) -> None:
    """workerの本処理は resolve_watchlist_job_type(body.get("job_type")) で既定を持たない
    (欠損は例外)。prescanにも既定を渡さない(=None)。terminal failureの既定と取り違えない。"""
    from jstock_advisor.lambda_handlers import watchlist_worker_handler as worker_module

    seen: list[WatchlistJobType | None] = []

    def _spy(event: dict[str, Any], *, missing_job_type_default: WatchlistJobType | None) -> bool:
        seen.append(missing_job_type_default)
        raise _StopHereError

    monkeypatch.setattr(worker_module, "load_config", lambda: None)
    monkeypatch.setattr(worker_module, "build_real_provider_bundle", lambda *a, **kw: object())
    monkeypatch.setattr(worker_module, "build_cached_provider_bundle", lambda *a, **kw: object())
    monkeypatch.setattr(worker_module, "sqs_records_require_notification_service", _spy)

    with pytest.raises(_StopHereError):
        worker_module.handler(_event("NEW_CANDIDATE_SCREENING"), None)

    assert seen == [None]


class _StopHereError(Exception):
    """prescanの直後でhandlerを止めるための番兵。"""


# --- F2: prescanと本処理が乖離したら、通知欠落ではなく明示的な失敗になること --------


def test_f2_terminal_failure_fails_explicitly_when_the_prescan_diverges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prescanがFalse(構築しない)を返したのに、本処理がNEW_CANDIDATE_SCREENINGへ進んだ場合、
    通知サービス無しでfinalizeを呼んで通知を黙って欠落させず、RuntimeErrorで失敗する。"""
    calls = _patch_common(monkeypatch)
    monkeypatch.setattr(
        handler_module, "sqs_records_require_notification_service", lambda *a, **kw: False
    )

    with pytest.raises(RuntimeError, match="notification service was not built"):
        handler_module.handler(_event("NEW_CANDIDATE_SCREENING"), None)

    assert calls["maybe_finalize"] == []


def test_f2_worker_fails_explicitly_when_the_prescan_diverges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """workerも同じ。prescanが乖離しても、通知欠落ではなく明示的な失敗になる。"""
    from jstock_advisor.infrastructure.aws.batch_tracker import WatchlistProgressStatus
    from jstock_advisor.lambda_handlers import watchlist_worker_handler as worker_module

    finalized: list[Any] = []
    monkeypatch.setattr(worker_module, "load_config", lambda: None)
    monkeypatch.setattr(worker_module, "build_real_provider_bundle", lambda *a, **kw: object())
    monkeypatch.setattr(worker_module, "build_cached_provider_bundle", lambda *a, **kw: object())
    monkeypatch.setattr(
        worker_module, "sqs_records_require_notification_service", lambda *a, **kw: False
    )
    monkeypatch.setattr(worker_module, "claim_candidate_lease", lambda *a, **kw: True)
    monkeypatch.setattr(
        worker_module,
        "_evaluate_candidate",
        lambda *a, **kw: worker_module._EvaluationOutcome(
            WatchlistProgressStatus.COMPLETED, "PASSED", None, False, []
        ),
    )
    monkeypatch.setattr(worker_module, "complete_candidate", lambda *a, **kw: True)
    monkeypatch.setattr(worker_module, "maybe_finalize", lambda *a, **kw: finalized.append(a))

    with pytest.raises(RuntimeError, match="notification service was not built"):
        worker_module.handler(_event("NEW_CANDIDATE_SCREENING"), object())

    assert finalized == []
