"""既存ロジック(SellSignalService/ProfitTakingService)への非回帰・mode排他制御の
統合テスト(実装プラン20節「回帰」観点)。

mock providersを使い、_analyze_one_holding()をlegacy/shadow/activeの3モードで
実行し、新旧エンジンが同一銘柄・同一サイクルで二重通知しないこと、
mode=legacyでは従来どおりHoldingDecisionServiceが一切実行されないこと、
ProfitTakingServiceが従来どおり機能し続けることを検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    AccountType,
    ConfidenceLevel,
    FinancialPolicyOverride,
    RecommendationType,
    RuntimeConfigMode,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.infrastructure.local_repository.daily_notification_priority_repository import (
    DailyNotificationPriorityRepository,
)
from jstock_advisor.infrastructure.local_repository.holding_decision_result_repository import (
    HoldingDecisionResultRepository,
)
from jstock_advisor.infrastructure.local_repository.holdings_snapshot_repository import (
    HoldingsSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers.holdings_watchlist_handler import _analyze_one_holding
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.holding_decision_runtime_config_service import (
    HoldingDecisionRuntimeConfigService,
)
from jstock_advisor.services.holding_decision_service import HoldingDecisionService
from jstock_advisor.services.investment_thesis_service import InvestmentThesisService
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.rule_version_service import RuleVersionService
from jstock_advisor.services.sell_signal_service import SellSignalOutcome, SellSignalService

_CFG = load_config()
_NOW = dt.datetime.now(dt.UTC)
_PROVIDERS = build_mock_provider_bundle(_NOW)


class _FakeLineClient:
    def __init__(self) -> None:
        self.sent_messages: list[str] = []

    def push_message(self, message: str) -> None:
        self.sent_messages.append(message)


def _build_services(store_dir: Path, mode: RuntimeConfigMode):
    thesis_service = InvestmentThesisService(store_dir=store_dir)
    runtime_config_service = HoldingDecisionRuntimeConfigService(store_dir=store_dir)
    runtime_config_service.init_config(
        "tester",
        mode=mode,
        notification_enabled=True,
        financial_policy_override=FinancialPolicyOverride.DEFAULT,
    )
    audit_service = AuditService(AuditLogRepository(store_dir))
    holding_decision_service = HoldingDecisionService(
        _PROVIDERS, _CFG, thesis_service, runtime_config_service, audit_service
    )
    holding_decision_result_repo = HoldingDecisionResultRepository(store_dir)
    recommendation_repo = RecommendationRepository(store_dir)
    line_client = _FakeLineClient()
    notification_service = LineNotificationService(
        line_client,
        NotificationLogRepository(store_dir),
        recommendation_repo,
        _CFG,
        audit_service,
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
    )
    return {
        "profit_service": ProfitTakingService(providers=_PROVIDERS, config=_CFG),
        "sell_service": SellSignalService(providers=_PROVIDERS, config=_CFG),
        "holding_decision_service": holding_decision_service,
        "runtime_config_service": runtime_config_service,
        "holding_decision_result_repo": holding_decision_result_repo,
        "recommendation_repo": recommendation_repo,
        "notification_service": notification_service,
        "rule_version_service": RuleVersionService(),
        "line_client": line_client,
    }


def _holding(stock_code: str) -> Holding:
    return Holding(
        stock_code=stock_code,
        stock_name="x",
        shares=100,
        average_purchase_price=Decimal("1000"),
        total_purchase_amount=Decimal("100000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _run(store_dir: Path, mode: RuntimeConfigMode, stock_code: str = "2914"):
    services = _build_services(store_dir, mode)
    result = _analyze_one_holding(
        _holding(stock_code),
        _NOW,
        _PROVIDERS,
        _CFG,
        services["profit_service"],
        services["sell_service"],
        services["holding_decision_service"],
        services["runtime_config_service"],
        services["holding_decision_result_repo"],
        services["recommendation_repo"],
        services["notification_service"],
        services["rule_version_service"],
        None,
        None,
    )
    return result, services


def test_legacy_mode_never_runs_holding_decision_service(store_dir: Path):
    result, services = _run(store_dir, RuntimeConfigMode.LEGACY)
    assert services["holding_decision_result_repo"].list_all() == []
    assert result.succeeded is True


def test_shadow_mode_always_saves_holding_decision_result(store_dir: Path):
    result, services = _run(store_dir, RuntimeConfigMode.SHADOW)
    saved = services["holding_decision_result_repo"].list_all()
    assert len(saved) == 1
    assert result.succeeded is True


def test_active_mode_saves_holding_decision_result_for_general_corporate(store_dir: Path):
    result, services = _run(store_dir, RuntimeConfigMode.ACTIVE)
    saved = services["holding_decision_result_repo"].list_all()
    assert len(saved) == 1
    assert result.succeeded is True


def test_profit_taking_still_functions_across_all_modes(tmp_path: Path):
    """健全な銘柄(売却シグナルが立たない)ではProfitTakingServiceのWATCH等の判定が
    従来どおり機能し続ける(いずれのmodeでも回帰しないこと)。"""
    outcomes = {}
    for mode in (RuntimeConfigMode.LEGACY, RuntimeConfigMode.SHADOW, RuntimeConfigMode.ACTIVE):
        store_dir = tmp_path / mode.value
        store_dir.mkdir()
        result, services = _run(store_dir, mode)
        outcomes[mode] = (
            result.audit.sell_signal_status,
            result.audit.profit_taking_status,
            result.audit.final_recommendation_type,
        )
    # いずれのmodeでも、売却系シグナルが立たない健全銘柄はProfitTakingServiceの
    # 判定(TRIGGERED/NO_SIGNAL)へ同じように到達する(回帰なし)。
    profit_statuses = {v[1] for v in outcomes.values()}
    assert profit_statuses <= {"TRIGGERED", "NO_SIGNAL"}


def test_no_double_notification_across_modes(store_dir: Path):
    """新旧双方が同一サイクルで通知を送ることは無い(recommendation_repoへの
    保存件数が0または1件であることで検証する。ExecutionPlanの不変条件により
    構造的に保証されている)。"""
    for mode in (RuntimeConfigMode.LEGACY, RuntimeConfigMode.SHADOW, RuntimeConfigMode.ACTIVE):
        sub_dir = store_dir / mode.value
        sub_dir.mkdir(parents=True)
        result, services = _run(sub_dir, mode)
        saved_recommendations = services["recommendation_repo"].list_all()
        assert len(saved_recommendations) <= 1


def _fake_sell_recommendation(stock_code: str) -> Recommendation:
    return Recommendation(
        recommendation_id="fake-rec-id",
        stock_code=stock_code,
        stock_name="test",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.SELL,
        price_at_recommendation=Decimal("1000"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1",
    )


def test_kill_switch_on_suppresses_legacy_notification(store_dir: Path, monkeypatch):
    """kill switch ON(notification_enabled=False)の場合、legacyモードで売却
    シグナルが実際に成立していても、そのLINE通知は送信されない
    (実装プラン修正2)。ただしRecommendationの生成・保存自体はkill switch
    中でも継続する(コードレビュー対応: 判定・保存は止めず送信のみ止める)ため、
    「fake-rec-idが保存されていないこと」ではなく「fake-rec-idを含むLINE
    メッセージが送信されていないこと」で検証する。
    """
    services = _build_services(store_dir, RuntimeConfigMode.LEGACY)

    def _fake_analyze(self, holding, now, snapshot=None):
        return SellSignalOutcome(
            holding.stock_code, _fake_sell_recommendation(holding.stock_code), None
        )

    monkeypatch.setattr(SellSignalService, "analyze", _fake_analyze)

    # kill switchをON(notification_enabled=False)にする。
    from jstock_advisor.infrastructure.local_repository import (
        holding_decision_runtime_config_repository as repo,
    )

    repo.update(
        expected_config_version=1,
        mode=RuntimeConfigMode.LEGACY,
        notification_enabled=False,
        financial_policy_override=FinancialPolicyOverride.DEFAULT,
        updated_by="operator",
        change_reason="emergency stop",
        store_dir=store_dir,
    )

    _analyze_one_holding(
        _holding("2914"),
        _NOW,
        _PROVIDERS,
        _CFG,
        services["profit_service"],
        services["sell_service"],
        services["holding_decision_service"],
        services["runtime_config_service"],
        services["holding_decision_result_repo"],
        services["recommendation_repo"],
        services["notification_service"],
        services["rule_version_service"],
        None,
        None,
    )

    saved_ids = {r.recommendation_id for r in services["recommendation_repo"].list_all()}
    assert "fake-rec-id" in saved_ids
    assert not any("fake-rec-id" in msg for msg in services["line_client"].sent_messages)


def test_kill_switch_off_allows_legacy_notification(store_dir: Path, monkeypatch):
    """比較対照: kill switch OFF(notification_enabled=True、既定)であれば
    従来どおり売却推奨が保存・通知される。"""
    services = _build_services(store_dir, RuntimeConfigMode.LEGACY)

    def _fake_analyze(self, holding, now, snapshot=None):
        return SellSignalOutcome(
            holding.stock_code, _fake_sell_recommendation(holding.stock_code), None
        )

    monkeypatch.setattr(SellSignalService, "analyze", _fake_analyze)

    result = _analyze_one_holding(
        _holding("2914"),
        _NOW,
        _PROVIDERS,
        _CFG,
        services["profit_service"],
        services["sell_service"],
        services["holding_decision_service"],
        services["runtime_config_service"],
        services["holding_decision_result_repo"],
        services["recommendation_repo"],
        services["notification_service"],
        services["rule_version_service"],
        None,
        None,
    )

    saved_ids = {r.recommendation_id for r in services["recommendation_repo"].list_all()}
    assert "fake-rec-id" in saved_ids
    assert result.notified is True
