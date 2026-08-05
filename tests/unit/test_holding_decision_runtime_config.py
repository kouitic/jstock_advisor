"""RuntimeConfigの初回作成・楽観ロック・フォールバックのテスト(実装プラン1節)。"""

from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import FinancialPolicyOverride, RuntimeConfigMode
from jstock_advisor.infrastructure.local_repository import (
    holding_decision_runtime_config_repository as runtime_config_repo,
)
from jstock_advisor.services.holding_decision_runtime_config_service import (
    FALLBACK_RUNTIME_CONFIG_VERSION,
    HoldingDecisionRuntimeConfigService,
    RuntimeConfigAlreadyInitializedError,
)


def _reset_cache() -> None:
    import jstock_advisor.services.holding_decision_runtime_config_service as mod

    mod._cached_config = None
    mod._cached_at = None


def test_before_init_returns_safe_fallback(store_dir: Path):
    _reset_cache()
    service = HoldingDecisionRuntimeConfigService(store_dir=store_dir)
    lookup = service.get_config()
    assert lookup.is_fallback is True
    assert lookup.config.mode == RuntimeConfigMode.LEGACY
    assert lookup.config.notification_enabled is False
    assert lookup.config.financial_policy_override == FinancialPolicyOverride.FORCE_DEFER_ALL
    assert lookup.effective_runtime_config_version == FALLBACK_RUNTIME_CONFIG_VERSION


def test_init_then_conflict_on_second_init(store_dir: Path):
    _reset_cache()
    service = HoldingDecisionRuntimeConfigService(store_dir=store_dir)
    created = service.init_config(updated_by="tester")
    assert created.config_version == 1
    with pytest.raises(RuntimeConfigAlreadyInitializedError):
        service.init_config(updated_by="tester")


def test_update_increments_version_and_conflict_detection(store_dir: Path):
    _reset_cache()
    service = HoldingDecisionRuntimeConfigService(store_dir=store_dir)
    service.init_config(updated_by="tester", mode=RuntimeConfigMode.LEGACY)

    updated = service.update_config(
        expected_config_version=1,
        mode=RuntimeConfigMode.ACTIVE,
        notification_enabled=True,
        financial_policy_override=FinancialPolicyOverride.DEFAULT,
        updated_by="tester",
        change_reason="go active",
    )
    assert updated.config_version == 2
    assert updated.mode == RuntimeConfigMode.ACTIVE

    with pytest.raises(runtime_config_repo.RuntimeConfigConflictError):
        service.update_config(
            expected_config_version=1,  # stale
            mode=RuntimeConfigMode.LEGACY,
            notification_enabled=False,
            financial_policy_override=FinancialPolicyOverride.DEFAULT,
            updated_by="tester",
            change_reason="stale update",
        )


def test_lost_update_is_prevented_between_two_operators(store_dir: Path):
    """運用者Aのmode変更と運用者Bのnotification_enabled変更が同時に行われた場合、
    片方だけが成功し、もう片方の変更が消える(lost update)ことがない。"""
    _reset_cache()
    service = HoldingDecisionRuntimeConfigService(store_dir=store_dir)
    service.init_config(
        updated_by="tester", mode=RuntimeConfigMode.LEGACY, notification_enabled=True
    )

    current = service.get_config()
    expected_version = current.config.config_version

    # 運用者A: modeを変更
    a_result = service.update_config(
        expected_config_version=expected_version,
        mode=RuntimeConfigMode.SHADOW,
        notification_enabled=current.config.notification_enabled,
        financial_policy_override=current.config.financial_policy_override,
        updated_by="operator_a",
        change_reason="A's change",
    )
    assert a_result.mode == RuntimeConfigMode.SHADOW
    assert a_result.notification_enabled is True  # Aが読んだ時点の値を維持

    # 運用者B: 古いバージョンのままnotification_enabledを変更しようとすると失敗する
    with pytest.raises(runtime_config_repo.RuntimeConfigConflictError):
        service.update_config(
            expected_config_version=expected_version,  # 古いバージョン(Aの変更前)
            mode=current.config.mode,
            notification_enabled=False,
            financial_policy_override=current.config.financial_policy_override,
            updated_by="operator_b",
            change_reason="B's change",
        )
    # Bは最新値を再取得してから改めて更新する必要がある
    latest = service.get_config()
    b_result = service.update_config(
        expected_config_version=latest.config.config_version,
        mode=latest.config.mode,  # Aの変更(SHADOW)を引き継ぐ
        notification_enabled=False,
        financial_policy_override=latest.config.financial_policy_override,
        updated_by="operator_b",
        change_reason="B's change (retry)",
    )
    assert b_result.mode == RuntimeConfigMode.SHADOW  # Aの変更が失われていない
    assert b_result.notification_enabled is False  # Bの変更も反映されている


def test_get_notification_enabled_bypasses_ttl_cache(store_dir: Path):
    """kill switch(notification_enabled)は緊急停止用途のため、get_config()の
    TTLキャッシュを経由せず常に最新値を返す(実装プラン修正2)。"""
    _reset_cache()
    service = HoldingDecisionRuntimeConfigService(cache_ttl_seconds=3600, store_dir=store_dir)
    service.init_config(
        updated_by="tester", mode=RuntimeConfigMode.ACTIVE, notification_enabled=True
    )

    # get_config()を呼び、mode等をキャッシュへ載せる(TTLが長いため通常は再取得しない)。
    cached = service.get_config()
    assert cached.config.notification_enabled is True

    # 別プロセス相当の直接更新(kill switch ON = notification_enabled False)。
    from jstock_advisor.infrastructure.local_repository import (
        holding_decision_runtime_config_repository as repo,
    )

    repo.update(
        expected_config_version=1,
        mode=RuntimeConfigMode.ACTIVE,
        notification_enabled=False,
        financial_policy_override=FinancialPolicyOverride.DEFAULT,
        updated_by="operator",
        change_reason="emergency stop",
        store_dir=store_dir,
    )

    # get_config()はTTL内なのでまだ古い値(True)を返す。
    stale = service.get_config()
    assert stale.config.notification_enabled is True

    # get_notification_enabled()はキャッシュを経由しないため、即座に最新値(False)を返す。
    assert service.get_notification_enabled() is False


def test_get_notification_enabled_falls_back_to_false_when_unconfigured(store_dir: Path):
    """RuntimeConfig未初期化時、kill switchは安全側(通知しない=False)へ
    フォールバックする。"""
    _reset_cache()
    service = HoldingDecisionRuntimeConfigService(store_dir=store_dir)
    assert service.get_notification_enabled() is False


def test_cache_is_used_within_ttl(store_dir: Path):
    _reset_cache()
    service = HoldingDecisionRuntimeConfigService(cache_ttl_seconds=3600, store_dir=store_dir)
    service.init_config(updated_by="tester")
    first = service.get_config()
    assert first.is_fallback is False

    # 実際に更新してもキャッシュ有効期間中は古い値が返る(直接リポジトリを操作)
    from jstock_advisor.infrastructure.local_repository import (
        holding_decision_runtime_config_repository as repo,
    )

    repo.update(
        expected_config_version=1,
        mode=RuntimeConfigMode.ACTIVE,
        notification_enabled=True,
        financial_policy_override=FinancialPolicyOverride.DEFAULT,
        updated_by="tester",
        change_reason="direct update bypassing service cache",
        store_dir=store_dir,
    )
    cached = service.get_config()
    assert cached.config.mode == RuntimeConfigMode.LEGACY  # まだキャッシュされた値
