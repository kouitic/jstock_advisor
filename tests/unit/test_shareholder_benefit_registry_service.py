import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import BenefitUtilityCategory
from jstock_advisor.infrastructure.local_repository.shareholder_benefit_registry_repository import (
    ShareholderBenefitRegistryRepository,
)
from jstock_advisor.providers.shareholder_benefit.local_registry_impl import (
    LocalRegistryShareholderBenefitProvider,
)
from jstock_advisor.services.shareholder_benefit_registry_service import (
    ShareholderBenefitRegistryService,
)

_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)


@pytest.fixture
def repository(tmp_path: Path) -> ShareholderBenefitRegistryRepository:
    return ShareholderBenefitRegistryRepository(store_dir=tmp_path)


@pytest.fixture
def service(repository: ShareholderBenefitRegistryRepository) -> ShareholderBenefitRegistryService:
    return ShareholderBenefitRegistryService(repository=repository)


def test_register_creates_single_tier_benefit(service: ShareholderBenefitRegistryService) -> None:
    benefit = service.register(
        stock_code="2914",
        min_shares_required=100,
        frequency_per_year=1,
        category=BenefitUtilityCategory.CASH_EQUIVALENT,
        description="クオカード1000円分",
        min_shares_for_tier=100,
        estimated_value=Decimal("1000"),
        now=_NOW,
    )
    assert benefit.stock_code == "2914"
    assert len(benefit.benefits) == 1
    assert benefit.benefits[0].estimated_value == Decimal("1000")


def test_register_rejects_non_positive_min_shares(
    service: ShareholderBenefitRegistryService,
) -> None:
    with pytest.raises(ValueError):
        service.register(
            stock_code="2914",
            min_shares_required=0,
            frequency_per_year=1,
            category=BenefitUtilityCategory.CASH_EQUIVALENT,
            description="x",
            min_shares_for_tier=100,
            now=_NOW,
        )


def test_register_rejects_non_positive_frequency(
    service: ShareholderBenefitRegistryService,
) -> None:
    with pytest.raises(ValueError):
        service.register(
            stock_code="2914",
            min_shares_required=100,
            frequency_per_year=0,
            category=BenefitUtilityCategory.CASH_EQUIVALENT,
            description="x",
            min_shares_for_tier=100,
            now=_NOW,
        )


def test_register_overwrites_existing(service: ShareholderBenefitRegistryService) -> None:
    service.register(
        stock_code="2914",
        min_shares_required=100,
        frequency_per_year=1,
        category=BenefitUtilityCategory.CASH_EQUIVALENT,
        description="first",
        min_shares_for_tier=100,
        now=_NOW,
    )
    updated = service.register(
        stock_code="2914",
        min_shares_required=200,
        frequency_per_year=2,
        category=BenefitUtilityCategory.VERSATILE_POINT,
        description="second",
        min_shares_for_tier=200,
        now=_NOW,
    )
    assert updated.min_shares_required == 200
    assert len(updated.benefits) == 1
    assert updated.benefits[0].description == "second"


def test_add_benefit_detail_appends_tier(service: ShareholderBenefitRegistryService) -> None:
    service.register(
        stock_code="2914",
        min_shares_required=100,
        frequency_per_year=1,
        category=BenefitUtilityCategory.CASH_EQUIVALENT,
        description="100株優待",
        min_shares_for_tier=100,
        now=_NOW,
    )
    updated = service.add_benefit_detail(
        stock_code="2914",
        category=BenefitUtilityCategory.CASH_EQUIVALENT,
        description="1000株優待(上位)",
        min_shares_for_tier=1000,
        now=_NOW,
    )
    assert len(updated.benefits) == 2
    assert updated.benefits[1].min_shares_for_tier == 1000


def test_add_benefit_detail_rejects_unregistered_stock(
    service: ShareholderBenefitRegistryService,
) -> None:
    with pytest.raises(ValueError, match="未登録"):
        service.add_benefit_detail(
            stock_code="9999",
            category=BenefitUtilityCategory.CASH_EQUIVALENT,
            description="x",
            min_shares_for_tier=100,
        )


def test_update_status_sets_abolished_flag(service: ShareholderBenefitRegistryService) -> None:
    service.register(
        stock_code="2914",
        min_shares_required=100,
        frequency_per_year=1,
        category=BenefitUtilityCategory.CASH_EQUIVALENT,
        description="x",
        min_shares_for_tier=100,
        now=_NOW,
    )
    updated = service.update_status(
        stock_code="2914", is_abolished=True, change_note="2026年に廃止発表", now=_NOW
    )
    assert updated.is_abolished is True
    assert updated.change_note == "2026年に廃止発表"


def test_update_status_rejects_unregistered_stock(
    service: ShareholderBenefitRegistryService,
) -> None:
    with pytest.raises(ValueError, match="未登録"):
        service.update_status(stock_code="9999", is_abolished=True)


def test_delete_removes_registration(service: ShareholderBenefitRegistryService) -> None:
    service.register(
        stock_code="2914",
        min_shares_required=100,
        frequency_per_year=1,
        category=BenefitUtilityCategory.CASH_EQUIVALENT,
        description="x",
        min_shares_for_tier=100,
        now=_NOW,
    )
    assert service.delete("2914") is True
    assert service.get("2914") is None
    assert service.delete("2914") is False


def test_local_registry_provider_returns_registered_benefit(
    repository: ShareholderBenefitRegistryRepository,
    service: ShareholderBenefitRegistryService,
) -> None:
    service.register(
        stock_code="2914",
        min_shares_required=100,
        frequency_per_year=1,
        category=BenefitUtilityCategory.CASH_EQUIVALENT,
        description="x",
        min_shares_for_tier=100,
        now=_NOW,
    )
    provider = LocalRegistryShareholderBenefitProvider(repository=repository)
    assert provider.get_shareholder_benefit("2914") is not None
    assert provider.get_shareholder_benefit("9999") is None
