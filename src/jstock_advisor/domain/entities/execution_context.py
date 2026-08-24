"""通知検証モード機能(2026-08追加)。プロセス内でのみ受け渡す実行モードの値オブジェクト。"""

from __future__ import annotations

from dataclasses import dataclass

from jstock_advisor.domain.entities.enums import ExecutionMode, NotificationMode


@dataclass(frozen=True)
class ExecutionContext:
    mode: ExecutionMode
    # 通知ドライラン機能(2026-08追加)。VALIDATION専用の補助設定で、NORMALでは
    # 常にSEND(既定値)のまま無視されず使われることもない
    # (_execution_mode.resolve_execution_context()がNORMAL+notification_mode指定を
    # エラーにするため、この既定値がNORMAL側で意味を持つことはない)。
    notification_mode: NotificationMode = NotificationMode.SEND

    @property
    def is_validation(self) -> bool:
        return self.mode == ExecutionMode.VALIDATION

    @property
    def is_dry_run(self) -> bool:
        """VALIDATIONかつnotification_mode=DRY_RUNの場合のみTrue。

        LineNotificationService._push()がこのプロパティのみを見て外部LINE
        送信の可否を判定する、唯一の判定集約点(呼び出し側の各通知メソッドへ
        判定ロジックを複製しない)。
        """
        return self.is_validation and self.notification_mode == NotificationMode.DRY_RUN

    @classmethod
    def normal(cls) -> ExecutionContext:
        return cls(mode=ExecutionMode.NORMAL)
