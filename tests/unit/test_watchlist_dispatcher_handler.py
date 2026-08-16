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


def _fake_config(
    *, candidate_limit: int | None, rotation_enabled: bool = True
) -> SimpleNamespace:
    watchlist_screening = SimpleNamespace(
        enabled=True,
        weekly_schedule_enabled=True,
        candidate_universe=SimpleNamespace(provider="csv"),
        screening_policy="high_dividend_financial_health",
        staged_rollout=SimpleNamespace(candidate_limit=candidate_limit, market_segment_filter=None),
        batch_record_ttl_hours=72,
        rotation=SimpleNamespace(enabled=rotation_enabled),
        batch_processing_timeout_hours=24,
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


# --- 本番検証2026-08対応: rotation window二重dispatch防止leaseのテスト -----------


def test_new_candidate_screening_skips_when_rotation_lease_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NEW_CANDIDATE_SCREENING + rotation.enabled=trueで、rotation dispatch
    leaseが取得できない場合、候補選択・SQS投入を一切行わずskipすること。"""
    monkeypatch.delenv("ALLOW_FULL_MARKET_SCREENING", raising=False)
    monkeypatch.setattr(
        handler_module,
        "load_config",
        lambda: _fake_config(candidate_limit=300, rotation_enabled=True),
    )
    monkeypatch.setattr(handler_module, "try_acquire_dispatch_lease", lambda *a, **kw: True)

    audit_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handler_module, "record_batch_audit", lambda **kw: audit_calls.append(kw)
    )

    lease_calls: list[tuple[Any, ...]] = []

    def _fake_try_acquire_rotation_lease(*args: Any, **kwargs: Any) -> bool:
        lease_calls.append(args)
        return False

    monkeypatch.setattr(
        handler_module,
        "try_acquire_rotation_dispatch_lease",
        _fake_try_acquire_rotation_lease,
    )
    monkeypatch.setattr(
        handler_module,
        "get_rotation_dispatch_lease_status",
        lambda *a, **kw: (
            "watchlist-20260815T121027-9dd0e8c7",
            "2026-08-15T12:10:27",
            "2026-08-16T12:10:27",
        ),
    )

    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("candidate selection must not run when the rotation lease is unavailable")

    monkeypatch.setattr(handler_module, "_collect_new_candidate_targets", _fail_if_called)
    monkeypatch.setattr(handler_module, "set_watchlist_batch_total", _fail_if_called)

    result = handler_module.handler({}, object())

    assert len(lease_calls) == 1
    assert result == {"skipped": "rotation_dispatch_in_progress"}
    assert len(audit_calls) == 1
    output_values = audit_calls[0]["output_values"]
    assert output_values["execution_result"] == "rotation_dispatch_already_in_progress"
    assert output_values["block_reason"] == "ROTATION_DISPATCH_ALREADY_IN_PROGRESS"
    assert output_values["active_batch_id"] == "watchlist-20260815T121027-9dd0e8c7"
    assert output_values["rotation_id"] == "default"


def test_watchlist_maintenance_is_not_blocked_by_rotation_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WATCHLIST_MAINTENANCEはNEW_CANDIDATE_SCREENING用のrotation dispatch
    leaseの対象外(job_type分岐でtry_acquire_rotation_dispatch_lease自体が
    呼ばれないこと)。"""
    monkeypatch.delenv("ALLOW_FULL_MARKET_SCREENING", raising=False)
    monkeypatch.setattr(
        handler_module,
        "load_config",
        lambda: _fake_config(candidate_limit=300, rotation_enabled=True),
    )
    monkeypatch.setattr(handler_module, "try_acquire_dispatch_lease", lambda *a, **kw: True)
    monkeypatch.setattr(handler_module, "record_batch_audit", lambda **kw: None)

    def _fail_if_called(*args: Any, **kwargs: Any) -> bool:
        pytest.fail("rotation dispatch lease must not be touched for WATCHLIST_MAINTENANCE")

    monkeypatch.setattr(
        handler_module, "try_acquire_rotation_dispatch_lease", _fail_if_called
    )
    monkeypatch.setattr(
        handler_module, "_collect_maintenance_targets", lambda event: ([], {})
    )

    result = handler_module.handler({"job_type": "WATCHLIST_MAINTENANCE"}, object())

    assert result == {"dispatched": 0}


def test_new_candidate_screening_not_gated_when_rotation_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rotation.enabled=false(固定スライスへのフォールバック)では、rotation
    dispatch leaseを取得しようとしないこと(候補選択がrotation windowに
    依存しないため対象外)。"""
    monkeypatch.delenv("ALLOW_FULL_MARKET_SCREENING", raising=False)
    monkeypatch.setattr(
        handler_module,
        "load_config",
        lambda: _fake_config(candidate_limit=300, rotation_enabled=False),
    )
    monkeypatch.setattr(handler_module, "try_acquire_dispatch_lease", lambda *a, **kw: True)
    monkeypatch.setattr(handler_module, "record_batch_audit", lambda **kw: None)

    def _fail_if_called(*args: Any, **kwargs: Any) -> bool:
        pytest.fail("rotation dispatch lease must not be touched when rotation.enabled=false")

    monkeypatch.setattr(
        handler_module, "try_acquire_rotation_dispatch_lease", _fail_if_called
    )
    monkeypatch.setattr(
        handler_module, "_collect_new_candidate_targets", lambda *a, **kw: ([], {})
    )

    result = handler_module.handler({}, object())

    assert result == {"dispatched": 0}


# --- 平日毎日起動化(2026-08)対応: WATCHLIST_MAINTENANCE後続起動 ------------------


def test_batch_id_override_from_event_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """maybe_trigger_maintenanceが決定論的に算出したbatch_idをevent["batch_id"]
    経由で渡した場合、Dispatcherは自動生成せずそれをそのまま使うこと(テスト#9の
    前提、および二重起動防止の二重の安全策)。"""
    monkeypatch.delenv("ALLOW_FULL_MARKET_SCREENING", raising=False)
    monkeypatch.setattr(
        handler_module,
        "load_config",
        lambda: _fake_config(candidate_limit=300, rotation_enabled=True),
    )
    captured_batch_ids: list[str] = []

    def _capture_and_reject(batch_id: str, *_a: Any, **_kw: Any) -> bool:
        captured_batch_ids.append(batch_id)
        return False

    monkeypatch.setattr(handler_module, "try_acquire_dispatch_lease", _capture_and_reject)

    result = handler_module.handler(
        {
            "job_type": "WATCHLIST_MAINTENANCE",
            "batch_id": "watchlist-maint-batch-1",
            "triggered_by_batch_id": "batch-1",
            "trigger_type": "POST_NEW_CANDIDATE_SCREENING",
        },
        object(),
    )

    assert captured_batch_ids == ["watchlist-maint-batch-1"]
    assert result == {"skipped": "lease_not_acquired"}


def test_collect_maintenance_targets_propagates_trigger_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """テスト#9: triggered_by_batch_id/trigger_typeがchild batchのextra_kwargs
    (最終的にset_watchlist_batch_totalへ渡る)へ正しく引き継がれること。"""
    fake_item = SimpleNamespace(
        stock_code="1301",
        registration_source=handler_module.WatchlistRegistrationSource.AUTO_SCREENING,
    )
    monkeypatch.setattr(
        handler_module,
        "WatchlistRepository",
        lambda: SimpleNamespace(list_all=lambda: [fake_item]),
    )

    codes, extra_kwargs = handler_module._collect_maintenance_targets(
        {
            "job_type": "WATCHLIST_MAINTENANCE",
            "triggered_by_batch_id": "batch-1",
            "trigger_type": "POST_NEW_CANDIDATE_SCREENING",
        }
    )

    assert codes == ["1301"]
    assert extra_kwargs == {
        "triggered_by_batch_id": "batch-1",
        "trigger_type": "POST_NEW_CANDIDATE_SCREENING",
    }


def test_collect_maintenance_targets_omits_trigger_metadata_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """triggered_by_batch_idが無いevent(手動CLI等)ではextra_kwargsへ何も
    追加しないこと(既存の後方互換動作)。"""
    monkeypatch.setattr(
        handler_module, "WatchlistRepository", lambda: SimpleNamespace(list_all=lambda: [])
    )

    codes, extra_kwargs = handler_module._collect_maintenance_targets(
        {"job_type": "WATCHLIST_MAINTENANCE"}
    )

    assert codes == []
    assert extra_kwargs == {}
