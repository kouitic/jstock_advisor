"""news_provider のモック実装。MVPでは対象外(未確定事項#8)のため常に空を返す。"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.interfaces.types import NewsItem


class MockNewsProvider:
    def get_news(self, stock_code: str, since: dt.date) -> list[NewsItem]:
        return []
