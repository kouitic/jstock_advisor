"""dividend_data_provider のモック実装(開発・テスト用の合成データ)。"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.interfaces.types import DividendInfo
from jstock_advisor.providers.mock_fixtures import MOCK_STOCKS

_PROVIDER_NAME = "mock_dividend_data"


class MockDividendDataProvider:
    def __init__(self, now: dt.datetime | None = None) -> None:
        self._now = now or dt.datetime.now(dt.UTC)

    def get_dividend_info(self, stock_code: str) -> DividendInfo | None:
        profile = MOCK_STOCKS.get(stock_code)
        if profile is None:
            return None

        year = self._now.year
        return DividendInfo(
            stock_code=stock_code,
            fiscal_year=f"{year}",
            forecast_annual_dividend_per_share=profile.forecast_annual_dividend_per_share,
            actual_annual_dividend_per_share=profile.previous_fiscal_year_dividend_per_share,
            previous_fiscal_year_dividend_per_share=profile.previous_fiscal_year_dividend_per_share,
            is_dividend_cut_announced=False,
            is_dividend_omission_announced=False,
            is_progressive_or_doe_policy=profile.is_progressive_or_doe_policy,
            dividend_policy_note=(
                "累進配当方針(減配を行わず配当維持または増配)"
                if profile.is_progressive_or_doe_policy
                else None
            ),
            dividend_record_dates=[dt.date(year, 3, 31), dt.date(year, 9, 30)],
            consecutive_dividend_increase_years=profile.consecutive_dividend_increase_years,
            source=DataSourceReference(provider=_PROVIDER_NAME, fetched_at=self._now),
        )
