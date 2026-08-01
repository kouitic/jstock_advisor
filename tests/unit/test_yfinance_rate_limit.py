"""候補ユニバース本格対応(5節、案B)の429再試行ヘルパーのテスト。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jstock_advisor.services.yfinance_rate_limit import call_with_rate_limit_retry


def test_call_with_rate_limit_retry_returns_value_on_immediate_success() -> None:
    result = call_with_rate_limit_retry(lambda: 42)
    assert result.value == 42
    assert result.is_rate_limit_suspected is False
    assert result.error is None


def test_call_with_rate_limit_retry_reraises_non_rate_limit_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("time.sleep", lambda _: None)

    def _raise() -> int:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        call_with_rate_limit_retry(_raise)


def test_call_with_rate_limit_retry_retries_on_429_status_code_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("time.sleep", lambda _: None)
    attempts = {"count": 0}

    def _flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            exc = Exception("rate limited")
            exc.response = SimpleNamespace(status_code=429, headers={})  # type: ignore[attr-defined]
            raise exc
        return "ok"

    result = call_with_rate_limit_retry(_flaky)
    assert result.value == "ok"
    assert attempts["count"] == 3


def test_call_with_rate_limit_retry_detects_429_from_message_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("time.sleep", lambda _: None)

    def _raise() -> int:
        raise Exception("HTTP Error: Too Many Requests")

    result = call_with_rate_limit_retry(_raise)
    assert result.is_rate_limit_suspected is True
    assert result.value is None
    assert result.error is not None


def test_call_with_rate_limit_retry_exhausts_retries_and_reports_suspected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("time.sleep", lambda _: None)
    attempts = {"count": 0}

    def _always_429() -> int:
        attempts["count"] += 1
        raise Exception("429 Client Error")

    result = call_with_rate_limit_retry(_always_429)
    assert result.value is None
    assert result.is_rate_limit_suspected is True
    assert attempts["count"] == 4  # 初回+最大3回再試行


def test_call_with_rate_limit_retry_respects_retry_after_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))
    attempts = {"count": 0}

    def _flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] < 2:
            exc = Exception("429")
            exc.response = SimpleNamespace(  # type: ignore[attr-defined]
                status_code=429, headers={"Retry-After": "7"}
            )
            raise exc
        return "ok"

    result = call_with_rate_limit_retry(_flaky)
    assert result.value == "ok"
    assert sleeps == [7.0]
