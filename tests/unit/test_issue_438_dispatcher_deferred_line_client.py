"""Issue #438(USER決定A): dispatcherのLINE認証情報の欠落を「送信時の失敗」にする。

worker / terminal_failure(#430)、reconciler(#429)と同じ方針。dispatcherは
NEW_CANDIDATE_SCREENINGで、LINE認証情報が無くても通知サービスの構築で失敗しない。
候補発見(dispatch lease・BatchRuns行・進捗行・SQS投入)は進み、通知が実際に
必要になった(finalizeが送信を試みた)場合だけ、handlerの末尾で
`LineCredentialsMissingError`を送出してErrorsとして顕在化する。

確認するもの:
* deferred clientは成功を返さず、送信の試みを記憶する(送信メソッド3種)
* 認証情報の欠落(`LineCredentialsMissingError`)だけをdeferredへ変える(他の例外は握りつぶさない)
* 認証情報が無くてもdispatchが最後まで進む(状態変更が完了する)
* finalizeが送信を試みた場合だけ、状態変更の完了後に末尾で例外が上がる
* 認証情報があればLiveLineClientを使い、末尾で例外を上げない
* maintenanceは通知サービスを構築しない
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.infrastructure.line.client import (
    LineCredentialsMissingError,
    LiveLineClient,
)
from jstock_advisor.lambda_handlers import watchlist_dispatcher_handler as handler_module


def _fake_config() -> SimpleNamespace:
    watchlist_screening = SimpleNamespace(
        enabled=True,
        scheduled_run_enabled=True,
        candidate_universe=SimpleNamespace(provider="csv"),
        screening_policy="high_dividend_financial_health",
        staged_rollout=SimpleNamespace(candidate_limit=300, market_segment_filter=None),
        batch_record_ttl_hours=72,
        candidate_progress_ttl_hours=72,
        rotation=SimpleNamespace(enabled=False),
        batch_processing_timeout_hours=24,
    )
    return SimpleNamespace(watchlist_screening=watchlist_screening)


# --- deferred client の単体 ----------------------------------------------------------


def test_deferred_client_never_succeeds_and_remembers_the_attempt() -> None:
    client = handler_module._CredentialDeferredLineClient(LineCredentialsMissingError("missing"))

    client.raise_if_send_attempted()  # 送信を試みていなければ送出しない
    assert client.send_attempted is False

    with pytest.raises(LineCredentialsMissingError):
        client.push_message("x")
    assert client.send_attempted is True
    with pytest.raises(LineCredentialsMissingError):
        client.raise_if_send_attempted()


@pytest.mark.parametrize("method", ["push_message", "reply_message", "reply_messages"])
def test_deferred_client_fails_for_every_send_method(method: str) -> None:
    client = handler_module._CredentialDeferredLineClient(LineCredentialsMissingError("missing"))
    args: tuple[Any, ...] = {
        "push_message": ("text",),
        "reply_message": ("token", "text"),
        "reply_messages": ("token", ["text"]),
    }[method]

    with pytest.raises(LineCredentialsMissingError):
        getattr(client, method)(*args)
    assert client.send_attempted is True


def test_builder_returns_live_client_when_credentials_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "token-value")
    monkeypatch.setenv("LINE_USER_ID", "user-value")

    client = handler_module._build_dispatcher_line_client()

    assert isinstance(client, LiveLineClient)


def test_builder_returns_deferred_client_when_credentials_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_USER_ID", raising=False)

    client = handler_module._build_dispatcher_line_client()

    assert isinstance(client, handler_module._CredentialDeferredLineClient)


def test_builder_does_not_swallow_other_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(handler_module, "build_live_line_client_from_env", _boom)

    with pytest.raises(RuntimeError, match="unexpected"):
        handler_module._build_dispatcher_line_client()


# --- handler の末尾まで通す -----------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.service: Any = None
        self.finalize_sends = False

    def note(self, name: str) -> None:
        self.calls.append(name)


@pytest.fixture
def run_env(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """NEW_CANDIDATE_SCREENINGの正常系を、状態変更・外部アクセスなしで最後まで通す。"""
    rec = _Recorder()
    monkeypatch.setenv("WATCHLIST_SCREENING_QUEUE_URL", "queue-url")
    monkeypatch.delenv("ALLOW_FULL_MARKET_SCREENING", raising=False)
    monkeypatch.setattr(handler_module, "load_config", _fake_config)
    monkeypatch.setattr(handler_module, "record_batch_audit", lambda **kw: rec.note("audit"))
    monkeypatch.setattr(handler_module, "should_skip_for_market_closed", lambda *a, **kw: False)

    def _lease(*a: Any, **kw: Any) -> bool:
        rec.note("dispatch_lease")
        return True

    monkeypatch.setattr(handler_module, "try_acquire_dispatch_lease", _lease)
    monkeypatch.setattr(
        handler_module, "_collect_new_candidate_targets", lambda *a, **kw: (["1301"], {})
    )
    monkeypatch.setattr(
        handler_module, "set_watchlist_batch_total", lambda *a, **kw: rec.note("batch_total")
    )
    monkeypatch.setattr(
        handler_module,
        "create_missing_candidate_progress_rows",
        lambda *a, **kw: rec.note("progress_rows"),
    )
    monkeypatch.setattr(
        handler_module,
        "query_all_candidate_progress",
        lambda *a, **kw: [SimpleNamespace(stock_code="1301", dispatched=False)],
    )
    monkeypatch.setattr(handler_module.boto3, "client", lambda *a, **kw: object())
    monkeypatch.setattr(handler_module, "_send_batch_with_retry", lambda *a, **kw: {"1301": True})
    monkeypatch.setattr(
        handler_module, "mark_candidate_dispatched", lambda *a, **kw: rec.note("dispatched")
    )
    monkeypatch.setattr(
        handler_module, "mark_dispatch_completed", lambda *a, **kw: rec.note("dispatch_completed")
    )
    monkeypatch.setattr(handler_module, "build_real_provider_bundle", lambda *a, **kw: object())
    monkeypatch.setattr(handler_module, "build_cached_provider_bundle", lambda *a, **kw: object())

    real_build = handler_module._build_notification_service

    def _build(config: Any, line_client: Any = None) -> Any:
        rec.note("build_notification_service")
        rec.service = real_build(config, line_client)
        return rec.service

    monkeypatch.setattr(handler_module, "_build_notification_service", _build)

    def _finalize(batch_id: str, now: Any, providers: Any, config: Any, service: Any) -> None:
        rec.note("finalize")
        if rec.finalize_sends:
            # finalizerのPhase 3は送信の例外を捕捉してNOTIFICATION_FAILEDと記録する
            # (handlerへは伝播しない)。それを模す。
            try:
                service._client.push_message("notice")
            except LineCredentialsMissingError:
                rec.note("notification_failed_recorded")

    monkeypatch.setattr(handler_module, "maybe_finalize", _finalize)
    return rec


def test_dispatch_completes_without_credentials_when_no_notification_is_needed(
    monkeypatch: pytest.MonkeyPatch, run_env: _Recorder
) -> None:
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_USER_ID", raising=False)

    result = handler_module.handler({}, object())

    assert result["dispatched"] == 1
    assert run_env.calls[:2] == [
        "build_notification_service",
        "dispatch_lease",
    ]  # 構築位置は従来どおり
    assert {"batch_total", "progress_rows", "dispatched", "dispatch_completed", "finalize"} <= set(
        run_env.calls
    )
    assert isinstance(run_env.service._client, handler_module._CredentialDeferredLineClient)


def test_missing_credentials_surface_at_the_end_only_after_state_changes_complete(
    monkeypatch: pytest.MonkeyPatch, run_env: _Recorder
) -> None:
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_USER_ID", raising=False)
    run_env.finalize_sends = True

    with pytest.raises(LineCredentialsMissingError):
        handler_module.handler({}, object())

    # 例外は、lease・batch・進捗行・SQS投入・完了記録・NOTIFICATION_FAILEDの記録を
    # 全て終えた後に上がる。
    assert {
        "dispatch_lease",
        "batch_total",
        "progress_rows",
        "dispatched",
        "dispatch_completed",
        "finalize",
        "notification_failed_recorded",
    } <= set(run_env.calls)
    assert run_env.calls.index("dispatch_completed") < run_env.calls.index("finalize")


def test_credentials_present_use_the_live_client_and_do_not_raise(
    monkeypatch: pytest.MonkeyPatch, run_env: _Recorder
) -> None:
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "token-value")
    monkeypatch.setenv("LINE_USER_ID", "user-value")

    result = handler_module.handler({}, object())

    assert result["dispatched"] == 1
    assert isinstance(run_env.service._client, LiveLineClient)


def test_maintenance_does_not_build_the_notification_service(
    monkeypatch: pytest.MonkeyPatch, run_env: _Recorder
) -> None:
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_USER_ID", raising=False)
    monkeypatch.setattr(
        handler_module, "_collect_maintenance_targets", lambda event: (["1301"], {})
    )
    monkeypatch.setattr(
        handler_module,
        "maybe_finalize_maintenance",
        lambda *a, **kw: run_env.note("finalize_maintenance"),
    )

    result = handler_module.handler({"job_type": "WATCHLIST_MAINTENANCE"}, object())

    assert result["dispatched"] == 1
    assert "build_notification_service" not in run_env.calls
    assert "finalize_maintenance" in run_env.calls
