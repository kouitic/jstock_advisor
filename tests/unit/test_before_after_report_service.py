import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import AccountType, ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.infrastructure.local_repository.holding_repository import HoldingRepository
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.providers.corporate_action.mock_impl import MockCorporateActionProvider
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.dividend_data.mock_impl import MockDividendDataProvider
from jstock_advisor.providers.financial_data.mock_impl import MockFinancialDataProvider
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.shareholder_benefit.mock_impl import MockShareholderBenefitProvider
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.before_after_report_service import BeforeAfterReportService
from jstock_advisor.services.provider_bundle import ProviderBundle

_NOW = dt.datetime(2026, 7, 27, 7, 0, tzinfo=dt.UTC)
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


def _service(tmp_path: Path) -> BeforeAfterReportService:
    return BeforeAfterReportService(
        providers=_providers(),
        config=_CONFIG,
        recommendation_repository=RecommendationRepository(store_dir=tmp_path),
        audit_log_repository=AuditLogRepository(store_dir=tmp_path),
        holding_repository=HoldingRepository(store_dir=tmp_path),
    )


def test_entry_without_holding_skips_after_and_notes_reason(tmp_path: Path) -> None:
    service = _service(tmp_path)

    entry = service.build_entry("2914", _NOW)

    assert entry.holding is None
    assert entry.after_profit_taking is None
    assert entry.after_sell_signal is None
    assert entry.not_held_note is not None
    assert entry.before_recommendations == []


def test_entry_with_holding_runs_after_pipeline(tmp_path: Path) -> None:
    holding_repo = HoldingRepository(store_dir=tmp_path)
    holding = Holding(
        stock_code="2914",
        stock_name="日本たばこ産業",
        shares=100,
        average_purchase_price=Decimal("4000"),
        total_purchase_amount=Decimal("400000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )
    holding_repo.upsert(holding)

    recommendation_repo = RecommendationRepository(store_dir=tmp_path)
    before_rec = Recommendation(
        recommendation_id="before-1",
        stock_code="2914",
        stock_name="日本たばこ産業",
        recommended_at=_NOW - dt.timedelta(days=30),
        recommendation_type=RecommendationType.FULL_PROFIT_TAKE,
        price_at_recommendation=Decimal("6531"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )
    recommendation_repo.save(before_rec)

    audit_repo = AuditLogRepository(store_dir=tmp_path)
    audit_repo.save(
        AuditService(repository=audit_repo).record(
            decision_type="profit_taking",
            stock_code="2914",
            input_values={},
            calculation_formulas={},
            output_values={},
            data_sources=[],
            rule_version="v1-mvp",
            timestamp=_NOW - dt.timedelta(days=30),
        )
    )

    service = BeforeAfterReportService(
        providers=_providers(),
        config=_CONFIG,
        recommendation_repository=recommendation_repo,
        audit_log_repository=audit_repo,
        holding_repository=holding_repo,
    )

    entry = service.build_entry("2914", _NOW)

    assert entry.holding is not None
    assert entry.before_recommendations == [before_rec]
    assert len(entry.before_audit_entries) == 1
    assert entry.after_error is None


def test_render_markdown_includes_both_sections(tmp_path: Path) -> None:
    service = _service(tmp_path)
    report = service.build_report(["2914"], _NOW)

    markdown = service.render_markdown(report)

    assert "## 2914" in markdown
    assert "### Before" in markdown
    assert "### After" in markdown
    assert "2026-07-27" in markdown
