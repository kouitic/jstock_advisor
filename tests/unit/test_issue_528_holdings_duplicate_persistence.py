"""Issue #528(#71 F-D2 + F-C8のD2/D3分): holdingsパス(sell/profit_taking/
holding_decision)の非同期fan-out再配信による判定履歴二重保存を防止する。

D1(BUYパス。buy_candidates_handler.py。PR #385)で確立済みのパターン
(logical execution identity→決定的ID→`insert_if_absent()`による原子的書き込み→
重複時は既存結果として扱う)を、D2(LEGACY_SELL/PROFIT_TAKING)・
D3(HOLDING_DECISION_SCORE/HoldingDecisionResult)へ対称的に適用する。

`handler_module.handler({...batch_id...}, ...)`を同じ(batch_id, holding_id)で
2回呼び、非同期invokeの再試行・二重配信を模す。RecommendationRepository /
HoldingDecisionResultRepositoryは`store_dir=tmp_path`の実物を使い、
`insert_if_absent()`の原子性を実際に確認する(モックのspyではなく実装のstoreで
確認する)。
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.enums import (
    AccountType,
    ExecutionPlanReason,
    HoldingDecisionCategory,
    HoldingDecisionConfidenceLevel,
    RecommendationType,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holding_decision import (
    CompanyQualityScore,
    ComponentCoverage,
    HoldingDecisionExecutionPlan,
    HoldingDecisionHardGate,
    HoldingDecisionResult,
    InvestmentThesisScore,
    RiskDeductionScore,
)
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.holding_decision_result_repository import (
    HoldingDecisionResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers import holdings_watchlist_handler as handler_module
from jstock_advisor.providers.corporate_action.mock_impl import MockCorporateActionProvider
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.dividend_data.mock_impl import MockDividendDataProvider
from jstock_advisor.providers.financial_data.mock_impl import MockFinancialDataProvider
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.shareholder_benefit.mock_impl import MockShareholderBenefitProvider
from jstock_advisor.services.holding_decision_service import HoldingDecisionEvaluationOutcome
from jstock_advisor.services.profit_taking_service import ProfitTakingOutcome
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.sell_signal_service import SellSignalOutcome

_NOW = dt.datetime(2026, 9, 25, 7, 0, tzinfo=dt.UTC)
_STOCK_CODE = "2914"
_HOLDING_ID = build_holding_id(DEFAULT_OWNER, _STOCK_CODE)


class _FakeContext:
    function_name = "jstock-advisor-holdings-watchlist"


class _FakeTradeCooldownService:
    def __init__(self, *a: object, **kw: object) -> None:
        pass

    def detect_and_apply(self, current_holdings: object, now: object) -> object:
        from jstock_advisor.services.trade_cooldown_service import TradeDetectionOutcome

        return TradeDetectionOutcome(confirmed=True, events=[])


def _mock_provider_bundle(now: dt.datetime) -> ProviderBundle:
    """`build_stock_snapshot()`はMock実装を使えば、価格鮮度(実時刻ベースで
    計算される)を含め正しく組み立てられる実StockSnapshotを返す(fair_value_range
    等の属性を手書きフェイクで再現する必要がない)。"""
    return ProviderBundle(
        market_data=MockMarketDataProvider(now=now),
        financial_data=MockFinancialDataProvider(now=now),
        dividend_data=MockDividendDataProvider(now=now),
        shareholder_benefit=MockShareholderBenefitProvider(now=now),
        disclosure=MockDisclosureProvider(now=now),
        corporate_action=MockCorporateActionProvider(),
    )


class _GoldenNotification:
    def notify_data_error(self, *a: object, **kw: object) -> bool:
        return False

    def notify_recommendation_with_status(self, rec: Recommendation, now: dt.datetime) -> object:
        from jstock_advisor.domain.entities.enums import NotificationStatus
        from jstock_advisor.services.line_notification_service import NotificationOutcome

        return NotificationOutcome(
            status=NotificationStatus.SENT, sent=True, data_quality_blocked=False
        )

    def check_data_quality_eligibility(self, rec: Recommendation, now: dt.datetime) -> object:
        from jstock_advisor.domain.entities.notification_eligibility import NotificationEligibility

        return NotificationEligibility(eligible=True)


def _holding(stock_code: str = _STOCK_CODE) -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        stock_name="テスト銘柄",
        shares=300,
        average_purchase_price=Decimal("4000"),
        total_purchase_amount=Decimal("400000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _minimal_recommendation(stock_code: str = _STOCK_CODE) -> Recommendation:
    from jstock_advisor.domain.entities.enums import ConfidenceLevel

    return Recommendation(
        recommendation_id=str(uuid.uuid4()),
        stock_code=stock_code,
        stock_name="テスト銘柄",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
        raw_recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
        price_at_recommendation=Decimal("1400"),
        reasons=["test"],
        rule_version="test-v1",
        confidence=ConfidenceLevel.HIGH,
    )


def _holding_decision_result(stock_code: str = _STOCK_CODE) -> HoldingDecisionResult:
    return HoldingDecisionResult(
        holding_decision_result_id=str(uuid.uuid4()),
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        evaluated_at=_NOW,
        company_quality=CompanyQualityScore(score=30.0, coverage_ratio=1.0),
        investment_thesis=InvestmentThesisScore(score=25.0, coverage_ratio=1.0),
        risk_deduction=RiskDeductionScore(score=10.0, coverage_ratio=1.0),
        base_score=45.0,
        hard_gate=HoldingDecisionHardGate(triggered=False),
        final_score=45.0,
        display_value=45,
        category=HoldingDecisionCategory.SELL_CONSIDERATION,
        coverage=ComponentCoverage(
            overall=1.0, company_quality=1.0, investment_thesis=1.0, risk_deduction=1.0
        ),
        confidence=HoldingDecisionConfidenceLevel.HIGH,
        should_notify=True,
        scoring_model_version=1,
        runtime_config_version=1,
        execution_plan_reason=ExecutionPlanReason.NORMAL_ACTIVE,
    )


_LEGACY_SELL_PLAN = HoldingDecisionExecutionPlan(
    run_legacy_sell_evaluation=True,
    allow_legacy_sell_notification=True,
    run_holding_decision_evaluation=False,
    allow_holding_decision_notification=False,
    execution_reason=ExecutionPlanReason.NORMAL_LEGACY,
)
_PROFIT_TAKING_PLAN = HoldingDecisionExecutionPlan(
    run_legacy_sell_evaluation=False,
    allow_legacy_sell_notification=False,
    run_holding_decision_evaluation=False,
    allow_holding_decision_notification=False,
    run_profit_taking_when_no_sell_notification=True,
    execution_reason=ExecutionPlanReason.NORMAL_LEGACY,
)
_HOLDING_DECISION_NOTIFIED_PLAN = HoldingDecisionExecutionPlan(
    run_legacy_sell_evaluation=False,
    allow_legacy_sell_notification=False,
    run_holding_decision_evaluation=True,
    allow_holding_decision_notification=True,
    run_profit_taking_when_no_sell_notification=False,
    execution_reason=ExecutionPlanReason.NORMAL_ACTIVE,
)
_HOLDING_DECISION_SHADOW_PLAN = HoldingDecisionExecutionPlan(
    run_legacy_sell_evaluation=False,
    allow_legacy_sell_notification=False,
    run_holding_decision_evaluation=True,
    allow_holding_decision_notification=False,
    run_profit_taking_when_no_sell_notification=False,
    execution_reason=ExecutionPlanReason.NORMAL_SHADOW,
)


def _patch_repos(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """handler内部が構築するRecommendationRepository/HoldingDecisionResultRepository
    を、tmp_path上の実store(モックspyではない)へ差し替える。"""

    class _RecoRepoFactory:
        @staticmethod
        def for_execution_context(execution_context: object) -> RecommendationRepository:
            return RecommendationRepository(store_dir=tmp_path)

    monkeypatch.setattr(handler_module, "RecommendationRepository", _RecoRepoFactory)
    monkeypatch.setattr(
        handler_module,
        "HoldingDecisionResultRepository",
        lambda: HoldingDecisionResultRepository(store_dir=tmp_path),
    )


def _patch_common(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "token-value")
    monkeypatch.setenv("LINE_USER_ID", "user-value")
    monkeypatch.setattr(
        handler_module, "build_real_provider_bundle", lambda now, config: _mock_provider_bundle(now)
    )
    monkeypatch.setattr(handler_module, "build_line_client_for_run", lambda **kw: object())
    monkeypatch.setattr(handler_module, "TradeCooldownService", _FakeTradeCooldownService)
    monkeypatch.setattr(
        handler_module, "LineNotificationService", lambda **kw: _GoldenNotification()
    )
    monkeypatch.setattr(handler_module.HoldingRepository, "get", lambda self, hid: _holding())
    monkeypatch.setattr(
        handler_module.HoldingDecisionRuntimeConfigService,
        "get_notification_enabled",
        lambda self: True,
    )
    _patch_repos(monkeypatch, tmp_path)


def _event(batch_id: str | None) -> dict[str, object]:
    event: dict[str, object] = {"task": "holding", "holding_id": _HOLDING_ID}
    if batch_id is not None:
        event["batch_id"] = batch_id
    return event


# --- LEGACY_SELL(D2) ---------------------------------------------------------------


def _patch_legacy_sell(
    monkeypatch: pytest.MonkeyPatch, recommendations: dict[str, Recommendation]
) -> None:
    monkeypatch.setattr(
        handler_module, "resolve_execution_plan", lambda *a, **kw: _LEGACY_SELL_PLAN
    )

    def _analyze(
        self: object, holding: Holding, now: dt.datetime, snapshot: object = None
    ) -> SellSignalOutcome:
        rec = recommendations[holding.stock_code]
        return SellSignalOutcome(stock_code=holding.stock_code, recommendation=rec, data_error=None)

    monkeypatch.setattr(handler_module.SellSignalService, "analyze", _analyze)


def test_n1_legacy_sell_duplicate_delivery_saves_recommendation_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """N1: LEGACY_SELLの同一(batch_id, holding_id)payloadを2回処理しても、
    Recommendationは1件しか保存されない。"""
    _patch_common(monkeypatch, tmp_path)
    _patch_legacy_sell(monkeypatch, {_STOCK_CODE: _minimal_recommendation()})

    for _ in range(2):
        handler_module.handler(_event("batch-528-1"), _FakeContext())

    repo = RecommendationRepository(store_dir=tmp_path)
    saved = [r for r in repo.list_all() if r.stock_code == _STOCK_CODE]
    assert len(saved) == 1


def test_n1b_legacy_sell_decision_snapshot_saved_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """N1続き: DecisionSnapshotの保存呼び出しも1回のみ(重複配信時はスキップ)。"""
    _patch_common(monkeypatch, tmp_path)
    _patch_legacy_sell(monkeypatch, {_STOCK_CODE: _minimal_recommendation()})
    snapshot_calls: list[str] = []
    monkeypatch.setattr(
        handler_module,
        "save_decision_snapshot_safely",
        lambda repo, rec, decision_type, log: snapshot_calls.append(rec.recommendation_id),
    )

    for _ in range(2):
        handler_module.handler(_event("batch-528-1"), _FakeContext())

    assert len(snapshot_calls) == 1


# --- PROFIT_TAKING(D2) --------------------------------------------------------------


def _patch_profit_taking(
    monkeypatch: pytest.MonkeyPatch, recommendations: dict[str, Recommendation]
) -> None:
    monkeypatch.setattr(
        handler_module, "resolve_execution_plan", lambda *a, **kw: _PROFIT_TAKING_PLAN
    )
    monkeypatch.setattr(
        handler_module.SellSignalService,
        "analyze",
        lambda self, holding, now, snapshot=None: SellSignalOutcome(
            stock_code=holding.stock_code, recommendation=None, data_error=None
        ),
    )

    def _analyze(
        self: object, holding: Holding, now: dt.datetime, snapshot: object = None
    ) -> ProfitTakingOutcome:
        rec = recommendations[holding.stock_code]
        return ProfitTakingOutcome(
            stock_code=holding.stock_code, recommendation=rec, data_error=None
        )

    monkeypatch.setattr(handler_module.ProfitTakingService, "analyze", _analyze)


def test_n2_profit_taking_duplicate_delivery_saves_recommendation_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """N2: PROFIT_TAKINGの同一payloadを2回処理してもRecommendationは1件のみ。"""
    _patch_common(monkeypatch, tmp_path)
    _patch_profit_taking(monkeypatch, {_STOCK_CODE: _minimal_recommendation()})

    for _ in range(2):
        handler_module.handler(_event("batch-528-2"), _FakeContext())

    repo = RecommendationRepository(store_dir=tmp_path)
    saved = [r for r in repo.list_all() if r.stock_code == _STOCK_CODE]
    assert len(saved) == 1


# --- HOLDING_DECISION(D3。通知条件を満たす場合) ---------------------------------------


def _patch_holding_decision(
    monkeypatch: pytest.MonkeyPatch,
    plan: HoldingDecisionExecutionPlan,
    results: dict[str, HoldingDecisionResult],
) -> None:
    monkeypatch.setattr(handler_module, "resolve_execution_plan", lambda *a, **kw: plan)
    monkeypatch.setattr(
        handler_module.SellSignalService,
        "analyze",
        lambda self, holding, now, snapshot=None: SellSignalOutcome(
            stock_code=holding.stock_code, recommendation=None, data_error=None
        ),
    )

    def _evaluate(self: object, holding: Holding, now: dt.datetime, *a: object, **kw: object):
        # サブちゃんレビュー(PR #567 F1): 本番のholding_decision_service.py:408は
        # 呼び出しごとに新しいuuid4を発行する。同一インスタンスをそのまま2回返すと、
        # 決定的上書きの有無に関わらずholding_decision_result_idが2回の呼び出しで
        # 偶然一致してしまい(同一object参照のため)、#528の決定的ID化の効果が
        # テストで検証できなくなる。呼び出しごとに新しいuuid4を発行し、本番と
        # 同じ条件(呼び出しごとに異なる素のID)を再現する。
        result = results[holding.stock_code].model_copy(
            update={"holding_decision_result_id": str(uuid.uuid4())}
        )
        return HoldingDecisionEvaluationOutcome(
            stock_code=holding.stock_code, result=result, data_error=None, integrity_error=False
        )

    monkeypatch.setattr(handler_module.HoldingDecisionService, "evaluate", _evaluate)


def test_n3_holding_decision_notified_duplicate_delivery_saves_all_three_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """N3: HOLDING_DECISION(通知あり)の同一payloadを2回処理しても、
    Recommendation・HoldingDecisionResultとも1件のみ(DecisionSnapshotの1件は
    N1b/N2で契約を確認済みのためここでは重複しない)。"""
    _patch_common(monkeypatch, tmp_path)
    _patch_holding_decision(
        monkeypatch, _HOLDING_DECISION_NOTIFIED_PLAN, {_STOCK_CODE: _holding_decision_result()}
    )

    for _ in range(2):
        handler_module.handler(_event("batch-528-3"), _FakeContext())

    reco_repo = RecommendationRepository(store_dir=tmp_path)
    hd_repo = HoldingDecisionResultRepository(store_dir=tmp_path)
    assert len([r for r in reco_repo.list_all() if r.stock_code == _STOCK_CODE]) == 1
    assert len([r for r in hd_repo.list_all() if r.stock_code == _STOCK_CODE]) == 1


def test_n5_holding_decision_recommendation_id_still_linked(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """test 5(#351契約): 通知条件を満たすHOLDING_DECISION経路で、保存された
    Recommendationのrecommendation_idと、保存されたHoldingDecisionResultの
    recommendation_idが一致する(値が決定的になっても紐付けは保たれる)。"""
    _patch_common(monkeypatch, tmp_path)
    _patch_holding_decision(
        monkeypatch, _HOLDING_DECISION_NOTIFIED_PLAN, {_STOCK_CODE: _holding_decision_result()}
    )

    handler_module.handler(_event("batch-528-linked"), _FakeContext())

    reco_repo = RecommendationRepository(store_dir=tmp_path)
    hd_repo = HoldingDecisionResultRepository(store_dir=tmp_path)
    [saved_reco] = [r for r in reco_repo.list_all() if r.stock_code == _STOCK_CODE]
    [saved_hd] = [r for r in hd_repo.list_all() if r.stock_code == _STOCK_CODE]
    assert saved_hd.recommendation_id == saved_reco.recommendation_id


# --- N4: HOLDING_DECISION(通知なし=SHADOW既定) ---------------------------------------


def test_n4_holding_decision_not_notified_duplicate_delivery_saves_result_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """N4: 通知条件を満たさない(SHADOW既定。should_notify=Falseまたは
    allow_holding_decision_notification=False)場合でも、HoldingDecisionResult
    自体は`run_holding_decision_evaluation=True`である限り保存される
    (11節)。同一payloadを2回処理してもHoldingDecisionResultは1件のみ。"""
    _patch_common(monkeypatch, tmp_path)
    result = _holding_decision_result()
    _patch_holding_decision(monkeypatch, _HOLDING_DECISION_SHADOW_PLAN, {_STOCK_CODE: result})

    for _ in range(2):
        handler_module.handler(_event("batch-528-4"), _FakeContext())

    hd_repo = HoldingDecisionResultRepository(store_dir=tmp_path)
    saved = [r for r in hd_repo.list_all() if r.stock_code == _STOCK_CODE]
    assert len(saved) == 1
    # 通知なし経路のためRecommendationは生成されない。
    reco_repo = RecommendationRepository(store_dir=tmp_path)
    assert [r for r in reco_repo.list_all() if r.stock_code == _STOCK_CODE] == []


# --- N5/N6/N7: 別batch_id・別holding_id・別engineでcollisionしない -----------------------


def test_n5_different_batch_id_produces_different_recommendation_and_is_saved(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """N5: 別のbatch_id(=別日の正当な再評価)は別idのまま両方保存される。

    ★ 期待値を`_deterministic_recommendation_id()`自身から作らない
    (D1のREVIEWER FINDING F2対応と同じ注意)。SAVED_COUNTと
    「2件のidが互いに異なること」という、生成式を参照しないobservableな
    事実だけを固定する。
    """
    _patch_common(monkeypatch, tmp_path)
    _patch_legacy_sell(monkeypatch, {_STOCK_CODE: _minimal_recommendation()})

    for batch_id in ("batch-528-a", "batch-528-b"):
        handler_module.handler(_event(batch_id), _FakeContext())

    repo = RecommendationRepository(store_dir=tmp_path)
    saved = [r for r in repo.list_all() if r.stock_code == _STOCK_CODE]
    assert len(saved) == 2
    assert saved[0].recommendation_id != saved[1].recommendation_id


def test_n6_different_holding_id_does_not_collide(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """N6: 同一batch内の別holding_id(別owner・別stock_code)は別idになり、
    どちらも保存される(重複としてスキップされない)。"""
    _patch_common(monkeypatch, tmp_path)
    other_stock = "8306"
    recommendations = {
        _STOCK_CODE: _minimal_recommendation(_STOCK_CODE),
        other_stock: _minimal_recommendation(other_stock),
    }
    monkeypatch.setattr(
        handler_module, "resolve_execution_plan", lambda *a, **kw: _LEGACY_SELL_PLAN
    )

    def _analyze(
        self: object, holding: Holding, now: dt.datetime, snapshot: object = None
    ) -> SellSignalOutcome:
        return SellSignalOutcome(
            stock_code=holding.stock_code,
            recommendation=recommendations[holding.stock_code],
            data_error=None,
        )

    monkeypatch.setattr(handler_module.SellSignalService, "analyze", _analyze)

    def _get(self: object, holding_id: str) -> Holding:
        stock_code = (
            other_stock
            if holding_id == build_holding_id(DEFAULT_OWNER, other_stock)
            else _STOCK_CODE
        )
        return _holding(stock_code)

    monkeypatch.setattr(handler_module.HoldingRepository, "get", _get)

    for stock_code in (_STOCK_CODE, other_stock):
        event = {
            "task": "holding",
            "holding_id": build_holding_id(DEFAULT_OWNER, stock_code),
            "batch_id": "batch-528-same",
        }
        handler_module.handler(event, _FakeContext())

    repo = RecommendationRepository(store_dir=tmp_path)
    saved = repo.list_all()
    assert len(saved) == 2
    assert {r.stock_code for r in saved} == {_STOCK_CODE, other_stock}
    assert len({r.recommendation_id for r in saved}) == 2


def test_n7_engine_does_not_collide_recommendation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """N7: 同一(batch_id, holding_id)でも、engineが異なればrecommendation_idは
    異なる(3 engineが同じrecommendation_idを共有しない)。"""
    ids = {
        engine: handler_module._deterministic_recommendation_id("batch-x", _HOLDING_ID, engine)
        for engine in ("LEGACY_SELL", "PROFIT_TAKING", "HOLDING_DECISION")
    }
    assert len(set(ids.values())) == 3


def test_holding_decision_result_id_does_not_collide_with_recommendation_id() -> None:
    """holding_decision_result_idのnamespace/prefixがrecommendation_id側と
    重ならないことを確認する(異なるprefix文字列を使っているため)。"""
    reco_id = handler_module._deterministic_recommendation_id(
        "batch-x", _HOLDING_ID, "HOLDING_DECISION"
    )
    hd_id = handler_module._deterministic_holding_decision_result_id("batch-x", _HOLDING_ID)
    assert reco_id != hd_id


# --- batch_id=None: 挙動不変 ---------------------------------------------------------


def test_batch_id_none_keeps_analyze_assigned_recommendation_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """batch_id=None(白箱テスト等の呼び出し元)は従来どおりanalyze()が
    割り当てたrecommendation_idのまま変更しない(挙動不変)。"""
    _patch_common(monkeypatch, tmp_path)
    fixed_recommendation = _minimal_recommendation()
    _patch_legacy_sell(monkeypatch, {_STOCK_CODE: fixed_recommendation})

    handler_module.handler(_event(None), _FakeContext())

    repo = RecommendationRepository(store_dir=tmp_path)
    assert repo.get(fixed_recommendation.recommendation_id) is not None


# --- N8: negative verification(mutation) -------------------------------------------


def test_n8_reverting_to_uuid4_and_save_changes_the_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """N8: 修正前ロジック(recommendation_idを毎回uuid4で新規発行し、plainな
    save()を使う)へ一時的に戻すと、同一(batch_id, holding_id)を2回処理した
    ケースでSAVED_COUNT==2(赤)になることを固定する(是正前の実装を反証に含める)。
    """
    _patch_common(monkeypatch, tmp_path)

    def _pre_fix_notify_legacy_sell_and_build_result(
        holding,
        now,
        recommendation,
        recommendation_repo,
        notification_service,
        notification_enabled,
        execution_context=handler_module._DEFAULT_EXECUTION_CONTEXT,
    ):
        # 修正前: recommendation_idの決定化なし・plainなsave()(#528以前の実装)。
        if not execution_context.is_validation:
            recommendation_repo.save(recommendation)
        outcome = handler_module._send_or_suppress_notification(
            recommendation, notification_enabled, notification_service, now
        )
        from jstock_advisor.domain.entities.enums import EvaluationStatus
        from jstock_advisor.domain.entities.evaluation_audit import HoldingEvaluationAudit

        audit = HoldingEvaluationAudit(
            stock_code=holding.stock_code,
            evaluated_at=now,
            evaluation_status=EvaluationStatus.COMPLETED,
            raw_sell_recommendation_type=recommendation.raw_recommendation_type,
            raw_profit_recommendation_type=None,
            final_recommendation_type=recommendation.recommendation_type,
            notification_status=outcome.status,
            notification_suppression_reason=None,
            sell_signal_status="TRIGGERED",
            profit_taking_status="NOT_EVALUATED",
            fair_value_status="NOT_AVAILABLE",
            data_quality_status="OK",
            confidence=recommendation.confidence,
            error_code=None,
        )
        return handler_module._HoldingResult(
            recommended=True,
            notified=outcome.sent,
            succeeded=True,
            category=handler_module.summary_category(audit),
            audit=audit,
            recommendation_id=recommendation.recommendation_id,
        )

    monkeypatch.setattr(
        handler_module,
        "_notify_legacy_sell_and_build_result",
        _pre_fix_notify_legacy_sell_and_build_result,
    )
    # 呼び出し元(_analyze_one_holding)側の決定的ID上書きも無効化する
    # (修正前は呼び出し元でも上書きしていなかったため)。
    monkeypatch.setattr(
        handler_module, "_deterministic_recommendation_id", lambda *a, **kw: str(uuid.uuid4())
    )
    _patch_legacy_sell(monkeypatch, {_STOCK_CODE: _minimal_recommendation()})

    for _ in range(2):
        handler_module.handler(_event("batch-528-n8"), _FakeContext())

    repo = RecommendationRepository(store_dir=tmp_path)
    saved = [r for r in repo.list_all() if r.stock_code == _STOCK_CODE]
    assert len(saved) == 2, (
        "修正前ロジックの再現でも1件しか保存されなかった(反証が本Issueの欠陥を"
        "正しく捉えられていない)"
    )


# --- F3(サブちゃんレビュー PR #567): PROFIT_TAKING経路でのbatch_id確定時の -----------
# --- 決定的ID使用を直接固定する(従来はtest_n2のdedup経由でのみ間接確認していた) ---


def test_profit_taking_batch_id_not_none_uses_deterministic_recommendation_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """F3: PROFIT_TAKING経路でbatch_idが確定している場合、保存されるRecommendationの
    recommendation_idは`_deterministic_recommendation_id(batch_id, holding_id,
    "PROFIT_TAKING")`と一致する(analyze()が返した素のIDのままではない)。

    test_n2はこの分岐を通ってdedupの結果(SAVED_COUNT==1)だけを見ており、
    ID自体がPROFIT_TAKING経路で実際に上書きされていることは検証していなかった。
    """
    _patch_common(monkeypatch, tmp_path)
    _patch_profit_taking(monkeypatch, {_STOCK_CODE: _minimal_recommendation()})

    handler_module.handler(_event("batch-528-f3"), _FakeContext())

    repo = RecommendationRepository(store_dir=tmp_path)
    [saved] = [r for r in repo.list_all() if r.stock_code == _STOCK_CODE]
    expected_id = handler_module._deterministic_recommendation_id(
        "batch-528-f3", _HOLDING_ID, "PROFIT_TAKING"
    )
    assert saved.recommendation_id == expected_id


# --- F2(サブちゃんレビュー PR #567): insert_if_absent()をsave()へ戻すと ----------------
# --- (a)例外が_process_single_holdingで飲み込まれ"failed"になる、または ---------------
# --- (b)保存件数が増える、のいずれかを検知する回帰テスト(4箇所それぞれ) --------------
#
# save()は既存ID保存時にValueErrorを送出する非upsertの契約であり、insert_if_absent()
# 無しで決定的IDだけを導入すると、2回目の配信は毎回ValueErrorになる。
# _process_single_hodling()のexcept Exceptionがこれを飲み込み、保存件数は1件のまま
# 変わらないため、「保存件数==1」だけを見るテスト(N1〜N4)ではこの回帰を検知できない
# (2回目の呼び出し結果がfailedになっていることを別途確認する必要がある)。


def _revert_insert_if_absent_to_save(monkeypatch: pytest.MonkeyPatch, repo_class: type) -> None:
    def _reverted(self: object, item: object) -> bool:
        # #528以前の実装(insert_if_absent()を使わないplainなsave())を再現する。
        self.save(item)
        return True

    monkeypatch.setattr(repo_class, "insert_if_absent", _reverted)


def test_f2_legacy_sell_reverting_to_save_fails_second_delivery_instead_of_duplicating(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """F2-1(LEGACY_SELL): `_notify_legacy_sell_and_build_result`のinsert_if_absent()を
    save()相当へ戻すと、2回目の配信はValueErrorになり"failed"扱いになる
    (保存件数が2件に増えるのではない)。"""
    _patch_common(monkeypatch, tmp_path)
    _patch_legacy_sell(monkeypatch, {_STOCK_CODE: _minimal_recommendation()})
    _revert_insert_if_absent_to_save(monkeypatch, RecommendationRepository)

    first = handler_module.handler(_event("batch-528-f2-legacy"), _FakeContext())
    second = handler_module.handler(_event("batch-528-f2-legacy"), _FakeContext())

    assert not first.get("failed")
    assert second.get("failed") is True
    repo = RecommendationRepository(store_dir=tmp_path)
    assert len([r for r in repo.list_all() if r.stock_code == _STOCK_CODE]) == 1


def test_f2_profit_taking_reverting_to_save_fails_second_delivery_instead_of_duplicating(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """F2-2(PROFIT_TAKING): 同様に、PROFIT_TAKING経路のinsert_if_absent()を
    save()相当へ戻すと2回目は"failed"になる。"""
    _patch_common(monkeypatch, tmp_path)
    _patch_profit_taking(monkeypatch, {_STOCK_CODE: _minimal_recommendation()})
    _revert_insert_if_absent_to_save(monkeypatch, RecommendationRepository)

    first = handler_module.handler(_event("batch-528-f2-pt"), _FakeContext())
    second = handler_module.handler(_event("batch-528-f2-pt"), _FakeContext())

    assert not first.get("failed")
    assert second.get("failed") is True
    repo = RecommendationRepository(store_dir=tmp_path)
    assert len([r for r in repo.list_all() if r.stock_code == _STOCK_CODE]) == 1


def test_f2_holding_decision_notified_reverting_to_save_fails_second_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """F2-3(HOLDING_DECISION、通知あり): `_notify_holding_decision_and_build_result`
    側のRecommendation保存のinsert_if_absent()をsave()相当へ戻すと、2回目は
    "failed"になる(この経路ではrecommendation保存がHoldingDecisionResult保存より先に
    実行されるため、HoldingDecisionResult側は2回目も保存されないまま1件で止まる)。"""
    _patch_common(monkeypatch, tmp_path)
    _patch_holding_decision(
        monkeypatch, _HOLDING_DECISION_NOTIFIED_PLAN, {_STOCK_CODE: _holding_decision_result()}
    )
    _revert_insert_if_absent_to_save(monkeypatch, RecommendationRepository)

    first = handler_module.handler(_event("batch-528-f2-hd"), _FakeContext())
    second = handler_module.handler(_event("batch-528-f2-hd"), _FakeContext())

    assert not first.get("failed")
    assert second.get("failed") is True
    reco_repo = RecommendationRepository(store_dir=tmp_path)
    hd_repo = HoldingDecisionResultRepository(store_dir=tmp_path)
    assert len([r for r in reco_repo.list_all() if r.stock_code == _STOCK_CODE]) == 1
    assert len([r for r in hd_repo.list_all() if r.stock_code == _STOCK_CODE]) == 1


def test_f2_holding_decision_result_reverting_to_save_fails_second_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """F2-4(HoldingDecisionResult): SHADOW計画(Recommendation非生成)でHoldingDecision
    Result自体のinsert_if_absent()をsave()相当へ戻すと、2回目は"failed"になる
    (この計画ではRecommendation側は関与しないため、HoldingDecisionResultの
    insert_if_absent()単独の必須性を切り離して確認できる)。"""
    _patch_common(monkeypatch, tmp_path)
    result = _holding_decision_result()
    _patch_holding_decision(monkeypatch, _HOLDING_DECISION_SHADOW_PLAN, {_STOCK_CODE: result})
    _revert_insert_if_absent_to_save(monkeypatch, HoldingDecisionResultRepository)

    first = handler_module.handler(_event("batch-528-f2-hdr"), _FakeContext())
    second = handler_module.handler(_event("batch-528-f2-hdr"), _FakeContext())

    assert not first.get("failed")
    assert second.get("failed") is True
    hd_repo = HoldingDecisionResultRepository(store_dir=tmp_path)
    assert len([r for r in hd_repo.list_all() if r.stock_code == _STOCK_CODE]) == 1


# --- HoldingDecisionResultRepository.insert_if_absent()単体(D3。新規追加メソッド) ---


def test_holding_decision_result_repository_insert_if_absent_is_atomic(tmp_path) -> None:
    """`HoldingDecisionResultRepository.insert_if_absent()`(#528で新規追加)が、
    RecommendationRepository.insert_if_absent()と同じ意味(同一IDの2回目はFalse・
    既存の値を変更しない)を持つことを、repository単体で直接固定する。"""
    repo = HoldingDecisionResultRepository(store_dir=tmp_path)
    result = _holding_decision_result()

    first = repo.insert_if_absent(result)
    second = repo.insert_if_absent(result)

    assert first is True
    assert second is False
    assert len(repo.list_all()) == 1


def test_holding_decision_result_repository_insert_if_absent_does_not_overwrite(tmp_path) -> None:
    """2回目の呼び出しが、既存の値を上書きしない(base_score等が変わっていても
    最初に保存した値のまま)ことを確認する。"""
    repo = HoldingDecisionResultRepository(store_dir=tmp_path)
    original = _holding_decision_result()
    changed = original.model_copy(update={"base_score": 999.0})
    # 同一IDのまま内容だけ変える(呼び出し元が再計算した別インスタンスを渡す想定)。
    changed = changed.model_copy(
        update={"holding_decision_result_id": original.holding_decision_result_id}
    )

    repo.insert_if_absent(original)
    repo.insert_if_absent(changed)

    saved = repo.get(original.holding_decision_result_id)
    assert saved is not None
    assert saved.base_score == original.base_score
    assert saved.base_score != 999.0
