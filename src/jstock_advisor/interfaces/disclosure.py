"""disclosure_provider インターフェース。"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from jstock_advisor.interfaces.types import Disclosure


class DisclosureProvider(Protocol):
    def get_disclosures(self, stock_code: str, since: dt.date) -> list[Disclosure]:
        """適時開示・決算短信等を取得する。無ければ空リスト。"""
        ...

    def get_next_earnings_date(self, stock_code: str) -> dt.date | None:
        """次回決算発表予定日を取得する。不明であればNone。"""
        ...
