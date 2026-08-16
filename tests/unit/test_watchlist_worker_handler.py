"""横断整合性レビュー対応(2026-08、指摘1・High): WatchlistWorkerFunctionの
job_type解釈のテスト。

Dispatcher側は「NEW_CANDIDATE_SCREENING以外はmaintenance扱い」、Worker側は
「WATCHLIST_MAINTENANCE以外はnew candidate扱い」という非対称な暗黙fallback
が存在すると、typo等の未知job_typeでDispatcherとWorkerの解釈が食い違う
危険があったため、`batch_tracker.resolve_watchlist_job_type()`を全経路で
唯一の入口とするよう修正した。このテストはWorker側の受け入れ・拒否挙動の
みを検証する(Dispatcher側は test_watchlist_dispatcher_handler.py 参照)。

重い依存(provider bundle構築・LINE通知・screening evaluate等)はすべて
フェイク化し、job_typeの解釈・分岐のみに焦点を当てる。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from jstock_advisor.infrastructure.aws.batch_tracker import (
    UnknownWatchlistJobTypeError,
    WatchlistJobType,
    WatchlistProgressStatus,
)
from jstock_advisor.lambda_handlers import watchlist_worker_handler as handler_module


def _fake_config() -> Any:
    return object()


def _sqs_event(body: dict[str, Any]) -> dict[str, Any]:
    return {"Records": [{"body": json.dumps(body)}]}


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """job_type解決より後段の重い依存をすべてフェイク化する。呼び出しの
    記録用に各種callのリストを返す。"""
    monkeypatch.setattr(handler_module, "load_config", _fake_config)
    monkeypatch.setattr(handler_module, "build_real_provider_bundle", lambda *a, **kw: object())
    monkeypatch.setattr(
        handler_module, "build_cached_provider_bundle", lambda *a, **kw: object()
    )
    monkeypatch.setattr(
        handler_module, "_build_notification_service", lambda config: object()
    )
    monkeypatch.setattr(
        handler_module, "claim_candidate_lease", lambda *a, **kw: True
    )
    calls: dict[str, list[Any]] = {
        "evaluate_candidate": [],
        "complete_candidate": [],
        "maybe_finalize": [],
        "maybe_finalize_maintenance": [],
    }

    def _fake_evaluate_candidate(stock_code, batch_id, now, providers, config, job_type):  # noqa: ANN001, ANN201
        calls["evaluate_candidate"].append(job_type)
        return handler_module._EvaluationOutcome(
            WatchlistProgressStatus.COMPLETED, "PASSED", None, False, []
        )

    monkeypatch.setattr(handler_module, "_evaluate_candidate", _fake_evaluate_candidate)

    def _fake_complete_candidate(*a: Any, **kw: Any) -> bool:
        calls["complete_candidate"].append(kw)
        return True

    monkeypatch.setattr(handler_module, "complete_candidate", _fake_complete_candidate)
    monkeypatch.setattr(
        handler_module,
        "maybe_finalize",
        lambda *a, **kw: calls["maybe_finalize"].append((a, kw)),
    )
    monkeypatch.setattr(
        handler_module,
        "maybe_finalize_maintenance",
        lambda *a, **kw: calls["maybe_finalize_maintenance"].append((a, kw)),
    )
    return calls


# --- NEW_CANDIDATE_SCREENING/WATCHLIST_MAINTENANCEが全経路で正しく扱われる ---


def test_worker_routes_new_candidate_screening_to_maybe_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_common(monkeypatch)
    event = _sqs_event(
        {"batch_id": "batch-1", "stock_code": "1111", "job_type": "NEW_CANDIDATE_SCREENING"}
    )

    result = handler_module.handler(event, object())

    assert calls["evaluate_candidate"] == [WatchlistJobType.NEW_CANDIDATE_SCREENING]
    assert len(calls["maybe_finalize"]) == 1
    assert len(calls["maybe_finalize_maintenance"]) == 0
    assert result["processed"][0]["completed"] is True


def test_worker_routes_watchlist_maintenance_to_maybe_finalize_maintenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_common(monkeypatch)
    event = _sqs_event(
        {"batch_id": "watchlist-maint-1", "stock_code": "1111", "job_type": "WATCHLIST_MAINTENANCE"}
    )

    handler_module.handler(event, object())

    assert calls["evaluate_candidate"] == [WatchlistJobType.WATCHLIST_MAINTENANCE]
    assert len(calls["maybe_finalize"]) == 0
    assert len(calls["maybe_finalize_maintenance"]) == 1


# --- typo/UNKNOWN・欠損はfail-closedで拒否する ------------------------------


def test_worker_rejects_unknown_job_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """typo等の未知job_typeは例外を送出し、この1メッセージの処理だけを失敗
    させる(SQS BatchSize=1のため、既存のインフラ障害用の再送・DLQ機構へ
    そのまま乗る)。lease取得すら行わないことも確認する。"""
    calls = _patch_common(monkeypatch)

    def _fail_if_called(*a: Any, **kw: Any) -> bool:
        pytest.fail("claim_candidate_lease should not be called for unknown job_type")

    monkeypatch.setattr(handler_module, "claim_candidate_lease", _fail_if_called)
    event = _sqs_event(
        {"batch_id": "batch-1", "stock_code": "1111", "job_type": "WATCHLIST_MAINTENENCE"}
    )

    with pytest.raises(UnknownWatchlistJobTypeError):
        handler_module.handler(event, object())

    assert calls["evaluate_candidate"] == []


def test_worker_rejects_missing_job_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """job_typeキー自体が無いSQSメッセージも、NEW_CANDIDATE_SCREENINGへ暗黙に
    フォールバックせず拒否する(Dispatcher側は必ず明示値を書き込むため、
    正常運用でこの状況は発生しない)。"""
    _patch_common(monkeypatch)
    event = _sqs_event({"batch_id": "batch-1", "stock_code": "1111"})

    with pytest.raises(UnknownWatchlistJobTypeError):
        handler_module.handler(event, object())


# --- parent-child maintenance triggerで正しいjob_typeが伝播する -------------


def test_worker_receives_maintenance_job_type_propagated_from_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispatcherが平日毎日起動化の後続トリガー(maybe_trigger_maintenance)
    経由で組み立てたSQSメッセージ本文と同じ形(job_type明示値・
    WATCHLIST_MAINTENANCE)をWorkerが正しく解釈できることを確認する
    (Dispatcher側の実際のペイロード構築は
    watchlist_dispatcher_handler._send_batch_with_retry呼び出し元を参照)。"""
    calls = _patch_common(monkeypatch)
    event = _sqs_event(
        {
            "batch_id": "watchlist-maint-batch-1",
            "stock_code": "1111",
            "job_type": WatchlistJobType.WATCHLIST_MAINTENANCE.value,
        }
    )

    handler_module.handler(event, object())

    assert calls["evaluate_candidate"] == [WatchlistJobType.WATCHLIST_MAINTENANCE]
    assert len(calls["maybe_finalize_maintenance"]) == 1
