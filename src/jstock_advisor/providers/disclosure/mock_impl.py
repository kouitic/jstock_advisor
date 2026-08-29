"""disclosure_provider のモック実装(開発・テスト用の合成データ)。"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.interfaces.disclosure import DisclosureQueryResult
from jstock_advisor.interfaces.types import Disclosure
from jstock_advisor.providers.mock_fixtures import MOCK_STOCKS

_PROVIDER_NAME = "mock_disclosure"


class MockDisclosureProvider:
    def __init__(self, now: dt.datetime | None = None) -> None:
        self._now = now or dt.datetime.now(dt.UTC)

    def get_disclosures(self, stock_code: str, since: dt.date) -> DisclosureQueryResult:
        """モックは常に「取得できた」provider。対象開示が無い場合も
        AVAILABLE + 空リスト(= 開示リスクなし)を返し、UNAVAILABLEにはしない
        (Issue #53 Phase B2: 「開示0件」と「調査できなかった」を混同しない)。"""
        profile = MOCK_STOCKS.get(stock_code)
        if profile is None:
            return DisclosureQueryResult.available([])
        source = DataSourceReference(provider=_PROVIDER_NAME, fetched_at=self._now)
        published = self._now - dt.timedelta(days=5)
        if published.date() < since:
            return DisclosureQueryResult.available([])
        return DisclosureQueryResult.available(
            [
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
        )

    def get_next_earnings_date(self, stock_code: str) -> dt.date | None:
        if stock_code not in MOCK_STOCKS:
            return None
        # 四半期ごとの決算発表を想定し、直近の3ヶ月後を返す簡易フィクスチャ
        return (self._now + dt.timedelta(days=45)).date()
