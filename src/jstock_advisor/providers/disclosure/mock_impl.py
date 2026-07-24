"""disclosure_provider のモック実装(開発・テスト用の合成データ)。"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.interfaces.types import Disclosure
from jstock_advisor.providers.mock_fixtures import MOCK_STOCKS

_PROVIDER_NAME = "mock_disclosure"


class MockDisclosureProvider:
    def __init__(self, now: dt.datetime | None = None) -> None:
        self._now = now or dt.datetime.now(dt.UTC)

    def get_disclosures(self, stock_code: str, since: dt.date) -> list[Disclosure]:
        profile = MOCK_STOCKS.get(stock_code)
        if profile is None:
            return []
        source = DataSourceReference(provider=_PROVIDER_NAME, fetched_at=self._now)
        published = self._now - dt.timedelta(days=5)
        if published.date() < since:
            return []
        return [
            Disclosure(
                stock_code=stock_code,
                published_at=published,
                title=f"{profile.stock_name} 決算短信(モックデータ)",
                category="決算短信",
                summary="直近四半期の業績は前年同期比で堅調に推移(モックデータ)。",
                url=None,
                source=source,
            )
        ]

    def get_next_earnings_date(self, stock_code: str) -> dt.date | None:
        if stock_code not in MOCK_STOCKS:
            return None
        # 四半期ごとの決算発表を想定し、直近の3ヶ月後を返す簡易フィクスチャ
        return (self._now + dt.timedelta(days=45)).date()
