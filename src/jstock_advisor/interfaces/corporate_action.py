"""corporate_action_provider インターフェース。"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from jstock_advisor.interfaces.types import CorporateActionEvent


class CorporateActionProvider(Protocol):
    def get_corporate_actions(self, stock_code: str, since: dt.date) -> list[CorporateActionEvent]:
        """株式分割・併合・自己株買い等のコーポレートアクションを取得する。無ければ空リスト。"""
        ...
