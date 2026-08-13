import datetime as dt
from pathlib import Path

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import AccountType, BuyAction, ExecutionMode
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.infrastructure.local_repository.audit_log_repository import (
    AuditLogRepository,
)
from jstock_advisor.infrastructure.local_repository.rule_version_repository import (
    RuleVersionRepository,
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
from jstock_advisor.services.rule_version_service import RuleVersionService
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


def test_record_roundtrips_traceability_extension_fields(tmp_path: Path) -> None:
    repo = AuditLogRepository(store_dir=tmp_path)
    audit = AuditService(repository=repo)

    entry = audit.record(
        decision_type="profit_taking",
        stock_code="2914",
        input_values={},
        calculation_formulas={},
        output_values={},
        data_sources=[],
        rule_version="v1-mvp",
        timestamp=_NOW,
        fair_value_results=[{"method": "per", "fair_value": "5000"}],
        triggered_rules=["含み益率63.0%が全株利確閾値に到達"],
        suppressed_rules=["累進配当方針のため緩和"],
        consistency_validation_result={"passed": False, "violations": []},
        confidence_score=0.8,
        source_metadata=[{"provider": "yfinance", "source_type": "CONTRACTED_PROVIDER"}],
    )

    reloaded = repo.get(entry.audit_id)
    assert reloaded is not None
    assert reloaded.fair_value_results == [{"method": "per", "fair_value": "5000"}]
    assert reloaded.triggered_rules == ["含み益率63.0%が全株利確閾値に到達"]
    assert reloaded.suppressed_rules == ["累進配当方針のため緩和"]
    assert reloaded.consistency_validation_result == {"passed": False, "violations": []}
    assert reloaded.confidence_score == 0.8
    assert reloaded.source_metadata == [
        {"provider": "yfinance", "source_type": "CONTRACTED_PROVIDER"}
    ]
    # 未指定のフィールドは推測で補完せずNone/空のままとなる
    assert reloaded.raw_input_data is None
    assert reloaded.data_quality_score is None


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

    # 9861(吉野家)は総合利回りが低いが、BUY候補裾野拡大機能(2026-08)で
    # 総合利回りは一次スクリーニングのハード除外条件では無くなったため、
    # 一次スクリーニングは通過し、後続の評価でNOT_ATTRACTIVEとなる想定
    # (既存の分析結果より)。screening自体の除外挙動(REIT/ETF/債務超過/
    # 継続企業疑義/流動性/データ鮮度/開示リスク)はtest_screening.pyで
    # 個別に検証済みのため、ここではEXCLUDED以外の判定でも監査が
    # 記録されることを確認する。
    outcome = service.analyze("9861", _NOW)
    assert outcome.recommendation is not None
    assert outcome.buy_action == BuyAction.NOT_ATTRACTIVE

    entries = audit_repo.list_by_stock("9861")
    assert len(entries) == 1
    assert entries[0].decision_type == "buy_signal"
    assert entries[0].output_values["final_buy_action"] == BuyAction.NOT_ATTRACTIVE.value
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
    service = ProfitTakingService(
        providers=_providers(), config=_CONFIG, audit_service=audit_service
    )

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
    assert entries[0].fair_value_results is not None
    assert entries[0].triggered_rules == entries[0].output_values["triggered_reasons"]
    assert entries[0].suppressed_rules == entries[0].output_values["mitigating_factors_applied"]


def test_profit_taking_service_uses_active_rule_version(tmp_path: Path) -> None:
    audit_repo = AuditLogRepository(store_dir=tmp_path)
    audit_service = AuditService(repository=audit_repo)
    rule_version_repo = RuleVersionRepository(store_dir=tmp_path)
    rule_version_service = RuleVersionService(repository=rule_version_repo)
    rule_version_service.create_draft(
        "v2-corporate-action-redesign", "desc", "reason", now=_NOW
    )
    rule_version_service.submit_for_approval("v2-corporate-action-redesign")
    rule_version_service.approve("v2-corporate-action-redesign", "alice")
    rule_version_service.activate("v2-corporate-action-redesign", now=_NOW)

    service = ProfitTakingService(
        providers=_providers(),
        config=_CONFIG,
        audit_service=audit_service,
        rule_version_service=rule_version_service,
    )
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
    outcome = service.analyze(holding, _NOW)

    entries = audit_repo.list_by_decision_type("profit_taking")
    assert entries[0].rule_version == "v2-corporate-action-redesign"
    if outcome.recommendation is not None:
        assert outcome.recommendation.rule_version == "v2-corporate-action-redesign"


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


def test_validation_record_does_not_persist_to_repository(tmp_path: Path) -> None:
    """通知検証モード コードレビュー対応: execution_context=VALIDATIONでは
    record()が呼び出し元へ通常どおりAuditLogEntryを返しつつ、本番
    AuditLogRepositoryへは一切保存しない(単一choke pointのguard)。
    """
    repo = AuditLogRepository(store_dir=tmp_path)
    audit = AuditService(
        repository=repo, execution_context=ExecutionContext(mode=ExecutionMode.VALIDATION)
    )

    entry = audit.record(
        decision_type="buy_signal",
        stock_code="8136",
        input_values={},
        calculation_formulas={},
        output_values={"recommended": True},
        data_sources=[],
        rule_version="v1-mvp",
        timestamp=_NOW,
    )

    assert entry.stock_code == "8136"
    assert repo.list_all() == []
    assert repo.get(entry.audit_id) is None


def test_validation_record_if_absent_does_not_persist_to_repository(tmp_path: Path) -> None:
    repo = AuditLogRepository(store_dir=tmp_path)
    audit = AuditService(
        repository=repo, execution_context=ExecutionContext(mode=ExecutionMode.VALIDATION)
    )

    entry = audit.record_if_absent(
        audit_id="deterministic-1",
        decision_type="watchlist_batch_audit",
        stock_code=None,
        input_values={},
        calculation_formulas={},
        output_values={},
        data_sources=[],
        rule_version="v1-mvp",
        timestamp=_NOW,
    )

    assert entry is not None
    assert repo.list_all() == []


def test_normal_execution_context_explicit_still_persists(tmp_path: Path) -> None:
    """NORMAL回帰確認: ExecutionContext.normal()を明示的に渡した場合も
    従来どおり本番AuditLogRepositoryへ保存される。"""
    repo = AuditLogRepository(store_dir=tmp_path)
    audit = AuditService(repository=repo, execution_context=ExecutionContext.normal())

    entry = audit.record(
        decision_type="buy_signal",
        stock_code="8136",
        input_values={},
        calculation_formulas={},
        output_values={"recommended": True},
        data_sources=[],
        rule_version="v1-mvp",
        timestamp=_NOW,
    )

    assert repo.get(entry.audit_id) is not None
