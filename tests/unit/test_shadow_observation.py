"""domain/shadow_observation.pyの単体テスト(Issue #384 PR-1)。

buy_signal_service.pyから共有部品として抽出したisolated_shadow_observation()
自体の契約(失敗の隔離・0.0/空への偽装なし)を、呼び出し元サービスに依存せず
直接固定する。既存の統合テスト(tests/unit/test_buy_signal_service.py)は
buy_signal_service経由の振る舞い不変を引き続き固定する。
"""

from __future__ import annotations

import logging

import pytest

from jstock_advisor.domain.shadow_observation import (
    SHADOW_STATE_COMPUTATION_FAILED,
    isolated_shadow_observation,
)


def test_success_returns_build_result_unchanged() -> None:
    result = isolated_shadow_observation("demo", lambda: {"shadow_state": "COMPUTED", "score": 42})
    assert result == {"shadow_state": "COMPUTED", "score": 42}


def test_failure_is_isolated_and_recorded_not_swallowed() -> None:
    def _boom() -> dict[str, object]:
        raise ValueError("injected failure")

    result = isolated_shadow_observation("demo", _boom)

    assert result["shadow_state"] == SHADOW_STATE_COMPUTATION_FAILED
    assert result["error_type"] == "ValueError"


def test_failure_does_not_masquerade_as_zero_or_empty() -> None:
    """失敗時に0.0・空dict・空値へ偽装しない。戻り値はshadow_state/error_type
    のみであり、呼び出し元が期待する他のキー(score・facts等)を含まない。
    """

    def _boom() -> dict[str, object]:
        raise RuntimeError("injected failure")

    result = isolated_shadow_observation("demo", _boom)

    assert "score" not in result
    assert "styles" not in result
    assert set(result.keys()) == {"shadow_state", "error_type"}


def test_failure_does_not_propagate_to_caller() -> None:
    """build()の例外はisolated_shadow_observation()の外へ伝播しない
    (v1判定経路を止めないという#22 C2 / #371の隔離契約)。
    """

    def _boom() -> dict[str, object]:
        raise KeyError("injected failure")

    result = isolated_shadow_observation("demo", _boom)  # 例外を送出しないことの確認
    assert result["shadow_state"] == SHADOW_STATE_COMPUTATION_FAILED


def test_failure_is_logged_as_warning(caplog: pytest.LogCaptureFixture) -> None:
    def _boom() -> dict[str, object]:
        raise ValueError("injected failure")

    with caplog.at_level(logging.WARNING):
        isolated_shadow_observation("demo_observation", _boom)

    assert any(
        record.levelno == logging.WARNING and "demo_observation" in record.getMessage()
        for record in caplog.records
    )
