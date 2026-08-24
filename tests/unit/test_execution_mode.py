import pytest

from jstock_advisor.domain.entities.enums import ExecutionMode, NotificationMode
from jstock_advisor.lambda_handlers._execution_mode import resolve_execution_context


def test_resolve_execution_context_defaults_to_normal_when_unspecified() -> None:
    context = resolve_execution_context({})
    assert context.mode == ExecutionMode.NORMAL
    assert context.is_validation is False


def test_resolve_execution_context_normal_explicit() -> None:
    context = resolve_execution_context({"execution_mode": "NORMAL"})
    assert context.mode == ExecutionMode.NORMAL
    assert context.is_validation is False


def test_resolve_execution_context_validation() -> None:
    context = resolve_execution_context({"execution_mode": "VALIDATION"})
    assert context.mode == ExecutionMode.VALIDATION
    assert context.is_validation is True


def test_resolve_execution_context_invalid_value_raises() -> None:
    with pytest.raises(ValueError, match="unknown execution_mode"):
        resolve_execution_context({"execution_mode": "BOGUS"})


def test_resolve_execution_context_does_not_fall_back_to_normal_on_invalid_value() -> None:
    """不正値をNORMALへフォールバックしてはいけない(要求仕様)。"""
    with pytest.raises(ValueError):
        resolve_execution_context({"execution_mode": "normal"})  # 大文字小文字違いも不正値扱い


# --- 通知ドライラン機能(2026-08追加): notification_mode ---------------------


def test_resolve_execution_context_normal_without_notification_mode_is_unchanged() -> None:
    """NORMAL+notification_mode未指定は既存動作どおり(SEND相当の既定値のまま)。"""
    context = resolve_execution_context({"execution_mode": "NORMAL"})
    assert context.mode == ExecutionMode.NORMAL
    assert context.notification_mode == NotificationMode.SEND
    assert context.is_dry_run is False


def test_resolve_execution_context_validation_without_notification_mode_is_send() -> None:
    context = resolve_execution_context({"execution_mode": "VALIDATION"})
    assert context.notification_mode == NotificationMode.SEND
    assert context.is_dry_run is False


def test_resolve_execution_context_validation_explicit_send() -> None:
    context = resolve_execution_context(
        {"execution_mode": "VALIDATION", "notification_mode": "SEND"}
    )
    assert context.notification_mode == NotificationMode.SEND
    assert context.is_dry_run is False


def test_resolve_execution_context_validation_dry_run() -> None:
    context = resolve_execution_context(
        {"execution_mode": "VALIDATION", "notification_mode": "DRY_RUN"}
    )
    assert context.mode == ExecutionMode.VALIDATION
    assert context.notification_mode == NotificationMode.DRY_RUN
    assert context.is_dry_run is True


def test_resolve_execution_context_normal_with_notification_mode_send_raises() -> None:
    """notification_modeはVALIDATION専用の補助設定。NORMAL+SENDの明示指定も
    黙って無視せず明確にエラーとする。"""
    with pytest.raises(ValueError, match="notification_mode requires execution_mode=VALIDATION"):
        resolve_execution_context({"execution_mode": "NORMAL", "notification_mode": "SEND"})


def test_resolve_execution_context_normal_with_notification_mode_dry_run_raises() -> None:
    with pytest.raises(ValueError, match="notification_mode requires execution_mode=VALIDATION"):
        resolve_execution_context({"execution_mode": "NORMAL", "notification_mode": "DRY_RUN"})


def test_resolve_execution_context_notification_mode_without_execution_mode_raises() -> None:
    """execution_mode未指定(NORMAL相当)でnotification_modeだけ指定した場合も
    エラー。"""
    with pytest.raises(ValueError, match="notification_mode requires execution_mode=VALIDATION"):
        resolve_execution_context({"notification_mode": "DRY_RUN"})


def test_resolve_execution_context_invalid_notification_mode_value_raises() -> None:
    with pytest.raises(ValueError, match="unknown notification_mode"):
        resolve_execution_context({"execution_mode": "VALIDATION", "notification_mode": "BOGUS"})


def test_resolve_execution_context_invalid_notification_mode_does_not_fall_back_to_send() -> None:
    """不正なnotification_mode値をSENDへ黙ってフォールバックしてはいけない。"""
    with pytest.raises(ValueError):
        resolve_execution_context({"execution_mode": "VALIDATION", "notification_mode": "send"})
