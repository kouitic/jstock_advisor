"""ProviderBundle生成のファクトリ。

MVPのローカル動作確認用にモックProviderを提供するほか、実データ提供元
(yfinance + EDINET)を組み合わせた実運用向けBundleも提供する
(責務分離の設計方針: 要求仕様3節)。株主優待・適時開示はまだ実データ提供元が
無いため、実運用向けBundleでは「データ無し」を返すプレースホルダーを使う
(モックの架空データと混在させないため)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.config.models import AppConfig
from jstock_advisor.infrastructure.edinet.client import EdinetClient
from jstock_advisor.infrastructure.edinet.document_finder import EdinetFilingCacheRepository
from jstock_advisor.providers.corporate_action.mock_impl import MockCorporateActionProvider
from jstock_advisor.providers.corporate_action.yfinance_impl import YFinanceCorporateActionProvider
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.disclosure.unavailable_impl import UnavailableDisclosureProvider
from jstock_advisor.providers.dividend_data.cross_validating_impl import (
    CrossValidatingDividendDataProvider,
)
from jstock_advisor.providers.dividend_data.edinet_impl import EdinetDividendDataProvider
from jstock_advisor.providers.dividend_data.mock_impl import MockDividendDataProvider
from jstock_advisor.providers.dividend_data.yfinance_impl import YFinanceDividendDataProvider
from jstock_advisor.providers.financial_data.mock_impl import MockFinancialDataProvider
from jstock_advisor.providers.financial_data.yfinance_impl import YFinanceFinancialDataProvider
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.market_data.yfinance_impl import YFinanceMarketDataProvider
from jstock_advisor.providers.shareholder_benefit.mock_impl import MockShareholderBenefitProvider
from jstock_advisor.providers.shareholder_benefit.unavailable_impl import (
    UnavailableShareholderBenefitProvider,
)
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


def build_real_provider_bundle(now: dt.datetime, config: AppConfig) -> ProviderBundle:
    """yfinance(株価・財務・配当の主データ源)+ EDINET(配当のクロスバリデーション用)。

    株主優待・適時開示は実データ提供元が未実装のため、常にデータ無しを返す
    プレースホルダーを使う(要求仕様12節: 取得できない場合は推測で補完しない)。
    """
    corporate_action = YFinanceCorporateActionProvider(now=now)
    dividend_data = CrossValidatingDividendDataProvider(
        primary=YFinanceDividendDataProvider(now=now),
        secondary=EdinetDividendDataProvider(
            client=EdinetClient(), cache_repository=EdinetFilingCacheRepository(), now=now
        ),
        corporate_action_provider=corporate_action,
        config=config.data_validation,
        now=now,
    )
    return ProviderBundle(
        market_data=YFinanceMarketDataProvider(now=now),
        financial_data=YFinanceFinancialDataProvider(now=now),
        dividend_data=dividend_data,
        shareholder_benefit=UnavailableShareholderBenefitProvider(),
        disclosure=UnavailableDisclosureProvider(),
        corporate_action=corporate_action,
    )
