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
