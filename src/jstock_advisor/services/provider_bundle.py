"""買い判定・利確判定・売却判定サービスが共通して使う外部データProviderの束。"""

from __future__ import annotations

from dataclasses import dataclass

from jstock_advisor.interfaces.corporate_action import CorporateActionProvider
from jstock_advisor.interfaces.disclosure import DisclosureProvider
from jstock_advisor.interfaces.dividend_data import DividendDataProvider
from jstock_advisor.interfaces.financial_data import FinancialDataProvider
from jstock_advisor.interfaces.market_data import MarketDataProvider
from jstock_advisor.interfaces.shareholder_benefit import ShareholderBenefitProvider


@dataclass(frozen=True)
class ProviderBundle:
    market_data: MarketDataProvider
    financial_data: FinancialDataProvider
    dividend_data: DividendDataProvider
    shareholder_benefit: ShareholderBenefitProvider
    disclosure: DisclosureProvider
    corporate_action: CorporateActionProvider
