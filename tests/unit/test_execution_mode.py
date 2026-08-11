import pytest

from jstock_advisor.domain.entities.enums import ExecutionMode
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
