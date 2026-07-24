import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.interfaces.types import Disclosure
from jstock_advisor.services.disclosure_check_service import DisclosureCheckService
from jstock_advisor.services.portfolio_service import PortfolioService

_CONFIG = load_config()
_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)
_SOURCE = DataSourceReference(provider="test", fetched_at=_NOW)


class _FakeDisclosureProvider:
    def __init__(self, disclosures_by_stock: dict[str, list[Disclosure]]) -> None:
        self._disclosures_by_stock = disclosures_by_stock

    def get_disclosures(self, stock_code: str, since: dt.date) -> list[Disclosure]:
        return [
            d
            for d in self._disclosures_by_stock.get(stock_code, [])
            if d.published_at.date() >= since
        ]

    def get_next_earnings_date(self, stock_code: str) -> dt.date | None:
        return None


def _disclosure(stock_code: str, title: str, summary: str) -> Disclosure:
    return Disclosure(
        stock_code=stock_code,
        published_at=_NOW,
        title=title,
        category="臨時報告書",
        summary=summary,
        url=None,
        source=_SOURCE,
    )


@pytest.fixture
def portfolio_service(tmp_path: Path) -> PortfolioService:
    service = PortfolioService(
        holding_repository=HoldingRepository(store_dir=tmp_path),
        lot_repository=PurchaseLotRepository(store_dir=tmp_path),
    )
    service.register_purchase(
        stock_code="2914",
        stock_name="日本たばこ産業",
        shares=100,
        purchase_price=Decimal("3000"),
        purchase_date=dt.date(2026, 1, 10),
        account_type=AccountType.NISA,
    )
    return service


def test_check_holdings_finds_risk_keyword(portfolio_service: PortfolioService) -> None:
    provider = _FakeDisclosureProvider(
        {"2914": [_disclosure("2914", "臨時報告書", "特別調査委員会の設置について")]}
    )
    service = DisclosureCheckService(
        disclosure_provider=provider, config=_CONFIG, portfolio_service=portfolio_service
    )
    alerts = service.check_holdings(_NOW)
    assert len(alerts) == 1
    assert alerts[0].stock_code == "2914"
    assert "特別調査委員会" in alerts[0].matched_keywords


def test_check_holdings_ignores_disclosures_without_risk_keywords(
    portfolio_service: PortfolioService,
) -> None:
    provider = _FakeDisclosureProvider(
        {"2914": [_disclosure("2914", "臨時報告書", "代表取締役の異動について")]}
    )
    service = DisclosureCheckService(
        disclosure_provider=provider, config=_CONFIG, portfolio_service=portfolio_service
    )
    assert service.check_holdings(_NOW) == []


def test_check_holdings_ignores_unheld_stocks(portfolio_service: PortfolioService) -> None:
    provider = _FakeDisclosureProvider(
        {"9999": [_disclosure("9999", "臨時報告書", "特別調査委員会の設置について")]}
    )
    service = DisclosureCheckService(
        disclosure_provider=provider, config=_CONFIG, portfolio_service=portfolio_service
    )
    assert service.check_holdings(_NOW) == []


def test_check_holdings_returns_empty_when_no_holdings(tmp_path: Path) -> None:
    empty_portfolio = PortfolioService(
        holding_repository=HoldingRepository(store_dir=tmp_path),
        lot_repository=PurchaseLotRepository(store_dir=tmp_path),
    )
    provider = _FakeDisclosureProvider(
        {"2914": [_disclosure("2914", "臨時報告書", "特別調査委員会の設置について")]}
    )
    service = DisclosureCheckService(
        disclosure_provider=provider, config=_CONFIG, portfolio_service=empty_portfolio
    )
    assert service.check_holdings(_NOW) == []
