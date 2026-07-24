import datetime as dt
from pathlib import Path

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.infrastructure.local_repository.audit_log_repository import (
    AuditLogRepository,
)
from jstock_advisor.providers.corporate_action.mock_impl import MockCorporateActionProvider
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.dividend_data.mock_impl import MockDividendDataProvider
from jstock_advisor.providers.financial_data.mock_impl import MockFinancialDataProvider
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.shareholder_benefit.mock_impl import MockShareholderBenefitProvider
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.buy_signal_service import BuySignalService
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.sell_signal_service import SellSignalService

_NOW = dt.datetime(2026, 7, 24, 7, 0, tzinfo=dt.UTC)
_CONFIG = load_config()


def _providers() -> ProviderBundle:
    return ProviderBundle(
        market_data=MockMarketDataProvider(now=_NOW),
        financial_data=MockFinancialDataProvider(now=_NOW),
        dividend_data=MockDividendDataProvider(now=_NOW),
        shareholder_benefit=MockShareholderBenefitProvider(now=_NOW),
        disclosure=MockDisclosureProvider(now=_NOW),
        corporate_action=MockCorporateActionProvider(),
    )


def test_repository_roundtrip_preserves_data_sources(tmp_path: Path) -> None:
    repo = AuditLogRepository(store_dir=tmp_path)
    audit = AuditService(repository=repo)
    source = DataSourceReference(provider="mock_market_data", fetched_at=_NOW)

    entry = audit.record(
        decision_type="buy_signal",
        stock_code="8136",
        input_values={"current_price": "4200", "shares": 100},
        calculation_formulas={"total_yield": "dividend/price + benefit/price"},
        output_values={"recommended": True},
        data_sources=[source],
        rule_version="v1-mvp",
        timestamp=_NOW,
    )

    reloaded = repo.get(entry.audit_id)
    assert reloaded is not None
    assert reloaded.stock_code == "8136"
    assert reloaded.input_values == {"current_price": "4200", "shares": 100}
    assert reloaded.output_values == {"recommended": True}
    assert reloaded.data_sources[0].provider == "mock_market_data"


def test_list_by_stock_and_decision_type(tmp_path: Path) -> None:
    repo = AuditLogRepository(store_dir=tmp_path)
    audit = AuditService(repository=repo)

    audit.record(
        decision_type="buy_signal",
        stock_code="8136",
        input_values={},
        calculation_formulas={},
        output_values={},
        data_sources=[],
        rule_version="v1-mvp",
        timestamp=_NOW,
    )
    audit.record(
        decision_type="sell_signal",
        stock_code="8136",
        input_values={},
        calculation_formulas={},
        output_values={},
        data_sources=[],
        rule_version="v1-mvp",
        timestamp=_NOW,
    )
    audit.record(
        decision_type="buy_signal",
        stock_code="2914",
        input_values={},
        calculation_formulas={},
        output_values={},
        data_sources=[],
        rule_version="v1-mvp",
        timestamp=_NOW,
    )

    assert len(repo.list_by_stock("8136")) == 2
    assert len(repo.list_by_decision_type("buy_signal")) == 2
    assert len(repo.list_by_decision_type("sell_signal")) == 1


def test_buy_signal_service_records_audit_even_when_excluded(tmp_path: Path) -> None:
    calendar = BusinessCalendar.from_config(_CONFIG.holiday_calendar)
    audit_repo = AuditLogRepository(store_dir=tmp_path)
    audit_service = AuditService(repository=audit_repo)
    service = BuySignalService(
        providers=_providers(),
        config=_CONFIG,
        business_calendar=calendar,
        audit_service=audit_service,
    )

    # 9861(吉野家)は総合利回りが基準未満で除外される想定(既存の分析結果より)
    outcome = service.analyze("9861", _NOW)
    assert outcome.recommendation is None

    entries = audit_repo.list_by_stock("9861")
    assert len(entries) == 1
    assert entries[0].decision_type == "buy_signal"
    assert entries[0].output_values["recommended"] is False
    assert entries[0].rule_version == "v1-mvp"


def test_buy_signal_service_records_audit_on_data_error(tmp_path: Path) -> None:
    calendar = BusinessCalendar.from_config(_CONFIG.holiday_calendar)
    audit_repo = AuditLogRepository(store_dir=tmp_path)
    audit_service = AuditService(repository=audit_repo)
    service = BuySignalService(
        providers=_providers(),
        config=_CONFIG,
        business_calendar=calendar,
        audit_service=audit_service,
    )

    outcome = service.analyze("0000", _NOW)  # モックに存在しない銘柄コード
    assert outcome.data_error is not None

    entries = audit_repo.list_by_stock("0000")
    assert len(entries) == 1
    assert "data_error" in entries[0].output_values


def test_profit_taking_service_records_audit(tmp_path: Path) -> None:
    audit_repo = AuditLogRepository(store_dir=tmp_path)
    audit_service = AuditService(repository=audit_repo)
    service = ProfitTakingService(providers=_providers(), config=_CONFIG, audit_service=audit_service)

    holding = Holding(
        stock_code="8136",
        stock_name="サンリオ",
        shares=100,
        average_purchase_price=3000,
        total_purchase_amount=300000,
        first_purchase_date=dt.date(2021, 3, 1),
        last_purchase_date=dt.date(2021, 3, 1),
        account_type=AccountType.NISA,
        created_at=_NOW,
        updated_at=_NOW,
    )
    service.analyze(holding, _NOW)

    entries = audit_repo.list_by_decision_type("profit_taking")
    assert len(entries) == 1
    assert entries[0].stock_code == "8136"
    assert "recommendation_type" in entries[0].output_values


def test_sell_signal_service_records_audit(tmp_path: Path) -> None:
    audit_repo = AuditLogRepository(store_dir=tmp_path)
    audit_service = AuditService(repository=audit_repo)
    service = SellSignalService(providers=_providers(), config=_CONFIG, audit_service=audit_service)

    holding = Holding(
        stock_code="2914",
        stock_name="日本たばこ産業",
        shares=100,
        average_purchase_price=4000,
        total_purchase_amount=400000,
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )
    service.analyze(holding, _NOW)

    entries = audit_repo.list_by_decision_type("sell_signal")
    assert len(entries) == 1
    assert entries[0].stock_code == "2914"
    assert "triggered_rules" in entries[0].output_values
