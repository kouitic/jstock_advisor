"""corporate_action_provider のローカルレジストリ実装。

yfinanceが自動取得できない無償割当・スピンオフ・銘柄コード変更・合併・
上場廃止・配当基準変更は、運用者が一次情報を確認したうえで登録した内容を
そのまま返す(株主優待の手動登録と同じ設計方針、要求仕様2節)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.infrastructure.local_repository.corporate_action_registry_repository import (
    CorporateActionRegistryRepository,
)
from jstock_advisor.interfaces.types import CorporateActionEvent


class LocalRegistryCorporateActionProvider:
    def __init__(self, repository: CorporateActionRegistryRepository | None = None) -> None:
        self._repo = repository or CorporateActionRegistryRepository()

    def get_corporate_actions(self, stock_code: str, since: dt.date) -> list[CorporateActionEvent]:
        return [
            e
            for e in self._repo.list_by_stock(stock_code)
            if e.effective_date is None or e.effective_date >= since
        ]
