"""通知検証モード機能(2026-08追加)。Lambdaイベントのexecution_mode/notification_mode
キーを解析する。"""

from __future__ import annotations

from typing import Any

from jstock_advisor.domain.entities.enums import ExecutionMode, NotificationMode
from jstock_advisor.domain.entities.execution_context import ExecutionContext


def resolve_execution_context(event: dict[str, Any]) -> ExecutionContext:
    """未指定はNORMAL。不正な値はNORMALへフォールバックせず例外を送出する。

    通知ドライラン機能(2026-08追加): notification_modeはVALIDATION専用の補助設定
    であり、execution_mode=NORMALと組み合わせて指定することは許可しない(黙って
    無視・フォールバックせず、いずれも明確なValueErrorとする)。許可される組み合わせは
    次の3通りのみ: (1)NORMAL+notification_mode未指定、(2)VALIDATION+未指定
    (→SEND扱い)、(3)VALIDATION+SEND/DRY_RUNの明示指定。
    """
    raw_mode = event.get("execution_mode")
    raw_notification_mode = event.get("notification_mode")

    if raw_mode is None:
        if raw_notification_mode is not None:
            raise ValueError(
                "notification_mode requires execution_mode=VALIDATION "
                f"(got execution_mode unset, notification_mode={raw_notification_mode!r})"
            )
        return ExecutionContext.normal()

    try:
        mode = ExecutionMode(raw_mode)
    except ValueError:
        raise ValueError(f"unknown execution_mode: {raw_mode!r}") from None

    if raw_notification_mode is None:
        return ExecutionContext(mode=mode)

    if mode != ExecutionMode.VALIDATION:
        raise ValueError(
            "notification_mode requires execution_mode=VALIDATION "
            f"(got execution_mode={raw_mode!r}, notification_mode={raw_notification_mode!r})"
        )
    try:
        notification_mode = NotificationMode(raw_notification_mode)
    except ValueError:
        raise ValueError(f"unknown notification_mode: {raw_notification_mode!r}") from None
    return ExecutionContext(mode=mode, notification_mode=notification_mode)
