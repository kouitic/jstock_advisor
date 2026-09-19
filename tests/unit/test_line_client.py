"""infrastructure/line/client.pyのLINE client構築関数のテスト(Issue #117 Phase B1a)。

レビュー(#117事前確認)で判明したとおり、build_line_client_from_env()を参照する
既存テスト12箇所(6ファイル)はいずれも同関数をmonkeypatchで丸ごと差し替えるか、
モジュールに存在しないことをassertするのみで、実際のフォールバック分岐を検証する
テストが1件も無かった。本ファイルはその欠落を埋め、あわせて新規追加した
build_live_line_client_from_env()(Lambda handler専用の非フォールバック版)の
挙動を固定する。
"""

from __future__ import annotations

import pytest

from jstock_advisor.infrastructure.line.client import (
    ConsoleLineClient,
    LineCredentialsMissingError,
    LiveLineClient,
    build_line_client_for_run,
    build_line_client_from_env,
    build_live_line_client_from_env,
)


def test_build_line_client_from_env_returns_console_client_when_credentials_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_USER_ID", raising=False)

    client = build_line_client_from_env()

    assert isinstance(client, ConsoleLineClient)


def test_build_line_client_from_env_returns_live_client_when_credentials_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "token-value")
    monkeypatch.setenv("LINE_USER_ID", "user-value")

    client = build_line_client_from_env()

    assert isinstance(client, LiveLineClient)


def test_build_line_client_from_env_returns_console_client_when_only_token_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """レビュー指摘F1対応: token/user_idの片側だけ揃っている中間ケース。

    両方欠落・両方あり、の両端だけでなく、`and`判定が`or`へ後退した場合に
    LiveLineClient(user_id=None)のような不完全なclientを黙って返さないことを
    固定する。
    """
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "token-value")
    monkeypatch.delenv("LINE_USER_ID", raising=False)

    client = build_line_client_from_env()

    assert isinstance(client, ConsoleLineClient)


def test_build_line_client_from_env_returns_console_client_when_only_user_id_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("LINE_USER_ID", "user-value")

    client = build_line_client_from_env()

    assert isinstance(client, ConsoleLineClient)


def test_build_live_line_client_from_env_raises_when_both_credentials_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_USER_ID", raising=False)

    with pytest.raises(LineCredentialsMissingError):
        build_live_line_client_from_env()


def test_build_live_line_client_from_env_raises_when_only_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("LINE_USER_ID", "user-value")

    with pytest.raises(LineCredentialsMissingError):
        build_live_line_client_from_env()


def test_build_live_line_client_from_env_raises_when_only_user_id_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "token-value")
    monkeypatch.delenv("LINE_USER_ID", raising=False)

    with pytest.raises(LineCredentialsMissingError):
        build_live_line_client_from_env()


def test_build_live_line_client_from_env_returns_live_client_when_credentials_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "token-value")
    monkeypatch.setenv("LINE_USER_ID", "user-value")

    client = build_live_line_client_from_env()

    assert isinstance(client, LiveLineClient)


def test_build_line_client_for_run_dry_run_falls_back_to_console_when_credentials_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DRY_RUN(外部送信なし)は認証情報が無くても検証できる。"""
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_USER_ID", raising=False)

    assert isinstance(build_line_client_for_run(dry_run=True), ConsoleLineClient)


def test_build_line_client_for_run_dry_run_uses_live_client_when_credentials_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "token-value")
    monkeypatch.setenv("LINE_USER_ID", "user-value")

    assert isinstance(build_line_client_for_run(dry_run=True), LiveLineClient)


def test_build_line_client_for_run_sending_run_raises_when_credentials_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NORMAL / VALIDATION+SEND(外部送信が起きうる実行)は、欠落を黙って通さない。"""
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_USER_ID", raising=False)

    with pytest.raises(LineCredentialsMissingError):
        build_line_client_for_run(dry_run=False)


def test_build_line_client_for_run_sending_run_uses_live_client_when_credentials_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "token-value")
    monkeypatch.setenv("LINE_USER_ID", "user-value")

    assert isinstance(build_line_client_for_run(dry_run=False), LiveLineClient)
