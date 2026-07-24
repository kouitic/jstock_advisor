"""ProviderBundle生成のファクトリ。

MVPではモックProviderのみを提供する。本番運用では実データ提供元(J-Quants等)の
実装をここで組み立てるように差し替える(責務分離の設計方針: 要求仕様3節)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.providers.corporate_action.mock_impl import MockCorporateActionProvider
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.dividend_data.mock_impl import MockDividendDataProvider
from jstock_advisor.providers.financial_data.mock_impl import MockFinancialDataProvider
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.shareholder_benefit.mock_impl import MockShareholderBenefitProvider
from jstock_advisor.services.provider_bundle import ProviderBundle


def build_mock_provider_bundle(now: dt.datetime) -> ProviderBundle:
    return ProviderBundle(
        market_data=MockMarketDataProvider(now=now),
        financial_data=MockFinancialDataProvider(now=now),
        dividend_data=MockDividendDataProvider(now=now),
        shareholder_benefit=MockShareholderBenefitProvider(now=now),
        disclosure=MockDisclosureProvider(now=now),
        corporate_action=MockCorporateActionProvider(),
    )
