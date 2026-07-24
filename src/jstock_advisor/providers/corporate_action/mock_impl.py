"""corporate_action_provider のモック実装。MVPフィクスチャでは分割・併合等は発生しない。"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.interfaces.types import CorporateActionEvent


class MockCorporateActionProvider:
    def get_corporate_actions(self, stock_code: str, since: dt.date) -> list[CorporateActionEvent]:
        return []
