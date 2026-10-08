"""Issue #434: `cli/watchlist_screening.py` の LINE 認証情報の欠落の扱い。

これまで run / retry-finalize / retry-notification / retry-stock は、LINE 通知 client を
`build_line_client_from_env()` で作っていた。認証情報が無いと ConsoleLineClient(標準出力のみ・
送信しない)へ黙ってフォールバックし、実際には LINE へ送られていないのに『送信しました』と
表示・記録され、手動の通知再試行も送られないまま完了と記録されうる(不可視の失敗)。

確認するもの:
* run: 認証情報が無く通知が必要なとき、『送信していません(認証情報が無いため)』と表示し、
  終了コードが非 0。ウォッチリストへの追加は保持される。認証情報があれば従来どおり
* retry-notification: 認証情報が無ければ、状態を変える前に失敗(終了コード 1・分かりやすいメッセージ)
* retry-finalize / retry-stock: 構築では失敗しない(deferred client)。通知が必要で欠落なら、
  終了コード非 0 と明示のメッセージ。通知が不要なら従来どおり完了
* deferred client は成功を返さず、送信の試みを記憶する
* ConsoleLineClient を暗黙に使う箇所が、この CLI に残っていない
"""

from __future__ import annotations

import ast
import contextlib
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.cli import watchlist_screening as cli_module
from jstock_advisor.infrastructure.aws.batch_tracker import WatchlistBatchStatus
from jstock_advisor.infrastructure.line.client import (
    LineCredentialsMissingError,
    LiveLineClient,
)
from tests.unit.test_watchlist_screening_cli import (
    _FakeWatchlistRepository,
    _patch_common,
    _runner,
)

_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def _no_line_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_USER_ID", raising=False)


def _set_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "token-value")
    monkeypatch.setenv("LINE_USER_ID", "user-value")


# --- deferred client の単体 ----------------------------------------------------------


def test_deferred_client_never_succeeds_and_remembers_the_attempt() -> None:
    client = cli_module._CredentialDeferredLineClient(LineCredentialsMissingError("missing"))

    assert client.send_attempted is False
    with pytest.raises(LineCredentialsMissingError):
        client.push_message("x")
    assert client.send_attempted is True


@pytest.mark.parametrize("method", ["push_message", "reply_message", "reply_messages"])
def test_deferred_client_fails_for_every_send_method(method: str) -> None:
    client = cli_module._CredentialDeferredLineClient(LineCredentialsMissingError("missing"))
    args: tuple[Any, ...] = {
        "push_message": ("text",),
        "reply_message": ("token", "text"),
        "reply_messages": ("token", ["text"]),
    }[method]

    with pytest.raises(LineCredentialsMissingError):
        getattr(client, method)(*args)
    assert client.send_attempted is True


def test_builder_returns_live_client_with_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_credentials(monkeypatch)
    assert isinstance(cli_module._build_deferred_line_client(), LiveLineClient)


def test_builder_returns_deferred_client_without_credentials_never_a_console_client() -> None:
    client = cli_module._build_deferred_line_client()
    assert isinstance(client, cli_module._CredentialDeferredLineClient)


def test_builder_does_not_swallow_other_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cli_module, "build_live_line_client_from_env", _boom)
    with pytest.raises(RuntimeError, match="unexpected"):
        cli_module._build_deferred_line_client()


def test_no_implicit_console_client_remains_in_the_cli() -> None:
    """build_line_client_from_env(ConsoleLineClient へ黙ってフォールバック)を使わない。"""
    path = Path(cli_module.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    assert "build_line_client_from_env" not in names
    assert "ConsoleLineClient" not in names


# --- run ------------------------------------------------------------------------------


class _SendingNotificationService:
    """notify が実際に line_client へ送信する fake(送信の成否が結果を決める)。"""

    last_client: Any = None

    def __init__(self, **kwargs: Any) -> None:
        self._client = kwargs["line_client"]
        _SendingNotificationService.last_client = self._client

    def notify_watchlist_additions(self, summary: Any, content_hash: str) -> bool:
        self._client.push_message("notice")
        return True


def _prepare_run(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeWatchlistRepository, list[Any]]:
    # `_patch_common` は `_build_deferred_line_client` を差し替えるため、本テストでは元へ戻す。
    _patch_common(monkeypatch)
    monkeypatch.setattr(cli_module, "_build_deferred_line_client", _REAL_BUILD_DEFERRED_LINE_CLIENT)
    repo = _FakeWatchlistRepository()
    monkeypatch.setattr(cli_module, "WatchlistRepository", lambda: repo)
    monkeypatch.setattr(cli_module, "LineNotificationService", _SendingNotificationService)
    batch_audits: list[Any] = []
    monkeypatch.setattr(cli_module, "record_candidate_audit", lambda *a, **kw: None)
    monkeypatch.setattr(cli_module, "record_batch_audit", lambda **kw: batch_audits.append(kw))
    monkeypatch.setattr(cli_module, "record_repository_result_audit", lambda *a, **kw: None)
    return repo, batch_audits


_REAL_BUILD_DEFERRED_LINE_CLIENT = cli_module._build_deferred_line_client


def test_run_without_credentials_says_not_sent_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, batch_audits = _prepare_run(monkeypatch)

    result = _runner.invoke(cli_module.app, ["run"])

    assert result.exit_code == 1, result.output
    assert "LINE通知を送信していません(LINE認証情報が無いため)" in result.output
    assert "LINE通知: 送信していません" in result.output
    assert "送信しました" not in result.output
    assert len(repo.added) == 1  # ウォッチリストへの追加は保持される
    [audit] = batch_audits
    assert audit["output_values"]["notification_sent"] is False
    assert audit["output_values"]["notification_failure"] is True
    assert isinstance(
        _SendingNotificationService.last_client, cli_module._CredentialDeferredLineClient
    )


def test_run_without_credentials_does_not_suggest_retry_notification_can_resend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runで送れなかった通知はretry-notificationの対象にならない(batchの状態遷移を行わない)。
    メッセージは『再送できる』と読める文言を含まず、実装の事実だけを書く。"""
    _prepare_run(monkeypatch)

    result = _runner.invoke(cli_module.app, ["run"])

    assert result.exit_code == 1, result.output
    assert "この通知は retry-notification では再送できません" in result.output
    assert "再送手段は" in result.output
    # run は batch の状態遷移を行わない: batch を更新・参照する関数を呼んでいない
    assert "retry-notification を実行してください" not in result.output


def test_run_with_credentials_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    repo, batch_audits = _prepare_run(monkeypatch)
    _set_credentials(monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr(LiveLineClient, "push_message", lambda self, text: sent.append(text))

    result = _runner.invoke(cli_module.app, ["run"])

    assert result.exit_code == 0, result.output
    assert "LINE通知: 送信しました" in result.output
    assert sent == ["notice"]
    assert batch_audits[0]["output_values"]["notification_sent"] is True


def test_run_does_not_use_a_line_client_when_no_notification_is_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _ = _prepare_run(monkeypatch)
    monkeypatch.setattr(
        cli_module,
        "load_config",
        lambda: _config_without_notification(),
    )
    built: list[bool] = []
    monkeypatch.setattr(
        cli_module, "_build_deferred_line_client", lambda: built.append(True) or object()
    )

    result = _runner.invoke(cli_module.app, ["run"])

    assert result.exit_code == 0, result.output
    assert built == []


def _config_without_notification() -> SimpleNamespace:
    from tests.unit.test_watchlist_screening_cli import _fake_config

    config = _fake_config()
    config.watchlist_screening.notification_enabled = False
    return config


# --- retry-notification / retry-finalize / retry-stock ---------------------------------


def _patch_retry_common(monkeypatch: pytest.MonkeyPatch, status: WatchlistBatchStatus) -> None:
    monkeypatch.setattr(
        cli_module, "get_watchlist_batch", lambda batch_id: {"status": status.value}
    )
    monkeypatch.setattr(cli_module, "load_config", lambda: SimpleNamespace())
    monkeypatch.setattr(
        cli_module, "build_real_provider_bundle", lambda now, cfg: SimpleNamespace()
    )
    monkeypatch.setattr(
        cli_module, "build_cached_provider_bundle", lambda p, c, n: SimpleNamespace()
    )
    monkeypatch.setattr(cli_module, "LineNotificationService", _SendingNotificationService)


def test_retry_notification_without_credentials_fails_before_changing_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_retry_common(monkeypatch, WatchlistBatchStatus.NOTIFICATION_FAILED)
    called: list[str] = []
    monkeypatch.setattr(
        cli_module, "retry_notification", lambda *a, **kw: called.append("retry") or True
    )

    result = _runner.invoke(cli_module.app, ["retry-notification", "b1", "--execute"])

    assert result.exit_code == 1, result.output
    assert "LINE認証情報が無いため、通知を再試行できません" in result.output
    assert "バッチの状態は変更していません" in result.output
    assert called == []  # 状態を変える処理(retry_notification)を呼んでいない
    assert "Traceback" not in result.output


def test_retry_notification_with_credentials_runs_with_the_live_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_retry_common(monkeypatch, WatchlistBatchStatus.NOTIFICATION_FAILED)
    _set_credentials(monkeypatch)
    seen: list[Any] = []

    def _retry(batch_id: str, now: Any, providers: Any, config: Any, service: Any) -> bool:
        seen.append(service._client)
        return True

    monkeypatch.setattr(cli_module, "retry_notification", _retry)

    result = _runner.invoke(cli_module.app, ["retry-notification", "b1", "--execute"])

    assert result.exit_code == 0, result.output
    assert "通知の再試行に成功しました" in result.output
    assert isinstance(seen[0], LiveLineClient)


def test_retry_notification_dry_run_does_not_require_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_retry_common(monkeypatch, WatchlistBatchStatus.NOTIFICATION_FAILED)

    result = _runner.invoke(cli_module.app, ["retry-notification", "b1"])

    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output


def _retry_finalize_runner(sends: bool) -> Any:
    def _retry(batch_id: str, now: Any, providers: Any, config: Any, service: Any) -> bool:
        if sends:
            with contextlib.suppress(LineCredentialsMissingError):
                service._client.push_message("notice")  # finalizerのPhase 3は例外を捕捉する
        return True

    return _retry


def test_retry_finalize_without_credentials_completes_when_no_notification_is_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_retry_common(monkeypatch, WatchlistBatchStatus.FINALIZE_FAILED)
    monkeypatch.setattr(cli_module, "retry_finalize", _retry_finalize_runner(sends=False))

    result = _runner.invoke(cli_module.app, ["retry-finalize", "b1", "--execute"])

    assert result.exit_code == 0, result.output  # 構築で失敗しない
    assert "finalizeの再試行に成功しました" in result.output


def test_retry_finalize_without_credentials_exits_nonzero_when_a_notification_was_attempted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_retry_common(monkeypatch, WatchlistBatchStatus.FINALIZE_FAILED)
    monkeypatch.setattr(cli_module, "retry_finalize", _retry_finalize_runner(sends=True))

    result = _runner.invoke(cli_module.app, ["retry-finalize", "b1", "--execute"])

    assert result.exit_code == 1, result.output
    assert "LINE通知は送信していません(LINE認証情報が無いため)" in result.output
    assert "NOTIFICATION_FAILED" in result.output
    assert "finalizeの再試行に成功しました" not in result.output


def _patch_retry_stock(monkeypatch: pytest.MonkeyPatch, sends: bool) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "query_all_candidate_progress",
        lambda batch_id, consistent_read=True: [
            SimpleNamespace(
                stock_code="1234", status="PENDING", attempt_count=0, evaluation_result=None
            )
        ],
    )
    monkeypatch.setattr(cli_module, "claim_candidate_lease", lambda *a, **kw: True)
    monkeypatch.setattr(cli_module, "load_config", lambda: SimpleNamespace())
    monkeypatch.setattr(
        cli_module, "build_real_provider_bundle", lambda now, cfg: SimpleNamespace()
    )
    monkeypatch.setattr(
        cli_module, "build_cached_provider_bundle", lambda p, c, n: SimpleNamespace()
    )
    outcome = SimpleNamespace(
        terminal_status="COMPLETED",
        evaluation_result="PASSED",
        ranking_entry_json=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        total_score=None,
        notification_detail=None,
    )
    monkeypatch.setattr(cli_module, "_evaluate_candidate", lambda *a, **kw: outcome)
    monkeypatch.setattr(
        cli_module, "complete_candidate", lambda *a, **kw: calls.append("completed") or True
    )
    monkeypatch.setattr(cli_module, "LineNotificationService", _SendingNotificationService)

    def _finalize(batch_id: str, now: Any, providers: Any, config: Any, service: Any) -> bool:
        calls.append("finalize")
        if sends:
            with contextlib.suppress(LineCredentialsMissingError):
                service._client.push_message("notice")  # finalizerのPhase 3は例外を捕捉する
        return True

    monkeypatch.setattr(cli_module, "maybe_finalize", _finalize)
    return calls


def test_retry_stock_without_credentials_does_not_fail_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_retry_stock(monkeypatch, sends=False)

    result = _runner.invoke(cli_module.app, ["retry-stock", "b1", "1234", "--execute"])

    assert result.exit_code == 0, result.output
    # complete_candidate の後に中途状態を作らない: 完了記録と finalize の両方に到達する
    assert calls == ["completed", "finalize"]
    assert "finalize結果: 実行しました" in result.output


def test_retry_stock_without_credentials_exits_nonzero_when_a_notification_was_attempted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_retry_stock(monkeypatch, sends=True)

    result = _runner.invoke(cli_module.app, ["retry-stock", "b1", "1234", "--execute"])

    assert result.exit_code == 1, result.output
    assert calls == ["completed", "finalize"]
    assert "finalize結果: 実行しました" in result.output
    assert "LINE通知は送信していません(LINE認証情報が無いため)" in result.output
