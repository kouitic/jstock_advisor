"""news_provider インターフェース。MVPでは未使用(未確定事項#8)だが、拡張性のため定義しておく。"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from jstock_advisor.interfaces.types import NewsItem


class NewsProvider(Protocol):
    def get_news(self, stock_code: str, since: dt.date) -> list[NewsItem]:
        """関連ニュースを取得する。無ければ空リスト。"""
        ...
