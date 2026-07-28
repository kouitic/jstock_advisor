"""disclosure_provider の実データ実装。

適時開示(TDnet)は公式APIが無いため、実測検証の結果に基づき以下2つの実データ源を
組み合わせる:

  - get_disclosures: EDINET臨時報告書・訂正臨時報告書(docTypeCode 180/190)。
    代表者異動・特定子会社異動・財務上の特約(コベナンツ)等、金融商品取引法上
    重要とされる会社情報の変更は臨時報告書としてEDINETにも提出義務があるため、
    重大リスクの検知という目的においてはTDnetの適時開示と同等の実効性を持つ。
    ただし決算短信そのものはTDnet専用でEDINETには提出されないため取得不可。
  - get_next_earnings_date: yfinanceのTicker.calendarから取得する。実測検証済み
    (大型株〜中小型株の複数銘柄で取得できることを確認済み。ただし非公式ライブラリの
    ため将来的に取得できなくなる可能性はある)。
"""

from __future__ import annotations

import datetime as dt

import yfinance as yf

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import SourceType
from jstock_advisor.infrastructure.edinet.client import EdinetClient
from jstock_advisor.infrastructure.edinet.disclosure_finder import (
    EdinetDisclosureCacheRepository,
    find_extraordinary_reports,
)
from jstock_advisor.interfaces.types import Disclosure

_EDINET_PROVIDER_NAME = "edinet"


class EdinetYfinanceDisclosureProvider:
    def __init__(
        self,
        client: EdinetClient | None = None,
        cache_repository: EdinetDisclosureCacheRepository | None = None,
        now: dt.datetime | None = None,
    ) -> None:
        self._client = client or EdinetClient()
        self._cache_repo = cache_repository or EdinetDisclosureCacheRepository()
        self._now = now or dt.datetime.now(dt.UTC)

    def get_disclosures(self, stock_code: str, since: dt.date) -> list[Disclosure]:
        cache = find_extraordinary_reports(self._client, self._cache_repo, stock_code, self._now)
        if cache is None:
            return []
        source = DataSourceReference(
            provider=_EDINET_PROVIDER_NAME,
            fetched_at=self._now,
            source_type=SourceType.TDNET_EDINET,
            primary_source_flag=True,
        )
        return [
            Disclosure(
                stock_code=stock_code,
                published_at=dt.datetime.combine(record.submit_date, dt.time.min, tzinfo=dt.UTC),
                title="臨時報告書",
                category="臨時報告書",
                summary=record.summary,
                url=None,
                source=source,
            )
            for record in cache.records
            if record.submit_date >= since
        ]

    def get_next_earnings_date(self, stock_code: str) -> dt.date | None:
        try:
            ticker = yf.Ticker(f"{stock_code}.T")
            calendar = ticker.calendar
        except Exception:  # noqa: BLE001 - 非公式ライブラリのため例外種別を限定できない
            return None

        if not isinstance(calendar, dict):
            return None
        earnings_dates = calendar.get("Earnings Date")
        if not earnings_dates:
            return None
        first = earnings_dates[0]
        return first if isinstance(first, dt.date) else None
