"""Issue #117 Phase B1b-4d: weekly_review_handlerのLINE client構築(認証情報)のテスト。

weekly_review_handlerにはこれまでhandler自体のテストが無かった。認証情報の欠落は、集計・
メトリクス保存・候補検出(`service.run`)の**前**に失敗させる(fail-early)。送信時に失敗させる方式だと、
候補を保存した後に失敗し、再実行時は「既存候補」(is_newが立たない)となって通知が永久に失われうる。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.infrastructure.line.client import (
    LineCredentialsMissingError,
    LiveLineClient,
)
from jstock_advisor.lambda_handlers import weekly_review_handler as handler_module


class _RecordingService:
    """WeeklyImprovementReviewServiceの差し替え。渡されたclientと、runの呼び出しを記録する。"""

    instances: list[_RecordingService] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.run_calls = 0
        type(self).instances.append(self)

    def run(self, now: Any) -> SimpleNamespace:
        self.run_calls += 1
        return SimpleNamespace(
            review_week="2026-W36",
            total_evaluation_results=10,
            joined_count=9,
            candidates_detected=2,
            issue_eligible_candidates=1,
            github_statuses={"CREATED": 1},
            notified_new_issue_count=1,
        )


@pytest.fixture(autouse=True)
def _stub_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingService.instances = []
    monkeypatch.setattr(handler_module, "load_config", lambda: object())
    monkeypatch.setattr(handler_module, "WeeklyImprovementReviewService", _RecordingService)


def test_missing_credentials_fail_before_the_review_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """構築関数を差し替えない実分岐。認証情報が無ければConsoleLineClientへ黙って落ちず、
    集計・メトリクス保存・候補検出(run)の前にLineCredentialsMissingErrorで失敗する。"""
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_USER_ID", raising=False)

    with pytest.raises(LineCredentialsMissingError):
        handler_module.handler({}, object())

    assert all(service.run_calls == 0 for service in _RecordingService.instances)
    assert _RecordingService.instances == []  # serviceの構築にも到達しない


@pytest.mark.parametrize(
    ("token", "user_id"), [("test-token", None), (None, "test-user"), ("", "test-user")]
)
def test_one_sided_or_empty_credentials_also_fail(
    monkeypatch: pytest.MonkeyPatch, token: str | None, user_id: str | None
) -> None:
    for key, value in (("LINE_CHANNEL_ACCESS_TOKEN", token), ("LINE_USER_ID", user_id)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    with pytest.raises(LineCredentialsMissingError):
        handler_module.handler({}, object())


def test_present_credentials_pass_a_live_client_to_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("LINE_USER_ID", "test-user")

    handler_module.handler({}, object())

    (service,) = _RecordingService.instances
    assert isinstance(service.kwargs["line_client"], LiveLineClient)
    assert service.run_calls == 1


def test_handler_returns_the_review_outcome_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """認証情報が正常な運用の返り値は不変(既存の契約)。"""
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("LINE_USER_ID", "test-user")

    result = handler_module.handler({}, object())

    assert result == {
        "review_week": "2026-W36",
        "total_evaluation_results": 10,
        "joined_count": 9,
        "candidates_detected": 2,
        "issue_eligible_candidates": 1,
        "notified_new_issue_count": 1,
    }


def test_github_repository_is_still_split_and_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    """GITHUB_REPOSITORY("owner/repo")の分割は従来どおり(client構築の変更で壊れていない)。"""
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("LINE_USER_ID", "test-user")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example-owner/example-repo")

    handler_module.handler({}, object())

    (service,) = _RecordingService.instances
    assert service.kwargs["github_repo_owner"] == "example-owner"
    assert service.kwargs["github_repo_name"] == "example-repo"
