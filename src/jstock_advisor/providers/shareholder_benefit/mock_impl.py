"""shareholder_benefit_provider のモック実装(開発・テスト用の合成データ)。

本番運用では自動取得を行わず、ユーザーが手動/CSVで登録したデータを返す
ローカルリポジトリ実装(local_registry_impl.py)を使用する(未確定事項#5)。
このモック実装はロジックの単体テスト・ローカルMVP動作確認専用。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import BenefitUtilityCategory
from jstock_advisor.interfaces.types import BenefitDetail, ShareholderBenefit
from jstock_advisor.providers.mock_fixtures import MOCK_STOCKS

_PROVIDER_NAME = "mock_shareholder_benefit"


class MockShareholderBenefitProvider:
    def __init__(self, now: dt.datetime | None = None) -> None:
        self._now = now or dt.datetime.now(dt.UTC)

    def get_shareholder_benefit(self, stock_code: str) -> ShareholderBenefit | None:
        profile = MOCK_STOCKS.get(stock_code)
        if profile is None or not profile.benefits:
            return None

        year = self._now.year
        return ShareholderBenefit(
            stock_code=stock_code,
            min_shares_required=profile.benefit_min_shares,
            benefits=[
                BenefitDetail(
                    category=BenefitUtilityCategory[b.category],
                    description=b.description,
                    estimated_value=b.estimated_value,
                    min_shares_for_tier=b.min_shares_for_tier,
                    long_term_holding_condition_months=b.long_term_holding_condition_months,
                )
                for b in profile.benefits
            ],
            frequency_per_year=profile.benefit_frequency_per_year,
            benefit_record_dates=[dt.date(year, 3, 31)]
            if profile.benefit_frequency_per_year == 1
            else [dt.date(year, 3, 31), dt.date(year, 9, 30)],
            is_abolished=False,
            is_major_downgrade=False,
            source=DataSourceReference(provider=_PROVIDER_NAME, fetched_at=self._now),
        )
