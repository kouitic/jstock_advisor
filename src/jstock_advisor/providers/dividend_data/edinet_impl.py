"""dividend_data_provider の EDINET実装(検証・突合用)。

有価証券報告書の「経営指標等の推移」表から1株当たり配当額(過去5期分)を取得する。
この値は株式分割・併合による遡及調整がされていない額面ベースであることに注意
(実測で確認済み。株式分割があった銘柄をyfinance等の分割調整済みデータと比較する
際は分割比率で補正する必要がある)。forecast(予想配当)はこの表には含まれないため
Noneとなる。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.infrastructure.edinet.client import EdinetClient
from jstock_advisor.infrastructure.edinet.csv_parser import EdinetCsvRow, extract_main_document_rows
from jstock_advisor.infrastructure.edinet.document_finder import (
    EdinetFilingCacheRepository,
    find_latest_filings,
)
from jstock_advisor.interfaces.types import DividendInfo

_PROVIDER_NAME = "edinet"
_DIVIDEND_ELEMENT = "DividendPaidPerShareSummaryOfBusinessResults"
_PERIOD_LABELS_OLDEST_TO_NEWEST = ["四期前", "三期前", "前々期", "前期", "当期"]


class EdinetDividendDataProvider:
    def __init__(
        self,
        client: EdinetClient | None = None,
        cache_repository: EdinetFilingCacheRepository | None = None,
        now: dt.datetime | None = None,
    ) -> None:
        self._client = client or EdinetClient()
        self._cache_repo = cache_repository or EdinetFilingCacheRepository()
        self._now = now or dt.datetime.now(dt.UTC)

    def _source(self) -> DataSourceReference:
        return DataSourceReference(provider=_PROVIDER_NAME, fetched_at=self._now)

    def get_dividend_info(self, stock_code: str) -> DividendInfo | None:
        if not self._client.is_configured:
            return None

        filing = find_latest_filings(self._client, self._cache_repo, stock_code, self._now)
        if filing is None or filing.latest_annual_doc_id is None:
            return None

        zip_bytes = self._client.download_document_zip(filing.latest_annual_doc_id)
        if zip_bytes is None:
            return None

        rows = extract_main_document_rows(zip_bytes)
        if rows is None:
            return None

        yearly_values = self._extract_five_year_series(rows)
        if not yearly_values:
            return None

        actual = yearly_values.get("当期")
        previous = yearly_values.get("前期")
        consecutive_increase_years = self._count_consecutive_increases(yearly_values)

        return DividendInfo(
            stock_code=stock_code,
            fiscal_year=filing.latest_annual_period_end or str(self._now.year),
            forecast_annual_dividend_per_share=None,  # 経営指標等の推移表には次期予想が無い
            actual_annual_dividend_per_share=actual,
            previous_fiscal_year_dividend_per_share=previous,
            is_dividend_cut_announced=False,
            is_dividend_omission_announced=False,
            is_progressive_or_doe_policy=False,
            dividend_policy_note=None,
            dividend_record_dates=[],  # EDINETのこの表からは権利確定日を取得できない
            consecutive_dividend_increase_years=consecutive_increase_years,
            source=self._source(),
        )

    @staticmethod
    def _extract_five_year_series(rows: list[EdinetCsvRow]) -> dict[str, Decimal]:
        candidates = [r for r in rows if r.element_id.split(":")[-1] == _DIVIDEND_ELEMENT]
        consolidated = [r for r in candidates if "NonConsolidated" not in r.context_id]
        pool = consolidated or candidates

        result: dict[str, Decimal] = {}
        for label in _PERIOD_LABELS_OLDEST_TO_NEWEST:
            matches = [r for r in pool if r.relative_period == label]
            if not matches:
                continue
            try:
                result[label] = Decimal(matches[0].value)
            except (InvalidOperation, ValueError):
                continue
        return result

    @staticmethod
    def _count_consecutive_increases(yearly_values: dict[str, Decimal]) -> int | None:
        ordered = [
            yearly_values[label]
            for label in _PERIOD_LABELS_OLDEST_TO_NEWEST
            if label in yearly_values
        ]
        if len(ordered) < 2:
            return None
        count = 0
        for i in range(len(ordered) - 1, 0, -1):
            if ordered[i] > ordered[i - 1]:
                count += 1
            else:
                break
        return count
