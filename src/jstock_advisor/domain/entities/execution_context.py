"""通知検証モード機能(2026-08追加)。プロセス内でのみ受け渡す実行モードの値オブジェクト。"""

from __future__ import annotations

from dataclasses import dataclass

from jstock_advisor.domain.entities.enums import ExecutionMode


@dataclass(frozen=True)
class ExecutionContext:
    mode: ExecutionMode

    @property
    def is_validation(self) -> bool:
        return self.mode == ExecutionMode.VALIDATION

    @classmethod
    def normal(cls) -> ExecutionContext:
        return cls(mode=ExecutionMode.NORMAL)
