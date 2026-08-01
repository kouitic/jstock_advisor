"""運用ハードニング2節: ALLOW_FULL_MARKET_SCREENINGガードのテスト。

candidate_limit=null(全件処理)の週次バッチが、運用者が明示的に
ALLOW_FULL_MARKET_SCREENING=trueを設定しない限り開始しないことを確認する。
dispatch lease取得より前にガードが働くため、try_acquire_dispatch_leaseが
一切呼ばれないことも合わせて確認する(SQS投入・LINE通知が発生しないことの
間接的な確認)。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.lambda_handlers import watchlist_dispatcher_handler as handler_module


def _fake_config(*, candidate_limit: int | None) -> SimpleNamespace:
    watchlist_screening = SimpleNamespace(
        enabled=True,
        weekly_schedule_enabled=True,
        candidate_universe=SimpleNamespace(provider="csv"),
        screening_policy="high_dividend_financial_health",
        staged_rollout=SimpleNamespace(candidate_limit=candidate_limit, market_segment_filter=None),
        batch_record_ttl_hours=72,
    )
    return SimpleNamespace(watchlist_screening=watchlist_screening)


def _fail_if_called(*args: Any, **kwargs: Any) -> bool:
    pytest.fail("try_acquire_dispatch_lease should not be called when the guard blocks startup")


def test_full_market_screening_blocked_when_env_var_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALLOW_FULL_MARKET_SCREENING", raising=False)
    monkeypatch.setattr(
        handler_module, "load_config", lambda: _fake_config(candidate_limit=None)
    )
    audit_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handler_module, "record_batch_audit", lambda **kw: audit_calls.append(kw)
    )
    monkeypatch.setattr(handler_module, "try_acquire_dispatch_lease", _fail_if_called)

    result = handler_module.handler({}, object())

    assert result == {"error": "full_market_screening_blocked"}
    assert len(audit_calls) == 1
    assert audit_calls[0]["output_values"]["execution_result"] == "full_market_screening_blocked"


def test_full_market_screening_blocked_when_env_var_is_not_exactly_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"1"・"True"等の紛らわしい値では許可しない(文字列"true"の完全一致のみ許可)。"""
    monkeypatch.setenv("ALLOW_FULL_MARKET_SCREENING", "1")
    monkeypatch.setattr(
        handler_module, "load_config", lambda: _fake_config(candidate_limit=None)
    )
    monkeypatch.setattr(handler_module, "record_batch_audit", lambda **kw: None)
    monkeypatch.setattr(handler_module, "try_acquire_dispatch_lease", _fail_if_called)

    result = handler_module.handler({}, object())

    assert result == {"error": "full_market_screening_blocked"}


def test_full_market_screening_allowed_when_env_var_is_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_FULL_MARKET_SCREENING", "true")
    monkeypatch.setattr(
        handler_module, "load_config", lambda: _fake_config(candidate_limit=None)
    )
    monkeypatch.setattr(handler_module, "record_batch_audit", lambda **kw: None)
    # ガードを通過した先はdispatch lease取得(以降は別テストの範囲)。
    # ここではリース取得が実際に呼ばれたことのみ確認し、Falseを返して早期終了させる。
    lease_calls: list[tuple[Any, ...]] = []

    def _fake_try_acquire_dispatch_lease(*args: Any, **kwargs: Any) -> bool:
        lease_calls.append(args)
        return False

    monkeypatch.setattr(
        handler_module, "try_acquire_dispatch_lease", _fake_try_acquire_dispatch_lease
    )

    result = handler_module.handler({}, object())

    assert len(lease_calls) == 1  # ガードを通過してdispatch lease取得まで進んだ
    assert result == {"skipped": "lease_not_acquired"}


def test_full_market_screening_guard_does_not_apply_when_candidate_limit_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """段階導入中(candidate_limitが数値)は、ALLOW_FULL_MARKET_SCREENING未設定でも
    ブロックされないこと。"""
    monkeypatch.delenv("ALLOW_FULL_MARKET_SCREENING", raising=False)
    monkeypatch.setattr(
        handler_module, "load_config", lambda: _fake_config(candidate_limit=100)
    )
    monkeypatch.setattr(handler_module, "record_batch_audit", lambda **kw: None)
    lease_calls: list[tuple[Any, ...]] = []

    def _fake_try_acquire_dispatch_lease(*args: Any, **kwargs: Any) -> bool:
        lease_calls.append(args)
        return False

    monkeypatch.setattr(
        handler_module, "try_acquire_dispatch_lease", _fake_try_acquire_dispatch_lease
    )

    result = handler_module.handler({}, object())

    assert len(lease_calls) == 1
    assert result == {"skipped": "lease_not_acquired"}
