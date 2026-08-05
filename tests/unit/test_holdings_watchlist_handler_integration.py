"""holdings_watchlist_handler(_analyze_one_holding)のmode×通知有無マトリクス
結合テスト(実装プラン20節「回帰」・修正4)。

mode=legacy/shadow/active・金融業・kill switch・新方式例外(DATA_INTEGRITY_ERROR)
の組み合わせで、新旧どちらのエンジンが実際に通知を送るか/送らないかを
LINE送信メッセージ・recommendation_repo・holding_decision_result_repoの
実際の保存内容から直接検証する(_run()や差分計算に頼らず、各観点を
1テスト1アサーションの単位で明示する)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.classification.financial_industry import IndustryClassificationResult
from jstock_advisor.domain.entities.enums import (
    AccountType,
    ConfidenceLevel,
    ExecutionPlanReason,
    FinancialIndustryCategory,
    FinancialPolicyOverride,
    IndustryClassification,
    RecommendationType,
    RuntimeConfigMode,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holding_decision import (
    CompanyQualityScore,
    ComponentCoverage,
    HoldingDecisionHardGate,
    InvestmentThesisScore,
    RiskDeductionScore,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.holding_decision_score import combine_holding_decision
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.infrastructure.local_repository.holding_decision_result_repository import (
    HoldingDecisionResultRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers import holdings_watchlist_handler as handler_module
from jstock_advisor.lambda_handlers.holdings_watchlist_handler import _analyze_one_holding
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.holding_decision_runtime_config_service import (
    HoldingDecisionRuntimeConfigService,
)
from jstock_advisor.services.holding_decision_service import (
    HoldingDecisionEvaluationOutcome,
    HoldingDecisionService,
)
from jstock_advisor.services.investment_thesis_service import InvestmentThesisService
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.rule_version_service import RuleVersionService
from jstock_advisor.services.sell_signal_service import SellSignalOutcome, SellSignalService

_CFG = load_config()
_RULES = _CFG.holding_decision
_NOW = dt.datetime.now(dt.UTC)
_PROVIDERS = build_mock_provider_bundle(_NOW)
_STOCK_CODE = "2914"


class _FakeLineClient:
    def __init__(self) -> None:
        self.sent_messages: list[str] = []

    def push_message(self, message: str) -> None:
        self.sent_messages.append(message)


def _holding(stock_code: str = _STOCK_CODE) -> Holding:
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


def _fake_sell_recommendation(stock_code: str) -> Recommendation:
    return Recommendation(
        recommendation_id="legacy-fake-rec",
        stock_code=stock_code,
        stock_name="test",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.SELL,
        price_at_recommendation=Decimal("1000"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1",
    )


def _notifying_holding_decision_result(stock_code: str):
    """should_notify=Trueの正規のHoldingDecisionResultを組み立てる(実際の
    combine_holding_decision()で算出するため、should_notifyの論理式は
    本物のドメインロジックのまま)。"""
    from jstock_advisor.domain.entities.holding_decision import HoldingDecisionResult

    q = CompanyQualityScore(score=10, coverage_ratio=1.0)
    i = InvestmentThesisScore(score=10, coverage_ratio=1.0)
    r = RiskDeductionScore(score=70, coverage_ratio=1.0)  # base = 10+10-70 = -50
    gate = HoldingDecisionHardGate(triggered=False)
    outcome = combine_holding_decision(q, i, r, gate, _RULES)
    assert outcome.should_notify is True  # このヘルパーの前提を自己検証しておく

    return HoldingDecisionResult(
        holding_decision_result_id="new-fake-result",
        holding_id=stock_code,
        stock_code=stock_code,
        evaluated_at=_NOW,
        company_quality=q,
        investment_thesis=i,
        risk_deduction=r,
        base_score=outcome.base_score,
        hard_gate=outcome.hard_gate,
        final_score=outcome.final_score,
        display_value=outcome.display_value,
        category=outcome.category,
        coverage=ComponentCoverage(
            overall=1.0, company_quality=1.0, investment_thesis=1.0, risk_deduction=1.0
        ),
        confidence=outcome.confidence,
        should_notify=outcome.should_notify,
        scoring_model_version=_RULES.scoring_model_version,
        runtime_config_version=1,
        execution_plan_reason=ExecutionPlanReason.NORMAL_ACTIVE,
    )


def _build_services(store_dir: Path, mode: RuntimeConfigMode, notification_enabled: bool = True):
    thesis_service = InvestmentThesisService(store_dir=store_dir)
    runtime_config_service = HoldingDecisionRuntimeConfigService(store_dir=store_dir)
    runtime_config_service.init_config(
        "tester",
        mode=mode,
        notification_enabled=notification_enabled,
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
        line_client, NotificationLogRepository(store_dir), recommendation_repo, _CFG, audit_service
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


def _run(services: dict, stock_code: str = _STOCK_CODE):
    return _analyze_one_holding(
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


def _saved_recommendation_ids(services: dict) -> set[str]:
    return {r.recommendation_id for r in services["recommendation_repo"].list_all()}


# ===== mode=legacy: 旧方式通知あり・新方式通知なし =====


def test_legacy_mode_legacy_notifies(store_dir: Path, monkeypatch):
    services = _build_services(store_dir, RuntimeConfigMode.LEGACY)
    monkeypatch.setattr(
        SellSignalService,
        "analyze",
        lambda self, holding, now, snapshot=None: SellSignalOutcome(
            holding.stock_code, _fake_sell_recommendation(holding.stock_code), None
        ),
    )
    _run(services)
    assert "legacy-fake-rec" in _saved_recommendation_ids(services)
    assert len(services["line_client"].sent_messages) == 1


def test_legacy_mode_new_engine_never_notifies(store_dir: Path, monkeypatch):
    """mode=legacyではHoldingDecisionServiceが一切実行されないため、新方式が
    通知することは構造的にあり得ない(evaluate自体が呼ばれない)。"""
    services = _build_services(store_dir, RuntimeConfigMode.LEGACY)
    called = {"count": 0}

    def _spy_evaluate(self, *args, **kwargs):
        called["count"] += 1
        return HoldingDecisionEvaluationOutcome(
            _STOCK_CODE, _notifying_holding_decision_result(_STOCK_CODE)
        )

    monkeypatch.setattr(HoldingDecisionService, "evaluate", _spy_evaluate)
    _run(services)
    assert called["count"] == 0
    assert services["holding_decision_result_repo"].list_all() == []


# ===== mode=shadow: 新方式保存あり・新方式通知なし・旧方式通知あり =====


def test_shadow_mode_new_engine_result_is_saved(store_dir: Path):
    services = _build_services(store_dir, RuntimeConfigMode.SHADOW)
    _run(services)
    assert len(services["holding_decision_result_repo"].list_all()) == 1


def test_shadow_mode_new_engine_never_notifies_even_when_should_notify_true(
    store_dir: Path, monkeypatch
):
    """shadowモードでは新方式のshould_notify=Trueであっても、通知は送られず
    保存のみされる(ExecutionPlan.allow_holding_decision_notification=False)。"""
    services = _build_services(store_dir, RuntimeConfigMode.SHADOW)

    def _fake_evaluate(self, *args, **kwargs):
        return HoldingDecisionEvaluationOutcome(
            _STOCK_CODE, _notifying_holding_decision_result(_STOCK_CODE)
        )

    monkeypatch.setattr(HoldingDecisionService, "evaluate", _fake_evaluate)
    _run(services)
    assert "new-fake-result" not in _saved_recommendation_ids(services)


def test_shadow_mode_legacy_notifies(store_dir: Path, monkeypatch):
    services = _build_services(store_dir, RuntimeConfigMode.SHADOW)
    monkeypatch.setattr(
        SellSignalService,
        "analyze",
        lambda self, holding, now, snapshot=None: SellSignalOutcome(
            holding.stock_code, _fake_sell_recommendation(holding.stock_code), None
        ),
    )
    _run(services)
    assert "legacy-fake-rec" in _saved_recommendation_ids(services)
    # 新旧同時通知は起きない(legacyのみが通知した1件)。
    assert len(services["recommendation_repo"].list_all()) == 1


# ===== mode=active(一般事業会社): 新方式通知あり・旧方式通知なし =====


def test_active_mode_general_corporate_new_engine_notifies(store_dir: Path, monkeypatch):
    services = _build_services(store_dir, RuntimeConfigMode.ACTIVE)

    def _fake_evaluate(self, *args, **kwargs):
        return HoldingDecisionEvaluationOutcome(
            _STOCK_CODE, _notifying_holding_decision_result(_STOCK_CODE)
        )

    monkeypatch.setattr(HoldingDecisionService, "evaluate", _fake_evaluate)
    result = _run(services)
    assert result.notified is True
    assert len(services["line_client"].sent_messages) == 1


def test_active_mode_general_corporate_legacy_never_notifies(store_dir: Path, monkeypatch):
    """mode=active(一般事業会社)ではSellSignalService.analyze自体が呼ばれない
    (run_legacy_sell_evaluation=False)。"""
    services = _build_services(store_dir, RuntimeConfigMode.ACTIVE)
    called = {"count": 0}

    def _spy_analyze(self, holding, now, snapshot=None):
        called["count"] += 1
        return SellSignalOutcome(holding.stock_code, None, None)

    monkeypatch.setattr(SellSignalService, "analyze", _spy_analyze)
    _run(services)
    assert called["count"] == 0


# ===== 金融業: activeでも旧方式のまま =====


def test_active_mode_financial_industry_stays_on_legacy(store_dir: Path, monkeypatch):
    """業種分類が金融業の場合、mode=activeでも新方式は通知を担当せず、旧方式が
    引き続き通知する(専用モデル未整備のため、実装プラン2節)。"""
    services = _build_services(store_dir, RuntimeConfigMode.ACTIVE)
    monkeypatch.setattr(
        handler_module,
        "classify_industry",
        lambda sector, industry: IndustryClassificationResult(
            IndustryClassification.FINANCIAL, FinancialIndustryCategory.BANKING
        ),
    )
    monkeypatch.setattr(
        SellSignalService,
        "analyze",
        lambda self, holding, now, snapshot=None: SellSignalOutcome(
            holding.stock_code, _fake_sell_recommendation(holding.stock_code), None
        ),
    )

    def _fake_evaluate(self, *args, **kwargs):
        return HoldingDecisionEvaluationOutcome(
            _STOCK_CODE, _notifying_holding_decision_result(_STOCK_CODE)
        )

    monkeypatch.setattr(HoldingDecisionService, "evaluate", _fake_evaluate)

    _run(services)

    assert "legacy-fake-rec" in _saved_recommendation_ids(services)
    assert "new-fake-result" not in _saved_recommendation_ids(services)
    # 新方式もshadow相当で実行・保存はされる(将来のモデル検証データ蓄積のため)。
    assert len(services["holding_decision_result_repo"].list_all()) == 1


# ===== kill switch: ONでは新旧どちらも通知しない =====


def test_kill_switch_on_suppresses_new_engine_notification_in_active_mode(
    store_dir: Path, monkeypatch
):
    """kill switch ONの場合、新方式のshould_notify=Trueな評価結果があっても
    それが通知に変換されることはない。ProfitTakingService(kill switch対象外)が
    別途独立に通知することはあり得るため、「新方式由来の推奨が保存・通知
    されていないこと」で厳密に検証する(_saved_recommendation_idsで確認)。
    """
    services = _build_services(store_dir, RuntimeConfigMode.ACTIVE, notification_enabled=False)

    def _fake_evaluate(self, *args, **kwargs):
        return HoldingDecisionEvaluationOutcome(
            _STOCK_CODE, _notifying_holding_decision_result(_STOCK_CODE)
        )

    monkeypatch.setattr(HoldingDecisionService, "evaluate", _fake_evaluate)
    _run(services)
    assert "new-fake-result" not in _saved_recommendation_ids(services)
    # 判定・記録自体は継続する(空振りにならない)。
    assert len(services["holding_decision_result_repo"].list_all()) == 1


def test_kill_switch_on_suppresses_legacy_notification_in_shadow_mode(store_dir: Path, monkeypatch):
    services = _build_services(store_dir, RuntimeConfigMode.SHADOW, notification_enabled=False)
    monkeypatch.setattr(
        SellSignalService,
        "analyze",
        lambda self, holding, now, snapshot=None: SellSignalOutcome(
            holding.stock_code, _fake_sell_recommendation(holding.stock_code), None
        ),
    )
    _run(services)
    assert "legacy-fake-rec" not in _saved_recommendation_ids(services)


# ===== 新方式例外(DATA_INTEGRITY_ERROR): フォールバックし、バッチは継続する =====


def test_new_engine_data_integrity_error_does_not_raise_and_marks_failure(
    store_dir: Path, monkeypatch
):
    """HoldingDecisionService.evaluate()がintegrity_error=Trueを返した場合
    (Baseline不整合等)、例外を送出せず「処理失敗」として結果を返す(実装
    プランのDATA_INTEGRITY_ERROR設計)。呼び出し元(_process_single_holding)は
    1銘柄の失敗で他銘柄の処理を止めない設計だが、ここでは_analyze_one_holding
    自体が例外を投げずに完了することを直接確認する。"""
    services = _build_services(store_dir, RuntimeConfigMode.ACTIVE)

    def _fake_evaluate(self, *args, **kwargs):
        return HoldingDecisionEvaluationOutcome(_STOCK_CODE, None, integrity_error=True)

    monkeypatch.setattr(HoldingDecisionService, "evaluate", _fake_evaluate)

    result = _run(services)  # 例外を送出しないことそのものが検証対象

    assert result.succeeded is False
    assert services["line_client"].sent_messages == []


def test_batch_continues_after_one_holding_hits_data_integrity_error(store_dir: Path, monkeypatch):
    """1銘柄がDATA_INTEGRITY_ERRORで失敗しても、後続の別銘柄は正常に処理が
    継続できる(バッチ全体が停止しない=フォールバック方式であることの確認)。"""
    services = _build_services(store_dir, RuntimeConfigMode.ACTIVE)
    call_count = {"n": 0}

    def _fake_evaluate(self, holding, *args, **kwargs):
        call_count["n"] += 1
        if holding.stock_code == _STOCK_CODE:
            return HoldingDecisionEvaluationOutcome(holding.stock_code, None, integrity_error=True)
        return HoldingDecisionEvaluationOutcome(
            holding.stock_code, _notifying_holding_decision_result(holding.stock_code)
        )

    monkeypatch.setattr(HoldingDecisionService, "evaluate", _fake_evaluate)

    first = _run(services, stock_code=_STOCK_CODE)
    second = _run(services, stock_code="9861")

    assert first.succeeded is False
    assert second.succeeded is True
    assert call_count["n"] == 2
