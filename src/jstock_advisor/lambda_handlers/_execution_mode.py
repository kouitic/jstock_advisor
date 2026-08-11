"""通知検証モード機能(2026-08追加)。Lambdaイベントのexecution_modeキーを解析する。"""

from __future__ import annotations

from typing import Any

from jstock_advisor.domain.entities.enums import ExecutionMode
from jstock_advisor.domain.entities.execution_context import ExecutionContext


def resolve_execution_context(event: dict[str, Any]) -> ExecutionContext:
    """未指定はNORMAL。不正な値はNORMALへフォールバックせず例外を送出する。"""
    raw = event.get("execution_mode")
    if raw is None:
        return ExecutionContext.normal()
    try:
        return ExecutionContext(mode=ExecutionMode(raw))
    except ValueError:
        raise ValueError(f"unknown execution_mode: {raw!r}") from None
