import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.buy_candidate_evaluation_record import (
    BuyCandidateEvaluationRecord,
    build_evaluation_id,
)
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import (
    AccountType,
    BuyAction,
    BuyIndustrySector,
    CandidateSource,
    ConfidenceLevel,
    EligibilityBlockCategory,
    ExecutionMode,
    NotificationMode,
    PortfolioValuationBasis,
    PurchaseCategory,
    RecommendationType,
    WatchType,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.notification_eligibility import NotificationEligibility
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.domain.signals.add_on_risk import evaluate_add_on_eligibility
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.infrastructure.local_repository.buy_candidate_evaluation_record_repository import (  # noqa: E501
    BuyCandidateEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    VALIDATION_FILE_NAME,
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers import buy_candidates_handler as handler_module
from jstock_advisor.services.audit_service import AuditService as RealAuditService

_NOW = dt.datetime(2026, 7, 29, 7, 0, tzinfo=dt.UTC)
_CONFIG = load_config()


class _FakeContext:
    function_name = "jstock-advisor-buy-candidates"


def _watchlist_item(stock_code: str, stock_name: str | None = None) -> WatchlistItem:
    return WatchlistItem(
        stock_code=stock_code,
        stock_name=stock_name or f"銘柄{stock_code}",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _holding(
    stock_code: str,
    shares: int = 100,
    average_purchase_price: str = "1000",
    owner: str = DEFAULT_OWNER,
) -> Holding:
    price = Decimal(average_purchase_price)
    return Holding(
        owner=owner,
        holding_id=build_holding_id(owner, stock_code),
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        shares=shares,
        average_purchase_price=price,
        total_purchase_amount=price * shares,
        first_purchase_date=_NOW.date(),
        last_purchase_date=_NOW.date(),
        account_type=AccountType.NISA,
        created_at=_NOW,
        updated_at=_NOW,
    )


class _FakeTradeCooldownService:
    """本物はデフォルトでは本番のHoldingsSnapshotRepository(data/local_store配下)
    を読み書きするため、テストがPortfolioService.list_holdings()をモックしていても
    実データを汚染してしまう。ハンドラのディスパッチ入口テストでは常にこちらへ
    差し替え、副作用なしでconfirmed=Trueを返す(§5-1のfail-closed経路は
    line_notification_serviceのテストで別途検証済み)。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def detect_and_apply(self, current_holdings: object, now: object) -> object:
        from jstock_advisor.services.trade_cooldown_service import TradeDetectionOutcome

        return TradeDetectionOutcome(confirmed=True, events=[])


_FAKE_SNAPSHOT_PRICE = Decimal("1000")


def _fake_snapshot(
    *,
    current_price: str = str(_FAKE_SNAPSHOT_PRICE),
    industry: str | None = "Auto Parts",
    sector: str | None = "Consumer Cyclical",
) -> SimpleNamespace:
    """portfolio exposure fact生成に必要な最小限のsnapshotダブル(Issue #82)。

    exposure factはscreening/analysisより前に生成されるため、これらの属性は
    どの判定結果を模したテストでも必要になる。
    """
    return SimpleNamespace(
        current_price=Decimal(current_price),
        financial=SimpleNamespace(industry=industry, sector=sector),
        stock_type_classification=SimpleNamespace(types=[]),
    )


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler_module, "build_real_provider_bundle", lambda now, config: object())
    monkeypatch.setattr(handler_module, "build_line_client_from_env", lambda: object())
    monkeypatch.setattr(
        handler_module, "build_stock_snapshot", lambda *a, **kw: (_fake_snapshot(), None)
    )
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: _NoopAuditService())
    monkeypatch.setattr(handler_module, "TradeCooldownService", _FakeTradeCooldownService)
    monkeypatch.setattr(
        handler_module,
        "LineNotificationService",
        lambda **kwargs: type(
            "_Svc",
            (),
            {"notify_data_error": lambda self, *a, **kw: False},
        )(),
    )


class _NoopAuditService:
    def record(self, *args: object, **kwargs: object) -> None:
        return None


class _RecordingAuditService:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def record(
        self,
        decision_type: str,
        stock_code: str | None = None,
        input_values: dict[str, object] | None = None,
        calculation_formulas: dict[str, object] | None = None,
        output_values: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        self.records.append(
            {
                "decision_type": decision_type,
                "stock_code": stock_code,
                "input_values": input_values or {},
                "output_values": output_values or {},
            }
        )

    def records_by_type(self, decision_type: str) -> list[dict[str, object]]:
        return [r for r in self.records if r["decision_type"] == decision_type]


def _patch_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handler_module, "build_stock_snapshot", lambda *a, **kw: (_fake_snapshot(), None)
    )


def _patch_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: _NoopAuditService())


def test_dispatch_mode_dispatches_one_call_per_unified_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    items = [_watchlist_item("2914"), _watchlist_item("8136")]
    monkeypatch.setattr(handler_module.WatchlistService, "list_items", lambda self: items)
    monkeypatch.setattr(handler_module.PortfolioService, "list_holdings", lambda self: [])

    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        handler_module,
        "dispatch_async",
        lambda function_name, payload: dispatched.append({"fn": function_name, **payload}),
    )

    result = handler_module.handler({}, _FakeContext())

    assert result == {"dispatched": 2}
    batch_ids = {d["batch_id"] for d in dispatched}
    assert len(batch_ids) == 1

    def _without_batch_id(payload: dict[str, object]) -> dict[str, object]:
        return {k: v for k, v in payload.items() if k != "batch_id"}

    stripped = [_without_batch_id(d) for d in dispatched]
    assert {
        "fn": "jstock-advisor-buy-candidates",
        "task": "buy_candidate",
        "stock_code": "2914",
        "source": "WATCHLIST",
        "holding_quantity": None,
        "average_acquisition_price": None,
        "execution_mode": "NORMAL",
        "trade_detection_confirmed": True,
    } in stripped
    assert {
        "fn": "jstock-advisor-buy-candidates",
        "task": "buy_candidate",
        "stock_code": "8136",
        "source": "WATCHLIST",
        "holding_quantity": None,
        "average_acquisition_price": None,
        "execution_mode": "NORMAL",
        "trade_detection_confirmed": True,
    } in stripped


def test_dispatch_mode_merges_same_stock_code_into_both_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一銘柄がウォッチリストと保有銘柄の両方に登録されている場合、
    1回だけdispatchされsource=BOTHになる(要求仕様§2)。"""
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        handler_module.WatchlistService, "list_items", lambda self: [_watchlist_item("2914")]
    )
    monkeypatch.setattr(
        handler_module.PortfolioService, "list_holdings", lambda self: [_holding("2914")]
    )

    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        handler_module,
        "dispatch_async",
        lambda function_name, payload: dispatched.append(payload),
    )

    result = handler_module.handler({}, _FakeContext())

    assert result == {"dispatched": 1}
    assert len(dispatched) == 1
    payload = dispatched[0]
    assert payload["stock_code"] == "2914"
    assert payload["source"] == "BOTH"
    assert payload["holding_quantity"] == 100
    assert payload["average_acquisition_price"] == "1000"


def test_dispatch_mode_holding_only_stock_dispatched_as_holding_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(handler_module.WatchlistService, "list_items", lambda self: [])
    monkeypatch.setattr(
        handler_module.PortfolioService,
        "list_holdings",
        lambda self: [_holding("7203", shares=200)],
    )

    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        handler_module,
        "dispatch_async",
        lambda function_name, payload: dispatched.append(payload),
    )

    handler_module.handler({}, _FakeContext())

    assert len(dispatched) == 1
    assert dispatched[0]["source"] == "HOLDING"
    assert dispatched[0]["holding_quantity"] == 200


def test_dispatch_mode_respects_include_watchlist_and_include_holdings_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        handler_module.WatchlistService, "list_items", lambda self: [_watchlist_item("2914")]
    )
    monkeypatch.setattr(
        handler_module.PortfolioService, "list_holdings", lambda self: [_holding("7203")]
    )
    config = _CONFIG.model_copy(
        update={
            "notification": _CONFIG.notification.model_copy(update={"include_holdings": False})
        }
    )
    monkeypatch.setattr(handler_module, "load_config", lambda: config)

    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        handler_module,
        "dispatch_async",
        lambda function_name, payload: dispatched.append(payload),
    )

    handler_module.handler({}, _FakeContext())

    assert len(dispatched) == 1
    assert dispatched[0]["stock_code"] == "2914"


def test_task_buy_candidate_processes_only_requested_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(handler_module.WatchlistService, "get_item", lambda self, code: None)

    class _FakeOutcome:
        data_error = "テストエラー"
        recommendation = None
        buy_action = None
        ranking_group = None

    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: _FakeOutcome()
    )

    result = handler_module.handler(
        {"task": "buy_candidate", "stock_code": "2914", "source": "WATCHLIST"}, _FakeContext()
    )

    assert result == {"stock_code": "2914", "recommended": False, "notified": False}


def _make_recommendation(
    stock_code: str,
    company_quality_score: float,
    recommendation_id: str = "rec-1",
    buy_action: BuyAction = BuyAction.BUY,
    purchase_attractiveness_score: float = 50.0,
    current_vs_entry_price_pct: str | None = None,
    candidate_source: CandidateSource | None = None,
    buy_industry_sector: BuyIndustrySector | None = None,
    current_market_value: str | None = None,
    holding_quantity: int | None = None,
    average_acquisition_price: str | None = None,
    conflicting_holding_action: RecommendationType | None = None,
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            entry=PriceWithRationale(price=Decimal("3500"), rationale="x"),
            standard=PriceWithRationale(price=Decimal("3300"), rationale="x"),
            strong=PriceWithRationale(price=Decimal("3100"), rationale="x"),
        ),
        price_at_recommendation=Decimal("4200"),
        total_score=company_quality_score,
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
        buy_action=buy_action,
        base_buy_action=buy_action,
        company_quality_score=company_quality_score,
        purchase_attractiveness_score=purchase_attractiveness_score,
        current_vs_entry_price_pct=Decimal(current_vs_entry_price_pct)
        if current_vs_entry_price_pct is not None
        else None,
        candidate_source=candidate_source,
        buy_industry_sector=buy_industry_sector,
        current_market_value=Decimal(current_market_value)
        if current_market_value is not None
        else None,
        holding_quantity=holding_quantity,
        average_acquisition_price=Decimal(average_acquisition_price)
        if average_acquisition_price is not None
        else None,
        conflicting_holding_action=conflicting_holding_action,
    )


def _outcome(recommendation: Recommendation, ranking_group: str | None):
    from jstock_advisor.services.buy_signal_service import BuyAnalysisOutcome

    return BuyAnalysisOutcome(
        stock_code=recommendation.stock_code,
        recommendation=recommendation,
        screening_passed=True,
        exclusion_reasons=[],
        data_error=None,
        buy_action=recommendation.buy_action,
        ranking_group=ranking_group,
    )


class _FakeNotificationServiceForRanking:
    """check_data_quality_eligibility/check_resend_eligibility/
    notify_buy_candidates_digest/notify_batch_summaryの呼び出しを記録する
    フェイク(統合BUY候補パイプライン2026-07向けに刷新)。
    """

    def __init__(
        self,
        data_quality_by_stock: dict[str, NotificationEligibility] | None = None,
        resend_by_stock: dict[str, NotificationEligibility] | None = None,
        send_result: dict[str, str] | None = None,
    ) -> None:
        self._data_quality_by_stock = data_quality_by_stock or {}
        self._resend_by_stock = resend_by_stock or {}
        self._send_result = send_result
        self.digest_calls: list[list[Recommendation]] = []
        self.batch_summary_calls: list[dict[str, object]] = []
        self.data_quality_calls_context: list[object] = []

    def check_data_quality_eligibility(
        self, recommendation: Recommendation, now: dt.datetime, context: object | None = None
    ) -> NotificationEligibility:
        self.data_quality_calls_context.append(context)
        return self._data_quality_by_stock.get(
            recommendation.stock_code, NotificationEligibility(eligible=True)
        )

    def check_resend_eligibility(
        self, recommendation: Recommendation, now: dt.datetime
    ) -> NotificationEligibility:
        return self._resend_by_stock.get(
            recommendation.stock_code, NotificationEligibility(eligible=True)
        )

    def check_trade_cooldown_eligibility(
        self, recommendation: Recommendation, now: dt.datetime
    ) -> NotificationEligibility:
        # BUY候補裾野拡大機能(2026-08)。既存テストはクールダウン中の銘柄を
        # 持たないため通常呼ばれないが、フェイクの完全性のため常に許可を返す。
        return NotificationEligibility(eligible=True)

    def check_cross_pipeline_priority_eligibility(
        self, recommendation: Recommendation, now: dt.datetime
    ) -> NotificationEligibility:
        # cross-pipeline重複抑止(コードレビュー対応2026-08、指摘5)。既存テストは
        # 優先度競合を再現しないため常に許可を返す。
        return NotificationEligibility(eligible=True)

    def send_recommendation_notification(
        self, recommendation: Recommendation, now: dt.datetime
    ) -> None:
        self.digest_calls.append([recommendation])

    def notify_buy_candidates_digest(
        self, winners: list[Recommendation], now: dt.datetime, *, batch_id: str | None = None
    ) -> dict[str, str]:
        self.digest_calls.append(list(winners))
        if self._send_result is not None:
            return {code: self._send_result.get(code, "SENT_AND_RECORDED") for code in
                     [r.stock_code for r in winners]}
        return {r.stock_code: "SENT_AND_RECORDED" for r in winners}

    def notify_data_error(self, *args: object, **kwargs: object) -> bool:
        return False

    def notify_batch_summary(
        self,
        process_name: str,
        total: int,
        category_counts: dict[str, int],
        now: dt.datetime,
        data_insufficient_stock_codes: list[str] | None = None,
        failed_stock_codes: list[str] | None = None,
        buy_candidates_sent_count: int | None = None,
        near_buy_sent_count: int | None = None,
        send_empty_summary: bool = True,
        purchase_judgment_counts: dict[str, int] | None = None,
        notification_result_counts: dict[str, int] | None = None,
        **_kwargs: object,
    ) -> bool:
        self.batch_summary_calls.append(
            {
                "total": total,
                "category_counts": dict(category_counts),
                "buy_candidates_sent_count": buy_candidates_sent_count,
                "near_buy_sent_count": near_buy_sent_count,
                "send_empty_summary": send_empty_summary,
                "purchase_judgment_counts": (
                    dict(purchase_judgment_counts) if purchase_judgment_counts is not None else None
                ),
                "notification_result_counts": (
                    dict(notification_result_counts)
                    if notification_result_counts is not None
                    else None
                ),
            }
        )
        return True


def test_process_single_candidate_defers_send_and_records_buy_ranking_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """買い候補判定が成立しても、ワーカー単体ではLINE送信せず、ランキング候補として
    登録するだけであることを確認する。"""
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    recommendation = _make_recommendation(
        "2914", company_quality_score=72.5, recommendation_id="rec-1", buy_action=BuyAction.BUY
    )
    outcome = _outcome(recommendation, ranking_group="buy_candidate")
    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome
    )
    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)

    result = handler_module._process_single_candidate(
        "2914", CandidateSource.WATCHLIST, None, None, None, _NOW, object(), _CONFIG,
        object(), repo, fake_service,
    )

    assert result == {"stock_code": "2914", "recommended": True, "notified": False}
    assert fake_service.digest_calls == []
    assert repo.get("rec-1") is not None


def test_process_single_candidate_watch_price_counted_without_ranking_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """価格待ち(WATCH_FOR_PRICE、watch_type未設定=買い間近ではない)はLINE通知対象外
    のため、件数のみ集計しランキング登録は行わない。買い候補サマリー表示改修
    (2026-08)により、集計カテゴリは"watch_wait"(買い待ち)に細分化されている。"""
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    recommendation = _make_recommendation(
        "7239",
        company_quality_score=46.4,
        recommendation_id="rec-2",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        current_vs_entry_price_pct="19.7",
    )
    outcome = _outcome(recommendation, ranking_group="watch_price")
    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome
    )
    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda batch_id, category, ranking_entry=None, **kwargs: captured.update(
            category=category, ranking_entry=ranking_entry
        ),
    )

    handler_module._process_single_candidate(
        "7239", CandidateSource.WATCHLIST, None, None, "batch-1", _NOW, object(), _CONFIG,
        object(), repo, fake_service,
    )

    assert captured["category"] == "watch_wait"
    assert captured["ranking_entry"] is None


def test_process_single_candidate_review_when_manual_review_action(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """buy_action=MANUAL_REVIEW(整合性検証違反)の場合、reviewカテゴリへ振り分ける。"""
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    recommendation = _make_recommendation(
        "2914",
        company_quality_score=72.5,
        recommendation_id="rec-1",
        buy_action=BuyAction.MANUAL_REVIEW,
    )
    outcome = _outcome(recommendation, ranking_group="excluded")
    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome
    )
    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda batch_id, category, ranking_entry=None, **kwargs: captured.update(
            category=category, ranking_entry=ranking_entry
        ),
    )

    handler_module._process_single_candidate(
        "2914", CandidateSource.WATCHLIST, None, None, "batch-1", _NOW, object(), _CONFIG,
        object(), repo, fake_service,
    )

    assert captured["category"] == "review"


# --- 買い候補サマリー表示改修(2026-08): 購入判定7区分の分類 --------------------


def test_process_single_candidate_buy_family_records_buy_candidate_purchase_category(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """STRONG_BUY/BUY/SMALL_ENTRY(ranking_group=buy_candidate)は判定時点で
    PurchaseCategory.BUY_CANDIDATEとして記録される。後続のfinalize時の通知結果
    (送信/抑止/上限超過等)には一切左右されない(判定状態と通知処理状態の分離)。
    """
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    recommendation = _make_recommendation(
        "2914",
        company_quality_score=72.5,
        recommendation_id="rec-1",
        buy_action=BuyAction.STRONG_BUY,
    )
    outcome = _outcome(recommendation, ranking_group="buy_candidate")
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)
    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path / "eval")

    handler_module._process_single_candidate(
        "2914",
        CandidateSource.WATCHLIST,
        None,
        None,
        "batch-1",
        _NOW,
        object(),
        _CONFIG,
        object(),
        repo,
        fake_service,
        handler_module._DEFAULT_EXECUTION_CONTEXT,
        eval_repo,
    )

    record = eval_repo.get(build_evaluation_id("batch-1", "2914"))
    assert record is not None
    assert record.purchase_category == PurchaseCategory.BUY_CANDIDATE
    assert record.final_buy_action == BuyAction.STRONG_BUY


def test_process_single_candidate_near_buy_watch_type_classifies_as_near_buy(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """WATCH_FOR_PRICEかつwatch_type==NEAR_BUYは"near_buy"(買い間近)へ分類される。"""
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    recommendation = _make_recommendation(
        "7239",
        company_quality_score=46.4,
        recommendation_id="rec-2",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        current_vs_entry_price_pct="4.9",
    ).model_copy(update={"watch_type": WatchType.NEAR_BUY})
    outcome = _outcome(recommendation, ranking_group="watch_price")
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)
    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path / "eval")

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda batch_id, category, **kwargs: captured.update(category=category),
    )

    handler_module._process_single_candidate(
        "7239",
        CandidateSource.WATCHLIST,
        None,
        None,
        "batch-1",
        _NOW,
        object(),
        _CONFIG,
        object(),
        repo,
        fake_service,
        handler_module._DEFAULT_EXECUTION_CONTEXT,
        eval_repo,
    )

    assert captured["category"] == "near_buy"
    record = eval_repo.get(build_evaluation_id("batch-1", "7239"))
    assert record is not None
    assert record.purchase_category == PurchaseCategory.NEAR_BUY


def test_process_single_candidate_plain_watch_for_price_classifies_as_watch_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """WATCH_FOR_PRICEでwatch_typeが設定されていない(NEAR_BUYではない)場合は
    "watch_wait"(買い待ち)へ分類される。"""
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    recommendation = _make_recommendation(
        "7239",
        company_quality_score=46.4,
        recommendation_id="rec-2",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        current_vs_entry_price_pct="19.7",
    )
    outcome = _outcome(recommendation, ranking_group="watch_price")
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)
    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path / "eval")

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda batch_id, category, **kwargs: captured.update(category=category),
    )

    handler_module._process_single_candidate(
        "7239",
        CandidateSource.WATCHLIST,
        None,
        None,
        "batch-1",
        _NOW,
        object(),
        _CONFIG,
        object(),
        repo,
        fake_service,
        handler_module._DEFAULT_EXECUTION_CONTEXT,
        eval_repo,
    )

    assert captured["category"] == "watch_wait"
    record = eval_repo.get(build_evaluation_id("batch-1", "7239"))
    assert record is not None
    assert record.purchase_category == PurchaseCategory.WATCH_FOR_PRICE


def test_process_single_candidate_watch_before_earnings_classifies_as_watch_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """WATCH_BEFORE_EARNINGSも"watch_wait"(買い待ち)へ分類される。"""
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    recommendation = _make_recommendation(
        "7239",
        company_quality_score=46.4,
        recommendation_id="rec-2",
        buy_action=BuyAction.WATCH_BEFORE_EARNINGS,
    )
    outcome = _outcome(recommendation, ranking_group="watch_price")
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)
    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path / "eval")

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda batch_id, category, **kwargs: captured.update(category=category),
    )

    handler_module._process_single_candidate(
        "7239",
        CandidateSource.WATCHLIST,
        None,
        None,
        "batch-1",
        _NOW,
        object(),
        _CONFIG,
        object(),
        repo,
        fake_service,
        handler_module._DEFAULT_EXECUTION_CONTEXT,
        eval_repo,
    )

    assert captured["category"] == "watch_wait"
    record = eval_repo.get(build_evaluation_id("batch-1", "7239"))
    assert record is not None
    assert record.purchase_category == PurchaseCategory.WATCH_BEFORE_EARNINGS


def test_process_single_candidate_not_attractive_classifies_identically_for_holding_and_watchlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """保有銘柄(HOLDING)であることは購入判定の分類に一切影響しない。買い対象外
    (NOT_ATTRACTIVE)はウォッチリスト銘柄・保有銘柄のどちらでも同じ"hold"
    カテゴリ・PurchaseCategory.NOT_ATTRACTIVEへ分類され、廃止済みの
    「通知不要（保有継続）」という保有専用区分は存在しない。"""
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    monkeypatch.setattr(
        handler_module.PortfolioService,
        "list_holdings_by_stock",
        lambda self, code: [_holding(code)],
    )
    repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path / "eval")
    fake_service = _FakeNotificationServiceForRanking()

    watchlist_rec = _make_recommendation(
        "7238",
        company_quality_score=30.0,
        recommendation_id="rec-watchlist",
        buy_action=BuyAction.NOT_ATTRACTIVE,
    )
    outcome_watchlist = _outcome(watchlist_rec, ranking_group="excluded")
    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome_watchlist
    )
    captured_watchlist: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda batch_id, category, **kwargs: captured_watchlist.update(category=category),
    )
    handler_module._process_single_candidate(
        "7238",
        CandidateSource.WATCHLIST,
        None,
        None,
        "batch-1",
        _NOW,
        object(),
        _CONFIG,
        object(),
        repo,
        fake_service,
        handler_module._DEFAULT_EXECUTION_CONTEXT,
        eval_repo,
    )

    holding_rec = _make_recommendation(
        "7239",
        company_quality_score=30.0,
        recommendation_id="rec-holding",
        buy_action=BuyAction.NOT_ATTRACTIVE,
        candidate_source=CandidateSource.HOLDING,
        holding_quantity=100,
        average_acquisition_price="900",
    )
    outcome_holding = _outcome(holding_rec, ranking_group="excluded")
    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome_holding
    )
    captured_holding: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda batch_id, category, **kwargs: captured_holding.update(category=category),
    )
    handler_module._process_single_candidate(
        "7239",
        CandidateSource.HOLDING,
        100,
        Decimal("900"),
        "batch-1",
        _NOW,
        object(),
        _CONFIG,
        object(),
        repo,
        fake_service,
        handler_module._DEFAULT_EXECUTION_CONTEXT,
        eval_repo,
    )

    assert captured_watchlist["category"] == captured_holding["category"] == "hold"
    watchlist_record = eval_repo.get(build_evaluation_id("batch-1", "7238"))
    holding_record = eval_repo.get(build_evaluation_id("batch-1", "7239"))
    assert watchlist_record is not None
    assert holding_record is not None
    assert (
        watchlist_record.purchase_category
        == holding_record.purchase_category
        == PurchaseCategory.NOT_ATTRACTIVE
    )


def test_process_single_candidate_excluded_maps_to_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    from jstock_advisor.services.buy_signal_service import BuyAnalysisOutcome

    outcome = BuyAnalysisOutcome(
        stock_code="9861",
        recommendation=None,
        screening_passed=False,
        exclusion_reasons=["総合利回りが基準未満"],
        data_error=None,
        buy_action=BuyAction.EXCLUDED,
        ranking_group="excluded",
    )
    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome
    )
    fake_service = _FakeNotificationServiceForRanking()

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda batch_id, category, **kwargs: captured.update(category=category),
    )

    class _NoSaveRepo:
        def save(self, *_a, **_kw):
            raise AssertionError("EXCLUDEDの場合はRecommendationを保存しないはず")

        def get(self, *_a, **_kw):
            return None

    handler_module._process_single_candidate(
        "9861", CandidateSource.WATCHLIST, None, None, "batch-1", _NOW, object(), _CONFIG,
        object(), _NoSaveRepo(), fake_service,
    )

    assert captured["category"] == "hold"


def test_process_single_candidate_records_evaluation_audit_for_excluded_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BUY系でない候補(EXCLUDED)もunified_buy_candidate_evaluation監査へ
    candidate_source付きで記録される(要求仕様§4)。"""
    _patch_snapshot(monkeypatch)
    from jstock_advisor.services.buy_signal_service import BuyAnalysisOutcome

    outcome = BuyAnalysisOutcome(
        stock_code="9861",
        recommendation=None,
        screening_passed=False,
        exclusion_reasons=["総合利回りが基準未満"],
        data_error=None,
        buy_action=BuyAction.EXCLUDED,
        ranking_group="excluded",
    )
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)
    audit = _RecordingAuditService()
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: audit)
    fake_service = _FakeNotificationServiceForRanking()

    class _NoSaveRepo:
        def save(self, *_a: object, **_kw: object) -> None:
            raise AssertionError("EXCLUDEDの場合はRecommendationを保存しないはず")

        def get(self, *_a: object, **_kw: object) -> None:
            return None

    handler_module._process_single_candidate(
        "9861", CandidateSource.HOLDING, 100, Decimal("1000"), None, _NOW, object(), _CONFIG,
        object(), _NoSaveRepo(), fake_service,
    )

    records = audit.records_by_type("unified_buy_candidate_evaluation")
    assert len(records) == 1
    assert records[0]["stock_code"] == "9861"
    assert records[0]["output_values"]["base_buy_action"] == BuyAction.EXCLUDED.value
    assert records[0]["output_values"]["final_buy_action"] == BuyAction.EXCLUDED.value


def test_process_single_candidate_records_evaluation_audit_for_data_insufficient(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """データ取得エラー(DATA_INSUFFICIENT)もunified_buy_candidate_evaluation
    監査へ記録される(要求仕様§4・§12)。"""
    _patch_snapshot(monkeypatch)
    monkeypatch.setattr(handler_module.WatchlistService, "get_item", lambda self, code: None)

    class _FakeOutcome:
        data_error = "テストエラー"
        recommendation = None
        buy_action = None
        ranking_group = None

    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: _FakeOutcome()
    )
    audit = _RecordingAuditService()
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: audit)
    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)

    handler_module._process_single_candidate(
        "2914", CandidateSource.WATCHLIST, None, None, None, _NOW, object(), _CONFIG,
        object(), repo, fake_service,
    )

    records = audit.records_by_type("unified_buy_candidate_evaluation")
    assert len(records) == 1
    assert records[0]["stock_code"] == "2914"
    assert records[0]["output_values"]["base_buy_action"] == BuyAction.DATA_INSUFFICIENT.value
    assert records[0]["input_values"]["candidate_source"] == CandidateSource.WATCHLIST.value


def test_process_single_candidate_records_evaluation_audit_for_watch_price(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """非BUY系のランキング対象外(WATCH_FOR_PRICE)もunified_rankを持たないまま
    unified_buy_candidate_evaluation監査へ記録される(要求仕様§4)。"""
    _patch_snapshot(monkeypatch)
    recommendation = _make_recommendation(
        "7239",
        company_quality_score=46.4,
        recommendation_id="rec-2",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        current_vs_entry_price_pct="19.7",
    )
    outcome = _outcome(recommendation, ranking_group="watch_price")
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)
    audit = _RecordingAuditService()
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: audit)
    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)

    handler_module._process_single_candidate(
        "7239", CandidateSource.WATCHLIST, None, None, None, _NOW, object(), _CONFIG,
        object(), repo, fake_service,
    )

    records = audit.records_by_type("unified_buy_candidate_evaluation")
    assert len(records) == 1
    assert records[0]["output_values"]["base_buy_action"] == BuyAction.WATCH_FOR_PRICE.value
    assert records[0]["output_values"]["final_buy_action"] == BuyAction.WATCH_FOR_PRICE.value


def _config_with_max_notifications(max_notifications: int):
    return _CONFIG.model_copy(
        update={
            "notification": _CONFIG.notification.model_copy(
                update={"buy_candidate_max_notifications_per_run": max_notifications}
            )
        }
    )


def _config_with_notify_data_errors(notify_data_errors: bool):
    return _CONFIG.model_copy(
        update={
            "notification": _CONFIG.notification.model_copy(
                update={
                    "buy_candidates": _CONFIG.notification.buy_candidates.model_copy(
                        update={"notify_data_errors": notify_data_errors}
                    )
                }
            )
        }
    )


def test_process_single_candidate_data_error_does_not_notify_line_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog
) -> None:
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    monkeypatch.setattr(handler_module.WatchlistService, "get_item", lambda self, code: None)

    class _FakeOutcome:
        data_error = "テストエラー"
        recommendation = None
        buy_action = None
        ranking_group = None

    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: _FakeOutcome()
    )

    class _NotifyDataErrorAssertingService(_FakeNotificationServiceForRanking):
        def notify_data_error(self, *args: object, **kwargs: object) -> bool:
            raise AssertionError("既定ではnotify_data_errorを呼ばないはず")

    fake_service = _NotifyDataErrorAssertingService()
    repo = RecommendationRepository(store_dir=tmp_path)

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda batch_id, category, **kwargs: captured.update(category=category),
    )

    with caplog.at_level("WARNING"):
        result = handler_module._process_single_candidate(
            "2914", CandidateSource.WATCHLIST, None, None, "batch-1", _NOW, object(),
            _config_with_notify_data_errors(False), object(), repo, fake_service,
        )

    assert result == {"stock_code": "2914", "recommended": False, "notified": False}
    assert captured["category"] == "data_insufficient"
    assert "buy_candidate_data_error stock_code=2914" in caplog.text
    assert "テストエラー" in caplog.text


def test_process_single_candidate_data_error_notifies_line_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    monkeypatch.setattr(handler_module.WatchlistService, "get_item", lambda self, code: None)

    class _FakeOutcome:
        data_error = "テストエラー"
        recommendation = None
        buy_action = None
        ranking_group = None

    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: _FakeOutcome()
    )

    calls: list[str] = []

    class _NotifyDataErrorRecordingService(_FakeNotificationServiceForRanking):
        def notify_data_error(self, stock_code: str, *args: object, **kwargs: object) -> bool:
            calls.append(stock_code)
            return False

    fake_service = _NotifyDataErrorRecordingService()
    repo = RecommendationRepository(store_dir=tmp_path)

    handler_module._process_single_candidate(
        "2914", CandidateSource.WATCHLIST, None, None, None, _NOW, object(),
        _config_with_notify_data_errors(True), object(), repo, fake_service,
    )

    assert calls == ["2914"]


def test_process_single_candidate_shares_one_snapshot_across_buy_sell_profit_taking(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """保有銘柄(HOLDING/BOTH)については、購入判定(BuySignalService)と
    売却・利確判定(SellSignalService/ProfitTakingService)が同一の価格スナップ
    ショットを使う(要求仕様§3・§17: 同一銘柄について矛盾した判定が同時に
    成立しないようにするため)。build_stock_snapshotの呼び出しは1銘柄あたり
    1回だけであることも確認する(ポートフォリオ総額算出用の別経路price取得を
    行わない)。
    """
    _patch_audit(monkeypatch)

    snapshot_calls: list[object] = []
    sentinel_snapshot = _fake_snapshot()

    def _fake_build_stock_snapshot(*_a: object, **_kw: object) -> tuple[object, None]:
        snapshot_calls.append(sentinel_snapshot)
        return sentinel_snapshot, None

    monkeypatch.setattr(handler_module, "build_stock_snapshot", _fake_build_stock_snapshot)

    recommendation = _make_recommendation(
        "2914",
        company_quality_score=72.5,
        recommendation_id="rec-1",
        buy_action=BuyAction.BUY,
        buy_industry_sector=BuyIndustrySector.BANK,
        current_market_value="100000",
    )
    outcome = _outcome(recommendation, ranking_group="buy_candidate")
    buy_analyze_snapshots: list[object] = []

    def _fake_buy_analyze(self: object, *_a: object, snapshot: object = None, **_kw: object):
        buy_analyze_snapshots.append(snapshot)
        return outcome

    monkeypatch.setattr(handler_module.BuySignalService, "analyze", _fake_buy_analyze)
    monkeypatch.setattr(
        handler_module.PortfolioService,
        "list_holdings_by_stock",
        lambda self, code: [_holding(code)],
    )

    class _NoSignalOutcome:
        recommendation = None

    sell_analyze_snapshots: list[object] = []

    def _fake_sell_analyze(self: object, holding: object, now: object, snapshot: object = None):
        sell_analyze_snapshots.append(snapshot)
        return _NoSignalOutcome()

    monkeypatch.setattr(handler_module.SellSignalService, "analyze", _fake_sell_analyze)

    profit_analyze_snapshots: list[object] = []

    def _fake_profit_analyze(self: object, holding: object, now: object, snapshot: object = None):
        profit_analyze_snapshots.append(snapshot)
        return _NoSignalOutcome()

    monkeypatch.setattr(handler_module.ProfitTakingService, "analyze", _fake_profit_analyze)

    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)

    handler_module._process_single_candidate(
        "2914", CandidateSource.HOLDING, 100, Decimal("1000"), None, _NOW, object(),
        _CONFIG, object(), repo, fake_service,
    )

    assert len(snapshot_calls) == 1
    assert buy_analyze_snapshots == [sentinel_snapshot]
    assert sell_analyze_snapshots == [sentinel_snapshot]
    assert profit_analyze_snapshots == [sentinel_snapshot]


def _add_ranked_candidate(
    repo: RecommendationRepository,
    ranking_entries: list[str],
    stock_code: str,
    purchase_score: float,
    recommendation_id: str | None = None,
    **kwargs: object,
) -> Recommendation:
    rec = _make_recommendation(
        stock_code,
        company_quality_score=60.0,
        recommendation_id=recommendation_id or f"rec-{stock_code}",
        buy_action=BuyAction.BUY,
        purchase_attractiveness_score=purchase_score,
        **kwargs,
    )
    repo.save(rec)
    ranking_entries.append(handler_module._encode_buy_ranking_entry(rec))
    return rec


def _seed_evaluation_record(
    eval_repo: BuyCandidateEvaluationRecordRepository,
    batch_id: str,
    stock_code: str,
    purchase_category: PurchaseCategory = PurchaseCategory.BUY_CANDIDATE,
    final_buy_action: BuyAction = BuyAction.BUY,
) -> None:
    """_process_single_candidate()が判定時点で作成するのと同じ形のレコードを
    直接投入する(_finalize_batchの単体テストは_process_single_candidateを経由
    しないため、finalize側のupdateが対象を見つけられるよう事前に投入する)。"""
    eval_repo.upsert(
        BuyCandidateEvaluationRecord(
            evaluation_id=build_evaluation_id(batch_id, stock_code),
            batch_id=batch_id,
            stock_code=stock_code,
            evaluated_at=_NOW,
            rule_version="v1-mvp",
            candidate_source=CandidateSource.WATCHLIST,
            purchase_category=purchase_category,
            final_buy_action=final_buy_action,
            raw_buy_action=final_buy_action,
            recommendation_id=f"rec-{stock_code}",
        )
    )


def _progress(
    ranking_entries: list[str],
    total: int,
    category_counts: dict[str, int],
    sector_entries: list[str] | None = None,
    holding_count: int = 0,
    validation_recommendation_ids: list[str] | None = None,
):
    return handler_module.BatchProgress(
        total=total,
        completed=total,
        category_counts=category_counts,
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=ranking_entries,
        sector_entries=sector_entries or [],
        holding_count=holding_count,
        validation_recommendation_ids=validation_recommendation_ids or [],
    )


def test_finalize_batch_ranks_buy_candidates_and_digests_top_n(monkeypatch, tmp_path) -> None:
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)

    buy_specs = [
        ("1111", BuyAction.STRONG_BUY, 90.0, 60.0),
        ("2222", BuyAction.BUY, 30.0, 60.0),
        ("3333", BuyAction.BUY, 60.0, 60.0),
        ("4444", BuyAction.SMALL_ENTRY, 10.0, 60.0),
        ("5555", BuyAction.BUY, 50.0, 60.0),
    ]
    ranking_entries = []
    for i, (code, action, purchase_score, quality_score) in enumerate(buy_specs):
        rec = _make_recommendation(
            code,
            company_quality_score=quality_score,
            recommendation_id=f"buy-{i}",
            buy_action=action,
            purchase_attractiveness_score=purchase_score,
        )
        repo.save(rec)
        ranking_entries.append(handler_module._encode_buy_ranking_entry(rec))

    config = _config_with_max_notifications(2)
    progress = _progress(
        ranking_entries, total=8, category_counts={"candidate_not_ranked": 5, "watch_not_ranked": 3}
    )
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)

    assert len(fake_service.digest_calls) == 1
    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["1111", "3333"]

    call = fake_service.batch_summary_calls[0]
    # 買い候補サマリー表示改修(2026-08): category_countsは判定時点の値
    # (progress.category_counts)をそのまま渡すのみで、finalize側ではもはや
    # 上書き調整しない(通知結果はpurchase_judgment_counts/notification_result_
    # countsで表現する)。上限(2件)を超えた3件はOUTSIDE_TOP_5として
    # notification_limitへ計上される。
    assert call["notification_result_counts"]["sent"] == 2
    assert call["notification_result_counts"]["notification_limit"] == 3
    assert call["category_counts"]["candidate_not_ranked"] == 5
    assert call["category_counts"]["watch_not_ranked"] == 3
    assert call["buy_candidates_sent_count"] == 2


def test_finalize_batch_excludes_suppressed_and_data_quality_blocked_winners(
    monkeypatch, tmp_path
) -> None:
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)

    ranking_entries: list[str] = []
    _add_ranked_candidate(repo, ranking_entries, "1111", 50.0)
    _add_ranked_candidate(repo, ranking_entries, "2222", 49.0)
    _add_ranked_candidate(repo, ranking_entries, "3333", 48.0)

    config = _config_with_max_notifications(5)
    progress = _progress(ranking_entries, total=3, category_counts={"candidate_not_ranked": 3})
    fake_service = _FakeNotificationServiceForRanking(
        resend_by_stock={
            "2222": NotificationEligibility(
                eligible=False,
                block_category=EligibilityBlockCategory.RECENTLY_NOTIFIED,
                block_reason="DUPLICATE_SUPPRESSED",
            )
        },
        data_quality_by_stock={
            "3333": NotificationEligibility(
                eligible=False, block_category=EligibilityBlockCategory.DATA_QUALITY
            )
        },
    )

    handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)

    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["1111"]

    call = fake_service.batch_summary_calls[0]
    assert call["notification_result_counts"]["sent"] == 1
    assert call["notification_result_counts"]["resend_suppressed"] == 1
    assert call["notification_result_counts"]["other_suppressed"] == 1  # 3333: DATA_QUALITY
    assert call["category_counts"]["candidate_not_ranked"] == 3
    assert call["buy_candidates_sent_count"] == 1


def test_finalize_batch_reports_zero_buy_candidates_sent_when_none_ranked(
    monkeypatch, tmp_path
) -> None:
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    progress = _progress([], total=3, category_counts={"hold": 1, "watch_not_ranked": 2})
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, "batch-1", _CONFIG, _NOW, repo, fake_service)

    assert fake_service.batch_summary_calls[0]["buy_candidates_sent_count"] == 0
    assert fake_service.digest_calls == [[]]


def test_finalize_batch_passes_send_empty_summary_from_config(monkeypatch, tmp_path) -> None:
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    progress = _progress([], total=1, category_counts={"hold": 1})
    config = _CONFIG.model_copy(
        update={
            "notification": _CONFIG.notification.model_copy(
                update={"send_empty_summary": False}
            )
        }
    )
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)

    assert fake_service.batch_summary_calls[0]["send_empty_summary"] is False


def test_finalize_batch_promotes_lower_ranked_candidate_when_top_is_suppressed(
    monkeypatch, tmp_path
) -> None:
    """1位が再送抑止で除外されても、下位の適格候補(6位)が繰り上げられ、
    最終的に上限件数(5件)が送信される。"""
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []

    scores = [100.0, 90.0, 80.0, 70.0, 60.0, 50.0]
    codes = ["1st", "2nd", "3rd", "4th", "5th", "6th"]
    for code, score in zip(codes, scores, strict=True):
        _add_ranked_candidate(repo, ranking_entries, code, score)

    config = _config_with_max_notifications(5)
    progress = _progress(ranking_entries, total=6, category_counts={"candidate_not_ranked": 6})
    fake_service = _FakeNotificationServiceForRanking(
        resend_by_stock={
            "1st": NotificationEligibility(
                eligible=False, block_category=EligibilityBlockCategory.RECENTLY_NOTIFIED
            )
        }
    )

    handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)

    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["2nd", "3rd", "4th", "5th", "6th"]

    call = fake_service.batch_summary_calls[0]
    assert call["buy_candidates_sent_count"] == 5
    assert call["notification_result_counts"]["resend_suppressed"] == 1
    assert call["category_counts"]["candidate_not_ranked"] == 6


def test_finalize_batch_sends_exactly_all_eligible_when_fewer_than_max(
    monkeypatch, tmp_path
) -> None:
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    for code, score in [("a", 90.0), ("b", 80.0), ("c", 70.0)]:
        _add_ranked_candidate(repo, ranking_entries, code, score)

    config = _config_with_max_notifications(5)
    progress = _progress(ranking_entries, total=3, category_counts={"candidate_not_ranked": 3})
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)

    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["a", "b", "c"]
    assert fake_service.batch_summary_calls[0]["buy_candidates_sent_count"] == 3


def test_finalize_batch_sends_nothing_when_all_candidates_are_suppressed(
    monkeypatch, tmp_path
) -> None:
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    for code, score in [("a", 90.0), ("b", 80.0)]:
        _add_ranked_candidate(repo, ranking_entries, code, score)

    config = _config_with_max_notifications(5)
    progress = _progress(ranking_entries, total=2, category_counts={"candidate_not_ranked": 2})
    fake_service = _FakeNotificationServiceForRanking(
        resend_by_stock={
            code: NotificationEligibility(
                eligible=False, block_category=EligibilityBlockCategory.RECENTLY_NOTIFIED
            )
            for code in ("a", "b")
        }
    )

    handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)

    assert fake_service.digest_calls == [[]]
    call = fake_service.batch_summary_calls[0]
    assert call["buy_candidates_sent_count"] == 0
    assert call["notification_result_counts"]["resend_suppressed"] == 2


def test_finalize_batch_evaluates_with_buy_candidate_batch_context(monkeypatch, tmp_path) -> None:
    _patch_audit(monkeypatch)
    from jstock_advisor.domain.entities.enums import NotificationContext

    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    _add_ranked_candidate(repo, ranking_entries, "2914", 90.0)

    config = _config_with_max_notifications(5)
    progress = _progress(ranking_entries, total=1, category_counts={"candidate_not_ranked": 1})
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)

    assert fake_service.data_quality_calls_context == [NotificationContext.BUY_CANDIDATE_BATCH]


# --- v3: OUTSIDE_TOP_5は全ゲート通過後にのみ判定 ------------------------------


def test_finalize_batch_outside_top5_only_after_all_gates_pass(monkeypatch, tmp_path) -> None:
    """最大5件到達後でも、6位の候補にデータ品質異常がある場合はOUTSIDE_TOP_5では
    なくDATA_QUALITYとして監査される(v3修正)。"""
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    codes = [f"c{i}" for i in range(1, 7)]
    scores = [90.0 - i for i in range(6)]
    for code, score in zip(codes, scores, strict=True):
        _add_ranked_candidate(repo, ranking_entries, code, score)

    config = _config_with_max_notifications(5)
    progress = _progress(ranking_entries, total=6, category_counts={"candidate_not_ranked": 6})
    fake_service = _FakeNotificationServiceForRanking(
        data_quality_by_stock={
            "c6": NotificationEligibility(
                eligible=False, block_category=EligibilityBlockCategory.DATA_QUALITY
            )
        }
    )

    handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)

    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["c1", "c2", "c3", "c4", "c5"]
    call = fake_service.batch_summary_calls[0]
    # c6はデータ品質でother_suppressedへ計上され、notification_limitではない。
    assert call["notification_result_counts"]["other_suppressed"] == 1
    assert call["notification_result_counts"]["notification_limit"] == 0


def test_finalize_batch_sixth_candidate_passing_all_gates_is_outside_top5(
    monkeypatch, tmp_path
) -> None:
    """全ゲートを通過した6位はOUTSIDE_TOP_5として送信対象から外れる。"""
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    codes = [f"c{i}" for i in range(1, 7)]
    scores = [90.0 - i for i in range(6)]
    for code, score in zip(codes, scores, strict=True):
        _add_ranked_candidate(repo, ranking_entries, code, score)

    config = _config_with_max_notifications(5)
    progress = _progress(ranking_entries, total=6, category_counts={"candidate_not_ranked": 6})
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)

    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["c1", "c2", "c3", "c4", "c5"]
    call = fake_service.batch_summary_calls[0]
    assert call["notification_result_counts"]["notification_limit"] == 1


# --- v3: LINE送信結果の3状態 --------------------------------------------------


def test_finalize_batch_sent_log_failed_raises_for_operational_visibility(
    monkeypatch, tmp_path
) -> None:
    """LINE送信は成功したがNotificationLog保存に失敗した銘柄がある場合、
    Lambda呼び出し自体を失敗させて運用検知させる(v3修正)。"""
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    _add_ranked_candidate(repo, ranking_entries, "2914", 90.0)

    config = _config_with_max_notifications(5)
    progress = _progress(ranking_entries, total=1, category_counts={"candidate_not_ranked": 1})
    fake_service = _FakeNotificationServiceForRanking(
        send_result={"2914": "SENT_LOG_FAILED"}
    )

    with pytest.raises(RuntimeError, match="NotificationLog保存に失敗"):
        handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)


def test_finalize_batch_send_failed_does_not_raise(monkeypatch, tmp_path) -> None:
    """LINE送信自体が失敗した場合(SEND_FAILED)は、未送信として扱い例外を送出
    しない(次回バッチで自然に再評価される)。"""
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    _add_ranked_candidate(repo, ranking_entries, "2914", 90.0)

    config = _config_with_max_notifications(5)
    progress = _progress(ranking_entries, total=1, category_counts={"candidate_not_ranked": 1})
    fake_service = _FakeNotificationServiceForRanking(send_result={"2914": "SEND_FAILED"})

    handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)

    assert fake_service.batch_summary_calls[0]["buy_candidates_sent_count"] == 0


# --- 買い候補サマリー表示改修(2026-08): 購入判定/通知結果の分離 -----------------


def test_finalize_batch_purchase_judgment_and_notification_result_worked_example(
    monkeypatch, tmp_path
) -> None:
    """買い候補サマリー表示改修の代表シナリオ(A〜Hの8件、スコア降順)。
    B・Dが再送抑止され、既定の最大5件(max_notifications=5)到達によりHが
    通知上限で見送られる。purchase_judgment_counts["buy_candidate"]は8件のまま
    (送信可否に一切左右されない)、notification_result_countsは
    sent=5(A,C,E,F,G)/notification_limit=1(H)/resend_suppressed=2(B,D)となり、
    合計は必ずbuy_candidate件数と一致する。Hが最終的にnear_buy/watch_waitへ
    再分類されることも無い。
    """
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path / "eval")
    batch_id = "batch-worked-example"

    labels = ["A", "B", "C", "D", "E", "F", "G", "H"]
    codes = {label: f"91{i:02d}" for i, label in enumerate(labels, start=1)}
    scores = {label: 90.0 - i * 10 for i, label in enumerate(labels)}

    ranking_entries: list[str] = []
    for label in labels:
        _add_ranked_candidate(repo, ranking_entries, codes[label], scores[label])
        _seed_evaluation_record(eval_repo, batch_id, codes[label])

    config = _config_with_max_notifications(5)
    progress = _progress(ranking_entries, total=8, category_counts={"candidate_not_ranked": 8})
    fake_service = _FakeNotificationServiceForRanking(
        resend_by_stock={
            codes["B"]: NotificationEligibility(
                eligible=False,
                block_category=EligibilityBlockCategory.RECENTLY_NOTIFIED,
                block_reason="DUPLICATE_SUPPRESSED",
            ),
            codes["D"]: NotificationEligibility(
                eligible=False,
                block_category=EligibilityBlockCategory.RECENTLY_NOTIFIED,
                block_reason="DUPLICATE_SUPPRESSED",
            ),
        }
    )

    handler_module._finalize_batch(
        progress,
        batch_id,
        config,
        _NOW,
        repo,
        fake_service,
        handler_module._DEFAULT_EXECUTION_CONTEXT,
        eval_repo,
    )

    call = fake_service.batch_summary_calls[0]
    assert call["purchase_judgment_counts"]["buy_candidate"] == 8
    assert call["notification_result_counts"] == {
        "sent": 5,
        "notification_limit": 1,
        "resend_suppressed": 2,
        "addon_blocked": 0,
        "other_suppressed": 0,
        "send_failed": 0,
        "other_error": 0,
        # 通知ドライラン機能(2026-08追加): notification_mode=DRY_RUN専用の独立区分
        # (WOULD_SEND_DRY_RUN)。本テストはVALIDATION+DRY_RUNではなく既定の
        # execution_contextで実行しているため常に0。
        "dry_run_would_send": 0,
    }
    assert sum(call["notification_result_counts"].values()) == call["purchase_judgment_counts"][
        "buy_candidate"
    ]

    digested_codes = {r.stock_code for r in fake_service.digest_calls[0]}
    assert digested_codes == {codes[label] for label in ("A", "C", "E", "F", "G")}

    h_record = eval_repo.get(build_evaluation_id(batch_id, codes["H"]))
    assert h_record is not None
    assert h_record.purchase_category == PurchaseCategory.BUY_CANDIDATE
    assert h_record.notification_block_category == EligibilityBlockCategory.OUTSIDE_TOP_5.value


def test_finalize_batch_notification_result_sum_matches_buy_candidate_count_when_mixed(
    monkeypatch, tmp_path
) -> None:
    """resend抑止以外の組み合わせ(record-not-found・addon-blocked混在)でも、
    notification_result_countsの合計はpurchase_judgment_counts["buy_candidate"]と
    常に一致する(要求仕様の不変条件)。"""
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    batch_id = "batch-mixed"
    ranking_entries: list[str] = []

    # record-not-found: ランキングエントリのみ登録し、Recommendation自体は
    # 保存しない(ワーカー側の保存漏れ・タイミング競合を模擬)。
    missing_rec = _make_recommendation(
        "9301",
        company_quality_score=60.0,
        recommendation_id="rec-missing",
        buy_action=BuyAction.BUY,
        purchase_attractiveness_score=95.0,
    )
    ranking_entries.append(handler_module._encode_buy_ranking_entry(missing_rec))

    # addon-blocked: 保有銘柄でSELLと競合。
    _add_ranked_candidate(
        repo,
        ranking_entries,
        "9302",
        85.0,
        candidate_source=CandidateSource.HOLDING,
        current_market_value="100000",
        holding_quantity=100,
        average_acquisition_price="900",
        conflicting_holding_action=RecommendationType.SELL,
    )

    # 通常送信。
    _add_ranked_candidate(repo, ranking_entries, "9303", 70.0)

    config = _config_with_max_notifications(5)
    progress = _progress(
        ranking_entries,
        total=3,
        category_counts={"candidate_not_ranked": 3},
        sector_entries=[
            handler_module._encode_portfolio_exposure_fact(
                handler_module.PortfolioExposureFact(
                    stock_code="9302",
                    current_market_value=Decimal("100000"),
                    sector=BuyIndustrySector.AUTOMOTIVE_PARTS,
                    sector_availability=handler_module.SectorClassificationAvailability.KNOWN,
                )
            ),
        ],
        holding_count=1,
    )
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, batch_id, config, _NOW, repo, fake_service)

    call = fake_service.batch_summary_calls[0]
    assert call["purchase_judgment_counts"]["buy_candidate"] == 3
    assert call["notification_result_counts"] == {
        "sent": 1,
        "notification_limit": 0,
        "resend_suppressed": 0,
        "addon_blocked": 1,
        "other_suppressed": 0,
        "send_failed": 0,
        "other_error": 1,
        "dry_run_would_send": 0,
    }
    assert sum(call["notification_result_counts"].values()) == call["purchase_judgment_counts"][
        "buy_candidate"
    ]


def test_finalize_batch_addon_downgrade_to_manual_review_does_not_change_purchase_judgment(
    monkeypatch, tmp_path
) -> None:
    """買い増し固有ゲートでbuy_action表示がMANUAL_REVIEWへダウングレードされるのは
    finalize時点のin-memoryな表示上の処理にすぎず、判定時点で既に確定した
    PurchaseCategory.BUY_CANDIDATE・purchase_judgment_counts["buy_candidate"]には
    一切影響しない(要確認(manual_review)へ加算されない)。これは共通購入判断
    そのものがMANUAL_REVIEWだった場合(buy_action=MANUAL_REVIEW、category=
    "review")とは明確に区別される。
    """
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path / "eval")
    batch_id = "batch-addon-downgrade"

    ranking_entries: list[str] = []
    _add_ranked_candidate(
        repo,
        ranking_entries,
        "7239",
        90.0,
        candidate_source=CandidateSource.HOLDING,
        current_market_value="100000",
        holding_quantity=100,
        average_acquisition_price="900",
        conflicting_holding_action=RecommendationType.SELL,
    )
    _seed_evaluation_record(
        eval_repo,
        batch_id,
        "7239",
        purchase_category=PurchaseCategory.BUY_CANDIDATE,
        final_buy_action=BuyAction.BUY,
    )

    config = _config_with_max_notifications(5)
    progress = _progress(
        ranking_entries,
        total=1,
        category_counts={"candidate_not_ranked": 1},
        sector_entries=[
            handler_module._encode_portfolio_exposure_fact(
                handler_module.PortfolioExposureFact(
                    stock_code="7239",
                    current_market_value=Decimal("100000"),
                    sector=BuyIndustrySector.AUTOMOTIVE_PARTS,
                    sector_availability=handler_module.SectorClassificationAvailability.KNOWN,
                )
            ),
        ],
        holding_count=1,
    )
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(
        progress,
        batch_id,
        config,
        _NOW,
        repo,
        fake_service,
        handler_module._DEFAULT_EXECUTION_CONTEXT,
        eval_repo,
    )

    call = fake_service.batch_summary_calls[0]
    assert call["purchase_judgment_counts"]["buy_candidate"] == 1
    assert call["purchase_judgment_counts"]["manual_review"] == 0
    assert call["notification_result_counts"]["addon_blocked"] == 1

    record = eval_repo.get(build_evaluation_id(batch_id, "7239"))
    assert record is not None
    assert record.purchase_category == PurchaseCategory.BUY_CANDIDATE
    assert record.final_buy_action == BuyAction.BUY  # 判定時点の値のまま変わらない
    expected_block_category = EligibilityBlockCategory.CONFLICTING_HOLDING_ACTION.value
    assert record.notification_block_category == expected_block_category


def test_finalize_batch_other_suppressed_categories_remain_individually_identifiable(
    monkeypatch, tmp_path
) -> None:
    """表示上はDATA_QUALITY/TRADE_COOLDOWN/CROSS_PIPELINE_PRIORITYの3種とも
    notification_result_counts["other_suppressed"]へ合算されるが、内部の
    BuyCandidateEvaluationRecord.notification_block_categoryでは個別に識別できる
    (表示側の合算バケット名から内部コードを逆算できないことも合わせて確認する)。
    """
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path / "eval")
    batch_id = "batch-other-suppressed"

    ranking_entries: list[str] = []
    _add_ranked_candidate(repo, ranking_entries, "9401", 90.0)
    _add_ranked_candidate(repo, ranking_entries, "9402", 80.0)
    _add_ranked_candidate(repo, ranking_entries, "9403", 70.0)
    for code in ("9401", "9402", "9403"):
        _seed_evaluation_record(eval_repo, batch_id, code)

    class _MultiGateBlockingService(_FakeNotificationServiceForRanking):
        def check_trade_cooldown_eligibility(self, recommendation, now):
            if recommendation.stock_code == "9402":
                return NotificationEligibility(
                    eligible=False,
                    block_category=EligibilityBlockCategory.TRADE_COOLDOWN,
                    block_reason="TRADE_COOLDOWN_ACTIVE",
                )
            return super().check_trade_cooldown_eligibility(recommendation, now)

        def check_cross_pipeline_priority_eligibility(self, recommendation, now):
            if recommendation.stock_code == "9403":
                return NotificationEligibility(
                    eligible=False,
                    block_category=EligibilityBlockCategory.LOW_PRIORITY,
                    block_reason="DUPLICATE_STOCK_NOTIFICATION",
                )
            return super().check_cross_pipeline_priority_eligibility(recommendation, now)

    fake_service = _MultiGateBlockingService(
        data_quality_by_stock={
            "9401": NotificationEligibility(
                eligible=False, block_category=EligibilityBlockCategory.DATA_QUALITY
            )
        }
    )

    config = _config_with_max_notifications(5)
    progress = _progress(ranking_entries, total=3, category_counts={"candidate_not_ranked": 3})

    handler_module._finalize_batch(
        progress,
        batch_id,
        config,
        _NOW,
        repo,
        fake_service,
        handler_module._DEFAULT_EXECUTION_CONTEXT,
        eval_repo,
    )

    call = fake_service.batch_summary_calls[0]
    assert call["notification_result_counts"]["other_suppressed"] == 3
    assert call["notification_result_counts"]["sent"] == 0

    r1 = eval_repo.get(build_evaluation_id(batch_id, "9401"))
    r2 = eval_repo.get(build_evaluation_id(batch_id, "9402"))
    r3 = eval_repo.get(build_evaluation_id(batch_id, "9403"))
    assert r1 is not None and r2 is not None and r3 is not None
    assert r1.notification_block_category == EligibilityBlockCategory.DATA_QUALITY.value
    assert r2.notification_block_category == EligibilityBlockCategory.TRADE_COOLDOWN.value
    assert r3.notification_block_category == EligibilityBlockCategory.LOW_PRIORITY.value
    block_categories = {
        r1.notification_block_category,
        r2.notification_block_category,
        r3.notification_block_category,
    }
    assert len(block_categories) == 3


def test_finalize_batch_sent_log_failed_counts_as_sent_and_records_send_outcome(
    monkeypatch, tmp_path
) -> None:
    """SENT_LOG_FAILEDはLINE送信自体は成功しているため、表示・件数上は
    「通知済み」(sent)として扱う(「送信失敗」にしない)。内部のBuyCandidate
    EvaluationRecord.send_outcomeでのみSENT_LOG_FAILEDを区別する。既存どおり
    運用検知用のRuntimeErrorは引き続き送出される。"""
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path / "eval")
    batch_id = "batch-log-failed"
    ranking_entries: list[str] = []
    _add_ranked_candidate(repo, ranking_entries, "2914", 90.0)
    _seed_evaluation_record(eval_repo, batch_id, "2914")

    config = _config_with_max_notifications(5)
    progress = _progress(ranking_entries, total=1, category_counts={"candidate_not_ranked": 1})
    fake_service = _FakeNotificationServiceForRanking(send_result={"2914": "SENT_LOG_FAILED"})

    with pytest.raises(RuntimeError, match="NotificationLog保存に失敗"):
        handler_module._finalize_batch(
            progress,
            batch_id,
            config,
            _NOW,
            repo,
            fake_service,
            handler_module._DEFAULT_EXECUTION_CONTEXT,
            eval_repo,
        )

    call = fake_service.batch_summary_calls[0]
    assert call["notification_result_counts"]["sent"] == 1
    assert call["notification_result_counts"]["send_failed"] == 0

    record = eval_repo.get(build_evaluation_id(batch_id, "2914"))
    assert record is not None
    assert record.send_outcome == "SENT_LOG_FAILED"
    assert record.notification_eligible is True


def test_finalize_batch_send_failed_counts_as_send_failed_and_records_send_outcome(
    monkeypatch, tmp_path
) -> None:
    """SEND_FAILED(LINE送信自体の失敗)はnotification_result_counts["send_failed"]
    に計上され、RuntimeErrorは送出しない(次回バッチで自然に再評価される)。"""
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path / "eval")
    batch_id = "batch-send-failed"
    ranking_entries: list[str] = []
    _add_ranked_candidate(repo, ranking_entries, "2914", 90.0)
    _seed_evaluation_record(eval_repo, batch_id, "2914")

    config = _config_with_max_notifications(5)
    progress = _progress(ranking_entries, total=1, category_counts={"candidate_not_ranked": 1})
    fake_service = _FakeNotificationServiceForRanking(send_result={"2914": "SEND_FAILED"})

    handler_module._finalize_batch(
        progress,
        batch_id,
        config,
        _NOW,
        repo,
        fake_service,
        handler_module._DEFAULT_EXECUTION_CONTEXT,
        eval_repo,
    )

    call = fake_service.batch_summary_calls[0]
    assert call["notification_result_counts"]["send_failed"] == 1
    assert call["notification_result_counts"]["sent"] == 0

    record = eval_repo.get(build_evaluation_id(batch_id, "2914"))
    assert record is not None
    assert record.send_outcome == "SEND_FAILED"
    assert record.notification_eligible is False


def test_issue36_finalize_batch_claim_suppressed_counts_as_other_suppressed(
    monkeypatch, tmp_path
) -> None:
    """Issue #36: CLAIM_SUPPRESSED(claim機構による抑止=別実行が送信済み/
    送信中)は「通知済み」(sent)にも「送信失敗」(send_failed)にも計上せず、
    other_suppressedへ計上する。elseフォールスルーで送信失敗扱いになる
    回帰を防ぐ明示分岐の固定。RuntimeErrorも送出しない。"""
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path / "eval")
    batch_id = "batch-claim-suppressed"
    ranking_entries: list[str] = []
    _add_ranked_candidate(repo, ranking_entries, "2914", 90.0)
    _add_ranked_candidate(repo, ranking_entries, "7203", 80.0)
    _seed_evaluation_record(eval_repo, batch_id, "2914")
    _seed_evaluation_record(eval_repo, batch_id, "7203")

    config = _config_with_max_notifications(5)
    progress = _progress(ranking_entries, total=2, category_counts={"candidate_not_ranked": 2})
    fake_service = _FakeNotificationServiceForRanking(
        send_result={"2914": "SENT_AND_RECORDED", "7203": "CLAIM_SUPPRESSED"}
    )

    handler_module._finalize_batch(
        progress,
        batch_id,
        config,
        _NOW,
        repo,
        fake_service,
        handler_module._DEFAULT_EXECUTION_CONTEXT,
        eval_repo,
    )

    call = fake_service.batch_summary_calls[0]
    assert call["notification_result_counts"]["sent"] == 1  # 今回pushした銘柄のみ
    assert call["notification_result_counts"]["send_failed"] == 0  # 送信失敗と誤計上しない
    assert call["notification_result_counts"]["other_suppressed"] == 1
    assert call["buy_candidates_sent_count"] == 1
    assert sum(call["notification_result_counts"].values()) == call["purchase_judgment_counts"][
        "buy_candidate"
    ]  # 全銘柄がいずれかの区分へ過不足なく計上される

    record = eval_repo.get(build_evaluation_id(batch_id, "7203"))
    assert record is not None
    assert record.send_outcome == "CLAIM_SUPPRESSED"
    assert record.notification_eligible is True  # 通知条件自体は通過している
    assert record.notification_rank is None  # 今回pushしていないためrankなし


@pytest.mark.parametrize("outcome_value", ["SENT_AND_RECORDED", "SENT_VALIDATION"])
def test_finalize_batch_sent_outcomes_both_count_as_sent_but_remain_distinguishable(
    monkeypatch, tmp_path, outcome_value: str
) -> None:
    """SENT_AND_RECORDED/SENT_VALIDATIONはどちらもnotification_result_counts
    ["sent"]へ計上されるが、BuyCandidateEvaluationRecord.send_outcomeでは
    引き続き区別できる。"""
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path / "eval")
    batch_id = f"batch-{outcome_value}"
    ranking_entries: list[str] = []
    _add_ranked_candidate(repo, ranking_entries, "2914", 90.0)
    _seed_evaluation_record(eval_repo, batch_id, "2914")

    config = _config_with_max_notifications(5)
    progress = _progress(ranking_entries, total=1, category_counts={"candidate_not_ranked": 1})
    fake_service = _FakeNotificationServiceForRanking(send_result={"2914": outcome_value})

    handler_module._finalize_batch(
        progress,
        batch_id,
        config,
        _NOW,
        repo,
        fake_service,
        handler_module._DEFAULT_EXECUTION_CONTEXT,
        eval_repo,
    )

    call = fake_service.batch_summary_calls[0]
    assert call["notification_result_counts"]["sent"] == 1

    record = eval_repo.get(build_evaluation_id(batch_id, "2914"))
    assert record is not None
    assert record.send_outcome == outcome_value


# --- 保有銘柄の買い増しリスクゲート(統合) -------------------------------------


def test_finalize_batch_blocks_holding_with_conflicting_sell_signal(monkeypatch, tmp_path) -> None:
    """保有銘柄でSELL判定と競合する場合、共通購入判断がBUY系でも通知されない
    (要求仕様§6)。"""
    audit = _RecordingAuditService()
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: audit)
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    _add_ranked_candidate(
        repo,
        ranking_entries,
        "7239",
        90.0,
        candidate_source=CandidateSource.HOLDING,
        current_market_value="100000",
        holding_quantity=100,
        average_acquisition_price="900",
        conflicting_holding_action=RecommendationType.SELL,
    )

    config = _config_with_max_notifications(5)
    progress = _progress(
        ranking_entries,
        total=1,
        category_counts={"candidate_not_ranked": 1},
        sector_entries=[
            handler_module._encode_portfolio_exposure_fact(
                handler_module.PortfolioExposureFact(
                    stock_code="7239",
                    current_market_value=Decimal("100000"),
                    sector=BuyIndustrySector.AUTOMOTIVE_PARTS,
                    sector_availability=handler_module.SectorClassificationAvailability.KNOWN,
                )
            )
        ],
        holding_count=1,
    )
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)

    assert fake_service.digest_calls == [[]]
    outcome_records = audit.records_by_type("unified_buy_candidate_notification_outcome")
    assert len(outcome_records) == 1
    output_values = outcome_records[0]["output_values"]
    expected_category = EligibilityBlockCategory.CONFLICTING_HOLDING_ACTION.value
    assert output_values["block_category"] == expected_category
    assert output_values["block_reason"] == RecommendationType.SELL.value


def test_finalize_batch_allows_holding_with_no_conflict_and_reliable_portfolio_data(
    monkeypatch, tmp_path
) -> None:
    """保有銘柄でも売却競合が無く、ポートフォリオデータが信頼できる(全保有銘柄の
    sector_entriesが揃っている)場合、集中上限内ならBUY系候補として通知される。"""
    audit = _RecordingAuditService()
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: audit)
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    _add_ranked_candidate(
        repo,
        ranking_entries,
        "7239",
        90.0,
        candidate_source=CandidateSource.HOLDING,
        current_market_value="100000",
        holding_quantity=100,
        average_acquisition_price="900",
    )

    config = _config_with_max_notifications(5)
    # 他の保有銘柄(業種は分散)でポートフォリオ総額を薄め、7239の集中比率が
    # 上限(既定20%)を超えないようにする(現実的な複数銘柄ポートフォリオを模擬)。
    progress = _progress(
        ranking_entries,
        total=1,
        category_counts={"candidate_not_ranked": 1},
        sector_entries=[
            handler_module._encode_portfolio_exposure_fact(
                handler_module.PortfolioExposureFact(
                    stock_code="7239",
                    current_market_value=Decimal("100000"),
                    sector=BuyIndustrySector.AUTOMOTIVE_PARTS,
                    sector_availability=handler_module.SectorClassificationAvailability.KNOWN,
                )
            ),
            handler_module._encode_portfolio_exposure_fact(
                handler_module.PortfolioExposureFact(
                    stock_code="8001",
                    current_market_value=Decimal("2500000"),
                    sector=BuyIndustrySector.BANK,
                    sector_availability=handler_module.SectorClassificationAvailability.KNOWN,
                )
            ),
        ],
        holding_count=2,
    )
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)

    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["7239"]
    outcome_records = audit.records_by_type("unified_buy_candidate_notification_outcome")
    assert len(outcome_records) == 1
    output_values = outcome_records[0]["output_values"]
    assert output_values["notification_status"] == "SENT"
    assert output_values["portfolio_valuation_basis"] == PortfolioValuationBasis.MARKET_VALUE.value


def test_finalize_batch_blocks_holding_when_portfolio_data_unreliable(
    monkeypatch, tmp_path
) -> None:
    """sector_entriesが保有銘柄数に対して不足している(=一部の保有銘柄の時価が
    不明)場合、PortfolioValuationBasisがUNAVAILABLEとなり、保有銘柄の買い増し
    通知は行われない(要求仕様§7)。"""
    audit = _RecordingAuditService()
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: audit)
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    _add_ranked_candidate(
        repo,
        ranking_entries,
        "7239",
        90.0,
        candidate_source=CandidateSource.HOLDING,
        current_market_value="100000",
        holding_quantity=100,
        average_acquisition_price="900",
    )

    config = _config_with_max_notifications(5)
    # holding_count=2だがsector_entriesは1件分しか無い(=もう1銘柄の時価が欠落)。
    progress = _progress(
        ranking_entries,
        total=1,
        category_counts={"candidate_not_ranked": 1},
        sector_entries=[
            handler_module._encode_portfolio_exposure_fact(
                handler_module.PortfolioExposureFact(
                    stock_code="7239",
                    current_market_value=Decimal("100000"),
                    sector=BuyIndustrySector.AUTOMOTIVE_PARTS,
                    sector_availability=handler_module.SectorClassificationAvailability.KNOWN,
                )
            )
        ],
        holding_count=2,
    )
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)

    assert fake_service.digest_calls == [[]]
    outcome_records = audit.records_by_type("unified_buy_candidate_notification_outcome")
    assert len(outcome_records) == 1
    output_values = outcome_records[0]["output_values"]
    expected_category = EligibilityBlockCategory.PORTFOLIO_DATA_RELIABILITY.value
    assert output_values["block_category"] == expected_category
    assert output_values["portfolio_valuation_basis"] == PortfolioValuationBasis.UNAVAILABLE.value


def test_finalize_batch_watchlist_only_candidate_skips_addon_gate(monkeypatch, tmp_path) -> None:
    """気になる銘柄単独(source=WATCHLIST)の候補には買い増しリスクゲートを
    適用しない(保有情報が無いため)。"""
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    _add_ranked_candidate(
        repo, ranking_entries, "2914", 90.0, candidate_source=CandidateSource.WATCHLIST
    )

    config = _config_with_max_notifications(5)
    progress = _progress(ranking_entries, total=1, category_counts={"candidate_not_ranked": 1})
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)

    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["2914"]


# --- 保有銘柄であることが優遇/冷遇されないことの確認(ランキング統合) -----------


def test_finalize_batch_does_not_favor_holdings_in_ranking_order(monkeypatch, tmp_path) -> None:
    """保有銘柄であること自体はランキングを優遇・冷遇しない。純粋に
    purchase_attractiveness_scoreの高い順に並ぶ。"""
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    _add_ranked_candidate(
        repo, ranking_entries, "9001", 90.0, candidate_source=CandidateSource.WATCHLIST
    )
    _add_ranked_candidate(
        repo,
        ranking_entries,
        "9002",
        70.0,
        candidate_source=CandidateSource.HOLDING,
        current_market_value="50000",
        holding_quantity=50,
        average_acquisition_price="900",
    )
    _add_ranked_candidate(
        repo, ranking_entries, "9003", 50.0, candidate_source=CandidateSource.WATCHLIST
    )

    config = _config_with_max_notifications(5)
    progress = _progress(
        ranking_entries,
        total=3,
        category_counts={"candidate_not_ranked": 3},
        sector_entries=[
            handler_module._encode_portfolio_exposure_fact(
                handler_module.PortfolioExposureFact(
                    stock_code="9002",
                    current_market_value=Decimal("50000"),
                    sector=BuyIndustrySector.AUTOMOTIVE_PARTS,
                    sector_availability=handler_module.SectorClassificationAvailability.KNOWN,
                )
            ),
            handler_module._encode_portfolio_exposure_fact(
                handler_module.PortfolioExposureFact(
                    stock_code="8001",
                    current_market_value=Decimal("2500000"),
                    sector=BuyIndustrySector.BANK,
                    sector_availability=handler_module.SectorClassificationAvailability.KNOWN,
                )
            ),
        ],
        holding_count=2,
    )
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)

    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["9001", "9002", "9003"]


# --- sector_entries書き込み/読み込み時検証・保有データ整合性の単体テスト
# (統合BUY候補パイプライン2026-07 §4・§5) ---


def _exposure(
    stock_code: str,
    sector: BuyIndustrySector,
    market_value: str,
    *,
    known: bool = True,
) -> handler_module.PortfolioExposureFact:
    return handler_module.PortfolioExposureFact(
        stock_code=stock_code,
        current_market_value=Decimal(market_value),
        sector=sector,
        sector_availability=(
            handler_module.SectorClassificationAvailability.KNOWN
            if known
            else handler_module.SectorClassificationAvailability.UNKNOWN
        ),
    )


def _encode(
    stock_code: str,
    sector: BuyIndustrySector,
    market_value: str,
    *,
    known: bool = True,
) -> str:
    return handler_module._encode_portfolio_exposure_fact(
        _exposure(stock_code, sector, market_value, known=known)
    )


def _encode_v1(sector: BuyIndustrySector, market_value: str, stock_code: str) -> str:
    """Issue #82 以前の3フィールド形式(旧workerが書き込む形式)。"""
    return "|".join([sector.value, market_value, stock_code])


def test_encode_decode_portfolio_exposure_fact_round_trip() -> None:
    fact = _exposure("7203", BuyIndustrySector.BANK, "123456")

    entry = handler_module._encode_portfolio_exposure_fact(fact)

    assert entry == "BANK|123456|7203|KNOWN"
    assert handler_module._decode_portfolio_exposure_fact(entry) == fact


def test_encode_decode_round_trip_preserves_unknown_sector_availability() -> None:
    fact = _exposure("7203", BuyIndustrySector.UNKNOWN, "123456", known=False)

    decoded = handler_module._decode_portfolio_exposure_fact(
        handler_module._encode_portfolio_exposure_fact(fact)
    )

    assert decoded == fact
    assert decoded is not None
    assert decoded.sector_is_known is False


def test_decode_accepts_legacy_v1_entry_from_older_worker() -> None:
    """Issue #82: 旧worker(3フィールド)のエントリを新finalizeが読めること。

    デプロイ跨ぎで旧workerと新workerが混在しても、旧形式のエントリを
    落とさない(落とすとcoverage不足で不必要にfail-closeする)。
    """
    decoded = handler_module._decode_portfolio_exposure_fact(
        _encode_v1(BuyIndustrySector.BANK, "123456", "7203")
    )

    assert decoded == _exposure("7203", BuyIndustrySector.BANK, "123456")


def test_decode_legacy_v1_unknown_sector_is_treated_as_unavailable() -> None:
    """旧形式は可用性フィールドを持たないため、業種値そのものから復元する。"""
    decoded = handler_module._decode_portfolio_exposure_fact(
        _encode_v1(BuyIndustrySector.UNKNOWN, "123456", "7203")
    )

    assert decoded is not None
    assert decoded.sector_is_known is False


def test_decode_rejects_inconsistent_known_claim_for_unknown_sector() -> None:
    """業種UNKNOWNをKNOWNと主張するエントリを受理しない(業種集中度への誤算入防止)。"""
    decoded = handler_module._decode_portfolio_exposure_fact("UNKNOWN|123456|7203|KNOWN")

    assert decoded is not None
    assert decoded.sector_is_known is False


@pytest.mark.parametrize(
    "invalid_entry",
    [
        "BANK|123456",  # 要素数不足
        "BANK|123456|7203|KNOWN|extra",  # 要素数過多
        "NOT_A_SECTOR|123456|7203|KNOWN",  # 不正な業種
        "BANK|not_a_number|7203|KNOWN",  # Decimalとしてパース不可
        "BANK|123456|watch-high|KNOWN",  # 銘柄コードが数字パターンでない
        "BANK|123456|123|KNOWN",  # 銘柄コードが3桁(4〜5桁のパターンに不一致)
        "BANK|123456|7203|NOT_AN_AVAILABILITY",  # 不正な可用性
        "",
    ],
)
def test_decode_portfolio_exposure_fact_returns_none_for_invalid_entries(
    invalid_entry: str,
) -> None:
    assert handler_module._decode_portfolio_exposure_fact(invalid_entry) is None


@pytest.mark.parametrize(
    ("holding_quantity", "average_acquisition_price", "expected"),
    [
        (100, "1000", (False, False)),
        (0, "1000", (True, False)),
        (-100, "1000", (True, False)),
        (None, "1000", (True, False)),
        (100, "0", (True, False)),
        (100, "-500", (True, False)),
        (100, None, (True, False)),
        # 単元未満株(端株)はそれだけではハードブロックしない(is_odd_lot=Trueのみ)
        (150, "1000", (False, True)),
    ],
)
def test_holding_data_consistency(
    holding_quantity: int | None,
    average_acquisition_price: str | None,
    expected: tuple[bool, bool],
) -> None:
    price = Decimal(average_acquisition_price) if average_acquisition_price is not None else None

    result = handler_module._holding_data_consistency(holding_quantity, price, trading_unit=100)

    assert result == expected


def test_aggregate_portfolio_exposure_full_coverage_yields_market_value_basis() -> None:
    entries = [
        _encode("7203", BuyIndustrySector.BANK, "100000"),
        _encode("9001", BuyIndustrySector.AUTOMOTIVE_PARTS, "200000"),
    ]

    exposure = handler_module._aggregate_portfolio_exposure(entries, holding_count=2)

    assert exposure.basis == PortfolioValuationBasis.MARKET_VALUE
    assert exposure.portfolio_total == Decimal("300000")
    assert exposure.sector_totals == {
        BuyIndustrySector.BANK: Decimal("100000"),
        BuyIndustrySector.AUTOMOTIVE_PARTS: Decimal("200000"),
    }
    assert exposure.sector_exposure_complete is True
    assert exposure.coverage_ratio == 1.0


def test_aggregate_portfolio_exposure_price_missing_fails_close_for_both_gates() -> None:
    """時価を報告できない保有銘柄が残る場合はfail-close(0円補完しない)。

    Issue #82 以降、screening除外は欠落の原因ではない(除外銘柄も報告する)。
    ここで欠落するのは現在値・株数そのものを取得できなかった銘柄である。
    """
    entries = [_encode("7203", BuyIndustrySector.BANK, "100000")]

    exposure = handler_module._aggregate_portfolio_exposure(entries, holding_count=2)

    assert exposure.basis == PortfolioValuationBasis.UNAVAILABLE
    assert exposure.portfolio_total is None
    assert exposure.sector_totals == {}
    assert exposure.sector_exposure_complete is False
    assert exposure.coverage_ratio == 0.5


def test_aggregate_portfolio_exposure_includes_screening_excluded_holding() -> None:
    """Issue #82: 保有3銘柄のうち1銘柄がscreening除外でも分母へ3銘柄すべて含める。

    除外銘柄もexposureを報告するため coverage が満たされ、集中度判定が成立する
    (以前は除外1件で `UNAVAILABLE` となり全HOLDING/BOTH候補が停止していた)。
    """
    entries = [
        _encode("7203", BuyIndustrySector.AUTOMOTIVE_PARTS, "100000"),
        _encode("9001", BuyIndustrySector.GENERAL, "200000"),
        # screeningで除外された金融株(#29により毎日除外される)
        _encode("8306", BuyIndustrySector.BANK, "300000"),
    ]

    exposure = handler_module._aggregate_portfolio_exposure(entries, holding_count=3)

    assert exposure.basis == PortfolioValuationBasis.MARKET_VALUE
    assert exposure.portfolio_total == Decimal("600000")
    assert exposure.sector_totals[BuyIndustrySector.BANK] == Decimal("300000")
    assert exposure.sector_exposure_complete is True


def test_aggregate_portfolio_exposure_unknown_sector_keeps_total_but_marks_sector_incomplete(
) -> None:
    """Issue #82: 業種不明は欠落ではない。分母には算入し、業種側だけ不成立とする。"""
    entries = [
        _encode("7203", BuyIndustrySector.BANK, "100000"),
        _encode("9001", BuyIndustrySector.UNKNOWN, "200000", known=False),
    ]

    exposure = handler_module._aggregate_portfolio_exposure(entries, holding_count=2)

    assert exposure.basis == PortfolioValuationBasis.MARKET_VALUE
    assert exposure.portfolio_total == Decimal("300000")
    assert exposure.coverage_ratio == 1.0
    # 業種集中度のみDATA_INSUFFICIENT。
    assert exposure.sector_exposure_complete is False
    # 業種不明を「その他セクター」として集計へ含めない。
    assert BuyIndustrySector.UNKNOWN not in exposure.sector_totals
    assert exposure.sector_totals == {BuyIndustrySector.BANK: Decimal("100000")}


def test_aggregate_portfolio_exposure_accepts_mixed_legacy_and_new_entries() -> None:
    """デプロイ跨ぎ: 旧worker(v1)と新worker(v2)のエントリが混在しても集計できる。"""
    entries = [
        _encode_v1(BuyIndustrySector.BANK, "100000", "7203"),
        _encode("9001", BuyIndustrySector.GENERAL, "200000"),
    ]

    exposure = handler_module._aggregate_portfolio_exposure(entries, holding_count=2)

    assert exposure.basis == PortfolioValuationBasis.MARKET_VALUE
    assert exposure.portfolio_total == Decimal("300000")
    assert exposure.sector_exposure_complete is True


def test_aggregate_portfolio_exposure_conflicting_duplicate_is_unavailable() -> None:
    """同一銘柄で異なる値の複数エントリが存在する場合、信頼性不足として扱う
    (例: 価格再取得やリトライによる不一致)。"""
    entries = [
        _encode("7203", BuyIndustrySector.BANK, "100000"),
        _encode("7203", BuyIndustrySector.BANK, "999999"),
    ]

    exposure = handler_module._aggregate_portfolio_exposure(entries, holding_count=1)

    assert exposure.basis == PortfolioValuationBasis.UNAVAILABLE
    assert exposure.portfolio_total is None
    assert exposure.sector_totals == {}
    assert exposure.sector_exposure_complete is False


def test_aggregate_portfolio_exposure_identical_duplicate_is_not_double_counted() -> None:
    """DynamoDB String Setは完全一致文字列の重複を保持しないが、呼び出し側の
    集計ロジック自体も同一銘柄・同一値の重複入力を二重加算しないことを確認する。
    """
    entry = _encode("7203", BuyIndustrySector.BANK, "100000")

    exposure = handler_module._aggregate_portfolio_exposure([entry, entry], holding_count=1)

    assert exposure.basis == PortfolioValuationBasis.MARKET_VALUE
    assert exposure.portfolio_total == Decimal("100000")
    assert exposure.sector_totals == {BuyIndustrySector.BANK: Decimal("100000")}


def test_aggregate_portfolio_exposure_ignores_invalid_entries_without_crashing() -> None:
    entries = [
        _encode("7203", BuyIndustrySector.BANK, "100000"),
        "garbage|not|valid|entry|at|all",
    ]

    exposure = handler_module._aggregate_portfolio_exposure(entries, holding_count=1)

    # 不正エントリは無視され、有効な1銘柄分だけで完全カバレッジとして扱われる
    assert exposure.basis == PortfolioValuationBasis.MARKET_VALUE
    assert exposure.portfolio_total == Decimal("100000")
    assert exposure.coverage_ratio == 1.0
    assert exposure.sector_totals == {BuyIndustrySector.BANK: Decimal("100000")}


def test_aggregate_portfolio_exposure_zero_holding_count_is_unavailable_without_crashing() -> None:
    exposure = handler_module._aggregate_portfolio_exposure([], holding_count=0)

    assert exposure.basis == PortfolioValuationBasis.UNAVAILABLE
    assert exposure.portfolio_total is None
    assert exposure.sector_totals == {}
    assert exposure.sector_exposure_complete is False
    assert exposure.coverage_ratio == 0.0


def test_aggregate_portfolio_exposure_exceeding_max_is_unavailable_without_crashing() -> None:
    entries = [
        _encode(f"{9000 + i}", BuyIndustrySector.BANK, "1000")
        for i in range(handler_module.MAX_SECTOR_ENTRIES + 1)
    ]

    exposure = handler_module._aggregate_portfolio_exposure(
        entries, holding_count=len(entries)
    )

    assert exposure.basis == PortfolioValuationBasis.UNAVAILABLE
    assert exposure.portfolio_total is None
    assert exposure.sector_totals == {}


def test_build_unified_targets_skips_holdings_when_exceeding_max_sector_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保有銘柄数がMAX_SECTOR_ENTRIESを超える場合、dispatch前にfan-outを行わず
    (保有銘柄側は評価対象へ含めない)、ウォッチリスト側は継続する
    (統合BUY候補パイプライン2026-07 v3 §2)。監査へSECTOR_ENTRIES_LIMIT_EXCEEDED
    を記録する。
    """
    too_many_holdings = [
        _holding(f"{9000 + i}") for i in range(handler_module.MAX_SECTOR_ENTRIES + 1)
    ]
    monkeypatch.setattr(
        handler_module.WatchlistService, "list_items", lambda self: [_watchlist_item("2914")]
    )
    monkeypatch.setattr(
        handler_module.PortfolioService, "list_holdings", lambda self: too_many_holdings
    )
    recorded: list[dict[str, object]] = []

    class _RecordingAudit:
        def record(self, decision_type: str, **kwargs: object) -> None:
            recorded.append({"decision_type": decision_type, **kwargs})

    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: _RecordingAudit())

    targets = handler_module._build_unified_targets(_CONFIG, _NOW)

    assert len(targets) == 1
    assert targets[0].stock_code == "2914"
    assert targets[0].source == CandidateSource.WATCHLIST
    aborted = [r for r in recorded if r["decision_type"] == "unified_buy_candidate_batch_aborted"]
    assert len(aborted) == 1
    assert aborted[0]["output_values"]["reason"] == "SECTOR_ENTRIES_LIMIT_EXCEEDED"


# --- M3.1: buy_candidates_handler.pyの複数owner対応 --------------------------


def test_build_unified_targets_aggregates_multiple_owners_of_same_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本人#8306 100株@1500・子供#8306 500株@1300が同時に存在しても、8306の
    targetは1件だけになり、holding_quantity/average_acquisition_priceは
    owner横断の集約値(単純平均ではなく購入金額合計÷株数合計の加重平均)になる
    (M3.1、レビュー指摘3)。"""
    monkeypatch.setattr(handler_module.WatchlistService, "list_items", lambda self: [])
    holdings = [
        _holding("8306", shares=100, average_purchase_price="1500", owner=DEFAULT_OWNER),
        _holding("8306", shares=500, average_purchase_price="1300", owner="子供"),
    ]
    monkeypatch.setattr(handler_module.PortfolioService, "list_holdings", lambda self: holdings)

    targets = handler_module._build_unified_targets(_CONFIG, _NOW)

    assert len(targets) == 1
    target = targets[0]
    assert target.stock_code == "8306"
    assert target.source == CandidateSource.HOLDING
    assert target.holding_quantity == 600
    expected_avg = (Decimal("100") * Decimal("1500") + Decimal("500") * Decimal("1300")) / Decimal(
        "600"
    )
    assert target.average_acquisition_price == expected_avg


def test_dispatch_mode_holding_count_counts_unique_stock_codes_not_holding_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """holding_count(sector_entries集計のcoverage判定に使われる)は、同一銘柄を
    複数ownerが保有していても「Holdingレコード件数」ではなく「保有している
    ユニークstock_code数」として扱う(M3.1、レビュー指摘5)。"""
    _patch_common(monkeypatch)
    monkeypatch.setattr(handler_module.WatchlistService, "list_items", lambda self: [])
    holdings = [
        _holding("8306", shares=100, owner=DEFAULT_OWNER),
        _holding("8306", shares=500, owner="子供"),
    ]
    monkeypatch.setattr(handler_module.PortfolioService, "list_holdings", lambda self: holdings)

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module,
        "start_batch",
        lambda batch_id, total, now, holding_count=0: captured.update(
            total=total, holding_count=holding_count
        ),
    )
    monkeypatch.setattr(handler_module, "dispatch_async", lambda *a, **kw: None)

    result = handler_module.handler({}, _FakeContext())

    assert result == {"dispatched": 1}
    # Holdingは2件(本人・子供)だが、ユニーク銘柄コードは8306の1件のみ。
    assert captured["holding_count"] == 1


def test_process_single_candidate_sector_entry_uses_aggregated_holding_quantity(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """sector_entry(ポートフォリオ集中度算出用)のcurrent_market_valueは、
    呼び出し元から渡されたholding_quantity(_build_unified_targets()側で既に
    全owner集約済みの値)にそのまま基づく。単一owner分だけの株数にならないこと
    を確認する(M3.1、レビュー指摘4)。"""
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    recommendation = _make_recommendation(
        "8306",
        company_quality_score=60.0,
        recommendation_id="rec-8306",
        buy_action=BuyAction.BUY,
        buy_industry_sector=BuyIndustrySector.BANK,
    )
    outcome = _outcome(recommendation, ranking_group="buy_candidate")
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)
    monkeypatch.setattr(
        handler_module.PortfolioService, "list_holdings_by_stock", lambda self, code: []
    )
    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda batch_id, category, sector_entry=None, **kwargs: captured.update(
            sector_entry=sector_entry
        ),
    )

    # holding_quantity=600は本人100株+子供500株の合算(_build_unified_targets()側
    # で既に集約済みの値としてここへ渡される想定)。
    handler_module._process_single_candidate(
        "8306",
        CandidateSource.HOLDING,
        600,
        Decimal("1433.33"),
        "batch-1",
        _NOW,
        object(),
        _CONFIG,
        object(),
        repo,
        fake_service,
    )

    decoded = handler_module._decode_portfolio_exposure_fact(captured["sector_entry"])
    assert decoded is not None
    assert decoded.stock_code == "8306"
    # Issue #82: 時価はsnapshotのcurrent_price(=price_at_recommendationと同一値)
    # と集約済みholding_quantityから算出される。
    assert decoded.current_market_value == _FAKE_SNAPSHOT_PRICE * 600


def _conflict_recommendation(
    stock_code: str, recommendation_type: RecommendationType, owner: str
) -> Recommendation:
    return Recommendation(
        recommendation_id=f"conflict-{owner}",
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        recommended_at=_NOW,
        recommendation_type=recommendation_type,
        price_at_recommendation=Decimal("4200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
        owner=owner,
        holding_id=build_holding_id(owner, stock_code),
    )


class _FakeConflictOutcome:
    def __init__(self, recommendation: Recommendation | None) -> None:
        self.recommendation = recommendation


def _run_addon_conflict_scenario(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    sell_recommendation_by_owner: dict[str, Recommendation | None],
) -> dict[str, object]:
    """本人#8306・子供#8306の2 Holdingを対象に、SellSignalService.analyzeの
    結果をowner別に差し替えて_process_single_candidateを実行し、
    unified_buy_candidate_evaluation監査の最終レコードを返す。"""
    _patch_snapshot(monkeypatch)
    holdings = [
        _holding("8306", shares=100, average_purchase_price="1500", owner=DEFAULT_OWNER),
        _holding("8306", shares=500, average_purchase_price="1300", owner="子供"),
    ]
    monkeypatch.setattr(
        handler_module.PortfolioService, "list_holdings_by_stock", lambda self, code: holdings
    )
    recommendation = _make_recommendation(
        "8306",
        company_quality_score=60.0,
        recommendation_id="rec-8306",
        buy_action=BuyAction.BUY,
    )
    outcome = _outcome(recommendation, ranking_group="buy_candidate")
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)

    def _fake_sell_analyze(self: object, holding: object, now: object, snapshot: object = None):
        return _FakeConflictOutcome(sell_recommendation_by_owner.get(holding.owner))  # type: ignore[union-attr]

    monkeypatch.setattr(handler_module.SellSignalService, "analyze", _fake_sell_analyze)
    monkeypatch.setattr(
        handler_module.ProfitTakingService,
        "analyze",
        lambda self, holding, now, snapshot=None: _FakeConflictOutcome(None),
    )

    audit = _RecordingAuditService()
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: audit)
    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)

    handler_module._process_single_candidate(
        "8306",
        CandidateSource.HOLDING,
        600,
        Decimal("1433.33"),
        None,
        _NOW,
        object(),
        _CONFIG,
        object(),
        repo,
        fake_service,
    )

    records = audit.records_by_type("unified_buy_candidate_evaluation")
    assert len(records) == 1
    return records[0]


def test_addon_conflict_when_default_owner_sells_and_child_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """本人#8306→SELL、子供#8306→HOLD(競合無し)の場合でも、1つでも売却系の
    競合Recommendationがあれば買い増しは競合ありとして抑止される(M3.1、
    レビュー指摘6、必須テストD)。"""
    sell_rec = _conflict_recommendation("8306", RecommendationType.SELL, DEFAULT_OWNER)
    record = _run_addon_conflict_scenario(
        monkeypatch, tmp_path, {DEFAULT_OWNER: sell_rec, "子供": None}
    )

    assert record["output_values"]["conflicting_holding_action"] == RecommendationType.SELL.value


def test_addon_conflict_when_non_default_owner_sells_and_default_owner_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """本人#8306→HOLD、子供#8306→SELLの場合、DEFAULT_OWNERではない子供側の
    SELLも見逃さず買い増しが競合として抑止される(M3.1、レビュー指摘6、
    必須テストE: DEFAULT_OWNER依存が無いことの確認)。"""
    sell_rec = _conflict_recommendation("8306", RecommendationType.SELL, "子供")
    record = _run_addon_conflict_scenario(
        monkeypatch, tmp_path, {DEFAULT_OWNER: None, "子供": sell_rec}
    )

    assert record["output_values"]["conflicting_holding_action"] == RecommendationType.SELL.value


def test_addon_no_conflict_when_all_owners_hold(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """本人#8306・子供#8306ともHOLD(売却/利確系Recommendationなし)の場合、
    買い増し競合は発生しない(M3.1、必須テストF)。"""
    record = _run_addon_conflict_scenario(
        monkeypatch, tmp_path, {DEFAULT_OWNER: None, "子供": None}
    )

    assert record["output_values"]["conflicting_holding_action"] is None


def test_addon_conflict_picks_strongest_priority_among_multiple_owners(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """本人#8306→PARTIAL_PROFIT_TAKE(一部利確、priority=4)、子供#8306→SELL
    (priority=4、同tier)のように複数owner・複数種別の競合が同時に出た場合でも、
    新たな優先順位を新設せず、既存のCross Pipeline Priority優先度表
    (notification_priority_for_recommendation)に従って選ぶ(M3.1、レビュー
    指摘6)。ここではCRITICAL_RISK相当のURGENT_REVIEW(priority=6)を混ぜ、
    それが選ばれることを確認する。"""
    weak_rec = _conflict_recommendation("8306", RecommendationType.PARTIAL_PROFIT_TAKE, "子供")
    strong_rec = _conflict_recommendation(
        "8306", RecommendationType.URGENT_REVIEW, DEFAULT_OWNER
    )
    record = _run_addon_conflict_scenario(
        monkeypatch, tmp_path, {DEFAULT_OWNER: strong_rec, "子供": weak_rec}
    )

    assert (
        record["output_values"]["conflicting_holding_action"]
        == RecommendationType.URGENT_REVIEW.value
    )


# --- 通知検証モード機能(2026-08追加) -------------------------------------


def test_dispatch_mode_propagates_validation_execution_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        handler_module.WatchlistService, "list_items", lambda self: [_watchlist_item("2914")]
    )
    monkeypatch.setattr(handler_module.PortfolioService, "list_holdings", lambda self: [])

    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        handler_module,
        "dispatch_async",
        lambda function_name, payload: dispatched.append(payload),
    )

    result = handler_module.handler({"execution_mode": "VALIDATION"}, _FakeContext())

    assert result == {"dispatched": 1}
    assert dispatched[0]["execution_mode"] == "VALIDATION"
    # コードレビュー対応(2026-08、通知ドライラン機能のバグ修正): notification_mode
    # 未指定時は既定値SENDとして子Lambdaへも明示伝播する(VALIDATION同士なら
    # resolve_execution_context()はnotification_mode指定を許可するため問題ない)。
    assert dispatched[0]["notification_mode"] == "SEND"


def test_dispatch_mode_propagates_notification_mode_dry_run_to_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """バグ修正の回帰テスト(2026-08): VALIDATION+DRY_RUNで起動した場合、子Lambdaへの
    dispatchペイロードにnotification_mode=DRY_RUNが伝播されること。この伝播漏れに
    より、子Lambda側でnotification_modeが既定のSENDへ黙ってフォールバックし、
    DRY_RUN指定にもかかわらず実LINE送信が抑止されない不具合があった。
    """
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        handler_module.WatchlistService,
        "list_items",
        lambda self: [_watchlist_item("2914"), _watchlist_item("8136")],
    )
    monkeypatch.setattr(handler_module.PortfolioService, "list_holdings", lambda self: [])

    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        handler_module,
        "dispatch_async",
        lambda function_name, payload: dispatched.append(payload),
    )

    handler_module.handler(
        {"execution_mode": "VALIDATION", "notification_mode": "DRY_RUN"}, _FakeContext()
    )

    assert len(dispatched) == 2
    for payload in dispatched:
        assert payload["execution_mode"] == "VALIDATION"
        assert payload["notification_mode"] == "DRY_RUN"


def test_dispatch_mode_propagates_notification_mode_send_to_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        handler_module.WatchlistService, "list_items", lambda self: [_watchlist_item("2914")]
    )
    monkeypatch.setattr(handler_module.PortfolioService, "list_holdings", lambda self: [])

    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        handler_module,
        "dispatch_async",
        lambda function_name, payload: dispatched.append(payload),
    )

    handler_module.handler(
        {"execution_mode": "VALIDATION", "notification_mode": "SEND"}, _FakeContext()
    )

    assert dispatched[0]["notification_mode"] == "SEND"


def test_dispatch_mode_unspecified_execution_mode_propagates_normal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        handler_module.WatchlistService, "list_items", lambda self: [_watchlist_item("2914")]
    )
    monkeypatch.setattr(handler_module.PortfolioService, "list_holdings", lambda self: [])

    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        handler_module,
        "dispatch_async",
        lambda function_name, payload: dispatched.append(payload),
    )

    handler_module.handler({}, _FakeContext())

    assert dispatched[0]["execution_mode"] == "NORMAL"
    assert "notification_mode" not in dispatched[0]


def test_handler_invalid_execution_mode_raises_before_any_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不正なexecution_modeはNORMALへフォールバックせず例外を送出する
    (要求仕様)。他の一切の処理(config読み込み等)より前に検知されること。"""
    called: list[str] = []
    monkeypatch.setattr(
        handler_module, "load_config", lambda: called.append("load_config") or _CONFIG
    )

    with pytest.raises(ValueError, match="unknown execution_mode"):
        handler_module.handler({"execution_mode": "BOGUS"}, _FakeContext())

    assert called == []


def test_task_buy_candidate_validation_mode_logs_validation_marker(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(handler_module.WatchlistService, "get_item", lambda self, code: None)

    class _FakeOutcome:
        data_error = "テストエラー"
        recommendation = None
        buy_action = None
        ranking_group = None

    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: _FakeOutcome()
    )

    with caplog.at_level("INFO"):
        handler_module.handler(
            {
                "task": "buy_candidate",
                "stock_code": "2914",
                "source": "WATCHLIST",
                "batch_id": "batch-validation-1",
                "execution_mode": "VALIDATION",
            },
            _FakeContext(),
        )

    assert any(
        "VALIDATION MODE" in r.message and "batch-validation-1" in r.message
        for r in caplog.records
    )


def test_process_single_candidate_validation_mode_saves_to_validation_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """VALIDATION時、RecommendationRepository.for_execution_context()経由の
    検証用リポジトリへ保存され、file_nameが本番用と異なること。"""
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    recommendation = _make_recommendation(
        "2914", company_quality_score=72.5, recommendation_id="rec-1", buy_action=BuyAction.BUY
    )
    outcome = _outcome(recommendation, ranking_group="buy_candidate")
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)
    fake_service = _FakeNotificationServiceForRanking()
    execution_context = ExecutionContext(mode=ExecutionMode.VALIDATION)
    repo = RecommendationRepository.for_execution_context(execution_context, store_dir=tmp_path)

    handler_module._process_single_candidate(
        "2914", CandidateSource.WATCHLIST, None, None, None, _NOW, object(), _CONFIG,
        object(), repo, fake_service, execution_context,
    )

    assert repo.file_name == VALIDATION_FILE_NAME
    assert repo.get("rec-1") is not None


def test_process_single_candidate_validation_mode_skips_decision_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    recommendation = _make_recommendation(
        "2914", company_quality_score=72.5, recommendation_id="rec-1", buy_action=BuyAction.BUY
    )
    outcome = _outcome(recommendation, ranking_group="buy_candidate")
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)
    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)

    snapshot_calls: list[object] = []
    monkeypatch.setattr(
        handler_module,
        "save_decision_snapshot_safely",
        lambda *a, **kw: snapshot_calls.append(a),
    )

    handler_module._process_single_candidate(
        "2914", CandidateSource.WATCHLIST, None, None, None, _NOW, object(), _CONFIG,
        object(), repo, fake_service, ExecutionContext(mode=ExecutionMode.VALIDATION),
    )

    assert snapshot_calls == []


def test_process_single_candidate_normal_mode_still_calls_decision_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """NORMAL回帰確認: VALIDATION対応追加後もsave_decision_snapshot_safelyは
    従来通り呼ばれ続ける。"""
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    recommendation = _make_recommendation(
        "2914", company_quality_score=72.5, recommendation_id="rec-1", buy_action=BuyAction.BUY
    )
    outcome = _outcome(recommendation, ranking_group="buy_candidate")
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)
    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)

    snapshot_calls: list[object] = []
    monkeypatch.setattr(
        handler_module,
        "save_decision_snapshot_safely",
        lambda *a, **kw: snapshot_calls.append(a),
    )

    handler_module._process_single_candidate(
        "2914", CandidateSource.WATCHLIST, None, None, None, _NOW, object(), _CONFIG,
        object(), repo, fake_service,
    )

    assert len(snapshot_calls) == 1


def test_process_single_candidate_validation_mode_reports_validation_recommendation_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    recommendation = _make_recommendation(
        "2914", company_quality_score=72.5, recommendation_id="rec-1", buy_action=BuyAction.BUY
    )
    outcome = _outcome(recommendation, ranking_group="buy_candidate")
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)
    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda batch_id, category, validation_recommendation_id=None, **kwargs: (
            captured.update(validation_recommendation_id=validation_recommendation_id)
        ),
    )

    handler_module._process_single_candidate(
        "2914", CandidateSource.WATCHLIST, None, None, "batch-1", _NOW, object(), _CONFIG,
        object(), repo, fake_service, ExecutionContext(mode=ExecutionMode.VALIDATION),
    )

    assert captured["validation_recommendation_id"] == "rec-1"


def test_process_single_candidate_normal_mode_reports_no_validation_recommendation_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """NORMAL回帰確認: record_resultへvalidation_recommendation_idは渡らない(常にNone)。"""
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    recommendation = _make_recommendation(
        "2914", company_quality_score=72.5, recommendation_id="rec-1", buy_action=BuyAction.BUY
    )
    outcome = _outcome(recommendation, ranking_group="buy_candidate")
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)
    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda batch_id, category, validation_recommendation_id=None, **kwargs: (
            captured.update(validation_recommendation_id=validation_recommendation_id)
        ),
    )

    handler_module._process_single_candidate(
        "2914", CandidateSource.WATCHLIST, None, None, "batch-1", _NOW, object(), _CONFIG,
        object(), repo, fake_service,
    )

    assert captured["validation_recommendation_id"] is None


def test_finalize_batch_validation_mode_deletes_validation_recommendations(
    monkeypatch, tmp_path
) -> None:
    """_finalize_batch正常完了後、当該バッチで保存された全recommendation_idが
    検証用リポジトリから削除される(通知検証モード機能2026-08追加、4.2節(a))。"""
    _patch_audit(monkeypatch)
    execution_context = ExecutionContext(mode=ExecutionMode.VALIDATION)
    repo = RecommendationRepository.for_execution_context(execution_context, store_dir=tmp_path)
    ranking_entries: list[str] = []
    _add_ranked_candidate(repo, ranking_entries, "1111", 90.0)
    # ランキング対象外の"hold"銘柄もVALIDATION実行では保存される想定を模擬する
    # (_finalize_batchはranking_entriesだけでなくvalidation_recommendation_ids
    # 全件を走査して削除する)。
    unranked = _make_recommendation(
        "2222", company_quality_score=10.0, recommendation_id="rec-unranked",
    )
    repo.save(unranked)

    config = _config_with_max_notifications(5)
    progress = _progress(
        ranking_entries,
        total=2,
        category_counts={"candidate_not_ranked": 1, "hold": 1},
        validation_recommendation_ids=["rec-1111", "rec-unranked"],
    )
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(
        progress, "batch-1", config, _NOW, repo, fake_service, execution_context
    )

    assert repo.get("rec-1111") is None
    assert repo.get("rec-unranked") is None


def test_finalize_batch_normal_mode_does_not_delete_recommendations(monkeypatch, tmp_path) -> None:
    """NORMAL回帰確認: 削除処理自体がNORMALでは一切実行されない。"""
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    _add_ranked_candidate(repo, ranking_entries, "1111", 90.0)

    config = _config_with_max_notifications(5)
    progress = _progress(ranking_entries, total=1, category_counts={"candidate_not_ranked": 1})
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)

    assert repo.get("rec-1111") is not None


def test_process_single_candidate_validation_mode_does_not_grow_production_audit_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """通知検証モード コードレビュー対応(Issue 2): _process_single_candidateの
    unified_buy_candidate_evaluation監査(buy_candidates_handlerのevaluation audit)を
    VALIDATIONで実行しても、実際のAuditService/AuditLogRepositoryを使って(保存先の
    みtmp_pathへ差し替えて)本番AuditLogが一切増えないことを検証する。_patch_audit
    (Noopダブル)ではなく実物のAuditServiceを使うことで、execution_context guardの
    実効性そのものを確認する。
    """
    _patch_snapshot(monkeypatch)
    monkeypatch.setattr(handler_module.WatchlistService, "get_item", lambda self, code: None)

    class _FakeOutcome:
        data_error = "テストエラー"
        recommendation = None
        buy_action = None
        ranking_group = None

    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: _FakeOutcome()
    )
    audit_repo = AuditLogRepository(store_dir=tmp_path)
    monkeypatch.setattr(
        handler_module,
        "AuditService",
        lambda *a, **kw: RealAuditService(
            repository=audit_repo, execution_context=kw.get("execution_context")
        ),
    )
    fake_service = _FakeNotificationServiceForRanking()
    execution_context = ExecutionContext(mode=ExecutionMode.VALIDATION)
    repo = RecommendationRepository.for_execution_context(execution_context, store_dir=tmp_path)

    handler_module._process_single_candidate(
        "2914", CandidateSource.WATCHLIST, None, None, None, _NOW, object(), _CONFIG,
        object(), repo, fake_service, execution_context,
    )

    assert audit_repo.list_all() == []


def test_process_single_candidate_normal_mode_still_grows_audit_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """NORMAL回帰確認: 上記と同じ経路でもNORMALでは従来どおりAuditLogへ記録される。"""
    _patch_snapshot(monkeypatch)
    monkeypatch.setattr(handler_module.WatchlistService, "get_item", lambda self, code: None)

    class _FakeOutcome:
        data_error = "テストエラー"
        recommendation = None
        buy_action = None
        ranking_group = None

    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: _FakeOutcome()
    )
    audit_repo = AuditLogRepository(store_dir=tmp_path)
    monkeypatch.setattr(
        handler_module,
        "AuditService",
        lambda *a, **kw: RealAuditService(
            repository=audit_repo, execution_context=kw.get("execution_context")
        ),
    )
    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)

    handler_module._process_single_candidate(
        "2914", CandidateSource.WATCHLIST, None, None, None, _NOW, object(), _CONFIG,
        object(), repo, fake_service,
    )

    assert len(audit_repo.list_all()) == 1


# --- Issue #31: buy側finalize-onceゲート -------------------------------------


def _issue31_completed_progress() -> object:
    return handler_module.BatchProgress(
        total=1,
        completed=2,  # 処理済み銘柄のretryでcompleted>totalとなった状態を再現
        category_counts={},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[],
        sector_entries=[],
        holding_count=0,
        validation_recommendation_ids=[],
    )


def _issue31_run_candidate(monkeypatch, tmp_path) -> tuple[list, list]:
    """is_complete==Trueを2回観測するシナリオを実行し、
    (_finalize_batch呼び出し回数リスト, mark呼び出しリスト)を返す。"""
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    recommendation = _make_recommendation(
        "2914", company_quality_score=72.5, recommendation_id="rec-1",
        buy_action=BuyAction.NOT_ATTRACTIVE,
    )
    outcome = _outcome(recommendation, ranking_group="excluded")
    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome
    )
    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda *a, **kw: _issue31_completed_progress(),
    )
    finalize_calls: list[int] = []
    monkeypatch.setattr(
        handler_module, "_finalize_batch", lambda *a, **kw: finalize_calls.append(1)
    )
    acquire_results = iter(["issue31-token", None])
    monkeypatch.setattr(
        handler_module,
        "try_acquire_completion_finalize",
        lambda batch_id, now: next(acquire_results),
    )
    marks: list[tuple[str, str]] = []

    def _fake_mark(batch_id, token, now):
        marks.append((batch_id, token))
        return True

    monkeypatch.setattr(handler_module, "mark_completion_finalize_completed", _fake_mark)

    for _ in range(2):  # 二重トリガー(retryによるis_complete再成立)を再現
        handler_module._process_single_candidate(
            "2914", CandidateSource.WATCHLIST, None, None, "batch-1", _NOW, object(),
            _CONFIG, object(), repo, fake_service,
        )
    return finalize_calls, marks


def test_issue31_buy_finalize_runs_once_under_duplicate_trigger(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """K/L: is_completeを複数回観測しても、acquire成功側だけが_finalize_batchへ
    進み、正常終了後にmark_completion_finalize_completedが自分のtokenで
    1回だけ呼ばれる。"""
    finalize_calls, marks = _issue31_run_candidate(monkeypatch, tmp_path)
    assert finalize_calls == [1]
    assert marks == [("batch-1", "issue31-token")]


def test_issue31_buy_finalize_exception_does_not_mark_completed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """M: _finalize_batchが例外の場合、completed_atは記録されない
    (stale化後のtakeoverで再実行可能な状態を保つ)。"""
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    recommendation = _make_recommendation(
        "2914", company_quality_score=72.5, recommendation_id="rec-1",
        buy_action=BuyAction.NOT_ATTRACTIVE,
    )
    outcome = _outcome(recommendation, ranking_group="excluded")
    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome
    )
    fake_service = _FakeNotificationServiceForRanking()
    repo = RecommendationRepository(store_dir=tmp_path)
    monkeypatch.setattr(
        handler_module, "record_result", lambda *a, **kw: _issue31_completed_progress()
    )

    def _boom(*_a, **_kw):
        raise RuntimeError("finalize failed (simulated)")

    monkeypatch.setattr(handler_module, "_finalize_batch", _boom)
    monkeypatch.setattr(
        handler_module,
        "try_acquire_completion_finalize",
        lambda batch_id, now: "issue31-token",
    )
    marks: list[tuple[str, str]] = []
    monkeypatch.setattr(
        handler_module,
        "mark_completion_finalize_completed",
        lambda batch_id, token, now: marks.append((batch_id, token)) or True,
    )

    with pytest.raises(RuntimeError, match="finalize failed"):
        handler_module._process_single_candidate(
            "2914", CandidateSource.WATCHLIST, None, None, "batch-1", _NOW, object(),
            _CONFIG, object(), repo, fake_service,
        )
    assert marks == []


# --- Issue #82(2026-08-30): portfolio exposure は screening eligibility と独立 -----
#
# 「特定のscreening経路を通過した場合だけexposureが生成される」という責務結合が
# root causeであったため、**どのscreening結果でも同じexposureが生成される**ことを
# 固定する。これが崩れると、保有銘柄が1件でも除外された日に全HOLDING/BOTH候補の
# 集中度判定が停止する(#29有効時は金融株保有で恒久化する)。


def _capture_exposure_entry(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda batch_id, category, sector_entry=None, **kwargs: captured.update(
            sector_entry=sector_entry
        ),
    )
    return captured


def _outcome_excluded(stock_code: str):
    from jstock_advisor.services.buy_signal_service import BuyAnalysisOutcome

    return BuyAnalysisOutcome(
        stock_code=stock_code,
        recommendation=None,
        screening_passed=False,
        exclusion_reasons=["金融業は個別評価ルール未実装のため対象外"],
        data_error=None,
        buy_action=BuyAction.EXCLUDED,
        ranking_group="excluded",
    )


def _outcome_data_insufficient(stock_code: str):
    from jstock_advisor.services.buy_signal_service import BuyAnalysisOutcome

    return BuyAnalysisOutcome(
        stock_code=stock_code,
        recommendation=None,
        screening_passed=True,
        exclusion_reasons=[],
        data_error="財務データを取得できませんでした",
        buy_action=BuyAction.DATA_INSUFFICIENT,
        ranking_group=None,
    )


@pytest.mark.parametrize("screening_result", ["PASSED", "EXCLUDED", "DATA_INSUFFICIENT"])
def test_exposure_fact_is_identical_regardless_of_screening_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path, screening_result: str
) -> None:
    """同じsnapshot + 同じ保有情報なら、screening結果によらず同じexposure factになる。

    Issue #82 の核心。exposure factの生成はscreening_outcome / BuyAction /
    BuyAnalysisOutcome のいずれにも依存しない。
    """
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    monkeypatch.setattr(
        handler_module.PortfolioService, "list_holdings_by_stock", lambda self, code: []
    )

    if screening_result == "PASSED":
        recommendation = _make_recommendation(
            "8306",
            company_quality_score=60.0,
            recommendation_id="rec-8306",
            buy_action=BuyAction.BUY,
            buy_industry_sector=BuyIndustrySector.BANK,
        )
        outcome = _outcome(recommendation, ranking_group="buy_candidate")
    elif screening_result == "EXCLUDED":
        outcome = _outcome_excluded("8306")
    else:
        outcome = _outcome_data_insufficient("8306")

    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)
    captured = _capture_exposure_entry(monkeypatch)

    handler_module._process_single_candidate(
        "8306",
        CandidateSource.HOLDING,
        600,
        Decimal("1433.33"),
        "batch-1",
        _NOW,
        object(),
        _CONFIG,
        object(),
        RecommendationRepository(store_dir=tmp_path),
        _FakeNotificationServiceForRanking(),
    )

    decoded = handler_module._decode_portfolio_exposure_fact(captured["sector_entry"])
    assert decoded is not None, f"{screening_result}でexposure factが報告されていない"
    assert decoded.stock_code == "8306"
    assert decoded.current_market_value == _FAKE_SNAPSHOT_PRICE * 600


def test_exposure_fact_not_reported_for_watchlist_only_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """保有していない銘柄はportfolio exposureを持たない(分母へ入れない)。"""
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: _outcome_excluded("2914")
    )
    captured = _capture_exposure_entry(monkeypatch)

    handler_module._process_single_candidate(
        "2914", CandidateSource.WATCHLIST, None, None, "batch-1", _NOW, object(), _CONFIG,
        object(), RecommendationRepository(store_dir=tmp_path),
        _FakeNotificationServiceForRanking(),
    )

    assert captured.get("sector_entry") is None


def test_exposure_fact_not_reported_when_holding_quantity_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """株数が不明なら時価を出せないため報告しない(**0円補完はしない**)。

    結果としてcoverage不足となり、銘柄集中度・業種集中度ともfail-closeする。
    """
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: _outcome_excluded("8306")
    )
    captured = _capture_exposure_entry(monkeypatch)

    handler_module._process_single_candidate(
        "8306", CandidateSource.HOLDING, None, None, "batch-1", _NOW, object(), _CONFIG,
        object(), RecommendationRepository(store_dir=tmp_path),
        _FakeNotificationServiceForRanking(),
    )

    assert captured.get("sector_entry") is None


def test_exposure_fact_marks_unknown_sector_when_industry_unresolvable(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """業種を解決できなくてもfactは作る(時価は分母へ算入する)。"""
    _patch_audit(monkeypatch)
    monkeypatch.setattr(
        handler_module,
        "build_stock_snapshot",
        lambda *a, **kw: (_fake_snapshot(industry=None, sector=None), None),
    )
    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: _outcome_excluded("8306")
    )
    captured = _capture_exposure_entry(monkeypatch)

    handler_module._process_single_candidate(
        "8306", CandidateSource.HOLDING, 100, Decimal("900"), "batch-1", _NOW, object(), _CONFIG,
        object(), RecommendationRepository(store_dir=tmp_path),
        _FakeNotificationServiceForRanking(),
    )

    decoded = handler_module._decode_portfolio_exposure_fact(captured["sector_entry"])
    assert decoded is not None
    assert decoded.sector_is_known is False
    assert decoded.current_market_value == _FAKE_SNAPSHOT_PRICE * 100


def test_exposure_fact_survives_exception_in_analysis_and_reaches_aggregation(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """producer→aggregator契約が**例外経路でも**維持されることを固定する。

    snapshot取得成功 → exposure fact生成 → analyze()例外 → handler終了
    → aggregate実行 → **そのfactが集計へ実際に使われる**、までを1本で確認する。

    「factが生成された」だけでは不十分で、例外を出した銘柄の時価が
    ポートフォリオ分母へ入らなければ coverage 不足となり、結局その日の
    HOLDING/BOTH候補が全滅する(#82のroot causeと同じ結果になる)。
    """
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)

    def _boom(self, *a: object, **kw: object) -> object:
        raise RuntimeError("unexpected analysis failure")

    monkeypatch.setattr(handler_module.BuySignalService, "analyze", _boom)
    captured = _capture_exposure_entry(monkeypatch)

    # --- producer側: analyze()が例外を投げてもhandlerは完走する ---
    handler_module._process_single_candidate(
        "8306", CandidateSource.HOLDING, 600, Decimal("1433.33"), "batch-1", _NOW, object(),
        _CONFIG, object(), RecommendationRepository(store_dir=tmp_path),
        _FakeNotificationServiceForRanking(),
    )

    reported_entry = captured["sector_entry"]
    assert isinstance(reported_entry, str)

    # --- aggregator側: 報告されたentryが実際に集計へ使われる ---
    # 例外を出した8306と、正常に評価された7239の2銘柄を保有している状況。
    # 7239は8306とは別業種にしておく(業種集中度の上限に触れさせないため。
    # ここで確認したいのは「判定が成立するか」であり、上限超過の有無ではない)。
    exposure = handler_module._aggregate_portfolio_exposure(
        [reported_entry, _encode("7239", BuyIndustrySector.GENERAL, "100000")],
        holding_count=2,
    )

    expected_8306_value = _FAKE_SNAPSHOT_PRICE * 600
    # 例外銘柄の時価が分母へ算入され、coverageが満たされている。
    assert exposure.coverage_ratio == 1.0
    assert exposure.basis == PortfolioValuationBasis.MARKET_VALUE
    assert exposure.portfolio_total == expected_8306_value + Decimal("100000")
    assert exposure.facts_by_stock["8306"].current_market_value == expected_8306_value

    # --- consumer側: 無関係な候補(7239)の集中度判定が成立する ---
    assessment, eligibility = evaluate_add_on_eligibility(
        current_market_value=Decimal("100000"),
        # 1単元あたりの買い増し額を小さくし、集中度上限に触れない条件にする
        # (ここで確認したいのは「判定が成立するか」であり、上限超過の有無ではない)。
        current_price=Decimal("100"),
        trading_unit=100,
        portfolio_total_market_value=exposure.portfolio_total,
        sector_total_market_value=exposure.sector_total_for("7239"),
        portfolio_valuation_basis=exposure.basis,
        conflicting_holding_action=None,
        holding_data_inconsistent=False,
        holding_is_odd_lot=False,
        config=_CONFIG.add_on,
    )

    # reliability不足でまとめてブロックされるのではなく、両ゲートが実際に評価される。
    assert assessment.portfolio_data_reliable is True
    assert assessment.sector_exposure_available is True
    assert assessment.current_position_ratio is not None
    assert assessment.current_sector_ratio is not None
    assert eligibility.eligible is True


@pytest.mark.parametrize(
    ("reported", "holding_count", "expected_coverage"),
    [
        (4, 5, 0.8),  # 高いcoverage(80%)でも許可の根拠にしない
        (1, 5, 0.2),  # 低いcoverageでも扱いは同じ(fail-close)
    ],
)
def test_partial_coverage_never_authorizes_add_on(
    reported: int, holding_count: int, expected_coverage: float
) -> None:
    """coverage_ratioは判定条件にならない(部分カバレッジでfail-openしない)。

    「80%取れたから残り20%を無視して買い増し許可」を禁止する契約を、
    **振る舞い**で固定する(実装内の変数名・ログ文言には依存しない)。

    coverageの値がいくつであっても、報告が揃っていなければ
    銘柄集中度・業種集中度ともfail-closeする。
    """
    entries = [_encode(f"{9000 + i}", BuyIndustrySector.BANK, "100000") for i in range(reported)]
    exposure = handler_module._aggregate_portfolio_exposure(entries, holding_count=holding_count)

    # coverageは観測値としては算出される(observability / audit用途)。
    assert exposure.coverage_ratio == expected_coverage
    # しかし判定の前提は成立しない。
    assert exposure.basis == PortfolioValuationBasis.UNAVAILABLE
    assert exposure.portfolio_total is None
    assert exposure.sector_exposure_complete is False

    assessment, eligibility = evaluate_add_on_eligibility(
        current_market_value=Decimal("100000"),
        current_price=Decimal("1000"),
        trading_unit=100,
        portfolio_total_market_value=exposure.portfolio_total,
        sector_total_market_value=exposure.sector_total_for("9000"),
        portfolio_valuation_basis=exposure.basis,
        conflicting_holding_action=None,
        holding_data_inconsistent=False,
        holding_is_odd_lot=False,
        config=_CONFIG.add_on,
    )

    # stock gate / sector gate ともfail-close。比率も算出されない。
    assert assessment.portfolio_data_reliable is False
    assert assessment.sector_exposure_available is False
    assert assessment.current_position_ratio is None
    assert assessment.current_sector_ratio is None
    assert eligibility.eligible is False
    assert eligibility.block_category == EligibilityBlockCategory.PORTFOLIO_DATA_RELIABILITY


def test_add_on_decision_is_identical_for_different_coverage_ratios() -> None:
    """coverage_ratioの値そのものが判定を変えないことを直接固定する。

    coverage 0.8 と 0.2 で集計結果・ゲート判定がまったく同じになることを示し、
    「coverageが高いほど緩む」という実装が入り込む余地を塞ぐ。
    """

    def _decide(reported: int) -> tuple[object, object, object]:
        entries = [
            _encode(f"{9000 + i}", BuyIndustrySector.BANK, "100000") for i in range(reported)
        ]
        exposure = handler_module._aggregate_portfolio_exposure(entries, holding_count=5)
        _assessment, eligibility = evaluate_add_on_eligibility(
            current_market_value=Decimal("100000"),
            current_price=Decimal("1000"),
            trading_unit=100,
            portfolio_total_market_value=exposure.portfolio_total,
            sector_total_market_value=exposure.sector_total_for("9000"),
            portfolio_valuation_basis=exposure.basis,
            conflicting_holding_action=None,
            holding_data_inconsistent=False,
            holding_is_odd_lot=False,
            config=_CONFIG.add_on,
        )
        return exposure.basis, eligibility.eligible, eligibility.block_reason

    high_coverage = _decide(4)
    low_coverage = _decide(1)

    assert high_coverage == low_coverage


def test_exposure_entry_stays_within_max_entry_bytes() -> None:
    """v2形式でも1エントリのバイト上限を超えない(既存上限の回帰)。"""
    entry = _encode("99999", BuyIndustrySector.GENERAL_MANUFACTURING, "999999999999")

    assert len(entry.encode("utf-8")) <= handler_module.MAX_SECTOR_ENTRY_BYTES


def test_finalize_batch_does_not_block_all_holdings_when_financial_stock_is_screened_out(
    monkeypatch, tmp_path
) -> None:
    """Issue #82: 金融株を保有していても無関係な買い増し候補が全滅しない。

    #29 により金融株は毎日screening除外されるが、除外銘柄もexposureを報告する
    ため coverage が満たされ、集中度判定が成立する。以前はこの状況で
    `CONCENTRATION_RELIABILITY_INSUFFICIENT` により全HOLDING/BOTH候補が
    恒久的にブロックされていた。
    """
    audit = _RecordingAuditService()
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: audit)
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    # 買い増し候補(金融株とは無関係な銘柄)
    _add_ranked_candidate(
        repo,
        ranking_entries,
        "7239",
        90.0,
        candidate_source=CandidateSource.HOLDING,
        current_market_value="100000",
        holding_quantity=100,
        average_acquisition_price="900",
    )

    config = _config_with_max_notifications(5)
    progress = _progress(
        ranking_entries,
        total=1,
        category_counts={"candidate_not_ranked": 1},
        sector_entries=[
            _encode("7239", BuyIndustrySector.AUTOMOTIVE_PARTS, "100000"),
            # #29 でscreening除外される金融株。除外されても保有しているため報告される。
            _encode("8306", BuyIndustrySector.BANK, "2500000"),
        ],
        holding_count=2,
    )
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)

    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["7239"]
    output_values = audit.records_by_type("unified_buy_candidate_notification_outcome")[0][
        "output_values"
    ]
    assert output_values["notification_status"] == "SENT"
    # 金融株の時価も分母へ含まれている(2,600,000)。
    assert output_values["portfolio_total_market_value"] == "2600000"
    assert output_values["portfolio_valuation_basis"] == PortfolioValuationBasis.MARKET_VALUE.value


def test_finalize_batch_unknown_sector_holding_blocks_only_sector_gate(
    monkeypatch, tmp_path
) -> None:
    """Issue #82: 業種不明の保有銘柄があっても分母は成立し、業種ゲートのみ不成立。

    銘柄集中度は評価されるため、理由コードで業種データ不足だと識別できる。
    """
    audit = _RecordingAuditService()
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: audit)
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    _add_ranked_candidate(
        repo,
        ranking_entries,
        "7239",
        90.0,
        candidate_source=CandidateSource.HOLDING,
        current_market_value="100000",
        holding_quantity=100,
        average_acquisition_price="900",
    )

    config = _config_with_max_notifications(5)
    progress = _progress(
        ranking_entries,
        total=1,
        category_counts={"candidate_not_ranked": 1},
        sector_entries=[
            _encode("7239", BuyIndustrySector.AUTOMOTIVE_PARTS, "100000"),
            _encode("9999", BuyIndustrySector.UNKNOWN, "2500000", known=False),
        ],
        holding_count=2,
    )
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, "batch-1", config, _NOW, repo, fake_service)

    assert [r.stock_code for r in fake_service.digest_calls[0]] == []
    output_values = audit.records_by_type("unified_buy_candidate_notification_outcome")[0][
        "output_values"
    ]
    # ポートフォリオ総額そのものは成立している(業種不明でも分母へ算入)。
    assert output_values["portfolio_valuation_basis"] == PortfolioValuationBasis.MARKET_VALUE.value
    assert output_values["portfolio_total_market_value"] == "2600000"
    # ブロック理由は業種データ不足であり、時価の信頼性不足とは区別できる。
    assert output_values["block_reason"] == "SECTOR_EXPOSURE_DATA_INSUFFICIENT"


# ============================================================================
# Issue #105: VALIDATION は BuyCandidateEvaluationRecord を本番テーブルへ書かない
#
# `BuyCandidateEvaluationRecord` は本番テーブル専用(検証用テーブルを持たない)。
# 対称的な `HoldingEvaluationRecord` と同じく、VALIDATION では **保存自体を
# スキップ** することで「VALIDATION は通常運用の永続履歴を汚さない」契約を守る。
#
# save 側だけを抑止して finalize の update 側を残すと、対象銘柄数ぶんの
# "BuyCandidateEvaluationRecord not found" warning が出続け、read 自体も
# 本番テーブルへ発生する。**save と update は対称に抑止する。**
# ============================================================================


class _SpyEvaluationRecordRepository:
    """本番 evaluation record repository への到達を検出する spy。

    `upsert` / `get` のいずれかが呼ばれたら記録する(呼ばれないことの assert 用)。
    """

    def __init__(self, seed: dict | None = None) -> None:
        self.items: dict = dict(seed or {})
        self.upsert_calls: list[str] = []
        self.get_calls: list[str] = []

    def upsert(self, record) -> None:  # noqa: ANN001
        self.upsert_calls.append(record.evaluation_id)
        self.items[record.evaluation_id] = record

    def get(self, evaluation_id: str):  # noqa: ANN201
        self.get_calls.append(evaluation_id)
        return self.items.get(evaluation_id)

    def list_by_batch(self, batch_id: str) -> list:
        return [r for r in self.items.values() if getattr(r, "batch_id", None) == batch_id]


def _run_single_candidate_with_spy(
    monkeypatch: pytest.MonkeyPatch, tmp_path, execution_context: ExecutionContext
) -> _SpyEvaluationRecordRepository:
    _patch_snapshot(monkeypatch)
    _patch_audit(monkeypatch)
    recommendation = _make_recommendation(
        "2914", company_quality_score=72.5, recommendation_id="rec-1", buy_action=BuyAction.BUY
    )
    outcome = _outcome(recommendation, ranking_group="buy_candidate")
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)
    monkeypatch.setattr(handler_module, "record_result", lambda *a, **kw: None)
    spy = _SpyEvaluationRecordRepository()
    handler_module._process_single_candidate(
        "2914", CandidateSource.WATCHLIST, None, None, "batch-105", _NOW, object(), _CONFIG,
        object(), RecommendationRepository(store_dir=tmp_path),
        _FakeNotificationServiceForRanking(), execution_context, spy,
    )
    return spy


def test_t1_normal_saves_evaluation_record_to_production_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """T1: NORMAL では従来どおり本番 repository へ upsert される(既存挙動の回帰)。"""
    spy = _run_single_candidate_with_spy(monkeypatch, tmp_path, ExecutionContext.normal())

    assert spy.upsert_calls == ["batch-105:2914"]


def test_t2_validation_does_not_write_evaluation_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """T2(#105 本体): VALIDATION では本番 repository へ一切 write しない。"""
    spy = _run_single_candidate_with_spy(
        monkeypatch, tmp_path, ExecutionContext(mode=ExecutionMode.VALIDATION)
    )

    assert spy.upsert_calls == [], "VALIDATION で本番 evaluation record へ書き込んでいる"
    assert spy.get_calls == []


def test_t2b_validation_dry_run_does_not_write_evaluation_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """T2 の DRY_RUN 版。notification_mode によらず抑止されること。"""
    ctx = ExecutionContext(
        mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.DRY_RUN
    )
    spy = _run_single_candidate_with_spy(monkeypatch, tmp_path, ctx)

    assert spy.upsert_calls == []
    assert spy.get_calls == []


def _run_finalize_with_spy(
    monkeypatch: pytest.MonkeyPatch, tmp_path, execution_context: ExecutionContext
):
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries = []
    for code in ("1111", "2222"):
        rec = _make_recommendation(
            code, company_quality_score=60.0, recommendation_id=f"rec-{code}",
            buy_action=BuyAction.BUY,
        )
        repo.save(rec)
        ranking_entries.append(handler_module._encode_buy_ranking_entry(rec))
    progress = _progress(ranking_entries, total=2, category_counts={"buy_candidate": 2})
    # finalize 側が「判定時点で作成済み」と想定するレコードを事前投入する。
    # VALIDATION では read すら発生しないことを確認するため、seed は両モード共通。
    seed = {}
    for code in ("1111", "2222"):
        record = BuyCandidateEvaluationRecord(
            evaluation_id=build_evaluation_id("batch-105", code),
            batch_id="batch-105", stock_code=code, evaluated_at=_NOW,
            rule_version="v1-mvp", candidate_source=CandidateSource.WATCHLIST,
            purchase_category=PurchaseCategory.BUY_CANDIDATE,
            final_buy_action=BuyAction.BUY, raw_buy_action=BuyAction.BUY,
            recommendation_id=f"rec-{code}",
        )
        seed[record.evaluation_id] = record
    spy = _SpyEvaluationRecordRepository(seed)
    fake_service = _FakeNotificationServiceForRanking()
    handler_module._finalize_batch(
        progress, "batch-105", _CONFIG, _NOW, repo, fake_service, execution_context, spy,
    )
    return spy, fake_service


def test_t5_validation_finalize_does_not_read_or_update_evaluation_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """T5: VALIDATION の finalize では repository の get / upsert を一切呼ばない。

    save 側だけ抑止して update 側を残すと "not found" warning が大量に出るため、
    **両方が対称に抑止されている**ことをここで固定する。
    """
    spy, _ = _run_finalize_with_spy(
        monkeypatch, tmp_path, ExecutionContext(mode=ExecutionMode.VALIDATION)
    )

    assert spy.get_calls == [], "VALIDATION finalize が本番 evaluation record を read している"
    assert spy.upsert_calls == []


def test_t5b_normal_finalize_still_updates_evaluation_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """T5 の NORMAL 回帰: finalize の outcome 追記は従来どおり行われる。"""
    spy, _ = _run_finalize_with_spy(monkeypatch, tmp_path, ExecutionContext.normal())

    assert spy.get_calls, "NORMAL で finalize の read が消えている"
    assert spy.upsert_calls, "NORMAL で finalize の outcome 追記が消えている"


def test_t6_validation_finalize_completes_without_evaluation_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """T6: evaluation record を保存しない VALIDATION でも finalize が完走する。

    ranking(digest 送信)と batch summary の生成がどちらも行われること。
    """
    _, fake_service = _run_finalize_with_spy(
        monkeypatch, tmp_path, ExecutionContext(mode=ExecutionMode.VALIDATION)
    )

    assert len(fake_service.digest_calls) == 1
    assert [r.stock_code for r in fake_service.digest_calls[0]] == ["1111", "2222"]
    assert fake_service.batch_summary_calls, "batch summary が生成されていない"


class _SpyPointerRepository:
    def __init__(self) -> None:
        self.updates: list = []

    def get(self):  # noqa: ANN201
        return None

    def update_latest_completed(self, pointer) -> None:  # noqa: ANN001
        self.updates.append(pointer)


@pytest.mark.parametrize(
    ("ctx", "expect_updated"),
    [
        (ExecutionContext.normal(), True),
        (ExecutionContext(mode=ExecutionMode.VALIDATION), False),
    ],
    ids=["normal", "validation"],
)
def test_t7_latest_batch_pointer_contract_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path, ctx: ExecutionContext, expect_updated: bool
) -> None:
    """T7: latest batch pointer の更新契約(NORMAL のみ)が #105 修正後も不変。

    NORMAL では全銘柄分の evaluation record 保存成功が条件のため更新され、
    VALIDATION では保存しない = 条件を満たさないうえ mode ゲートでも弾かれる。
    """
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    rec = _make_recommendation(
        "1111", company_quality_score=60.0, recommendation_id="rec-1111",
        buy_action=BuyAction.BUY,
    )
    repo.save(rec)
    progress = _progress(
        [handler_module._encode_buy_ranking_entry(rec)],
        total=1, category_counts={"buy_candidate": 1},
    )
    progress.evaluation_record_saved_stock_codes.append("1111")
    spy = _SpyEvaluationRecordRepository()
    pointer = _SpyPointerRepository()

    handler_module._finalize_batch(
        progress, "batch-105", _CONFIG, _NOW, repo,
        _FakeNotificationServiceForRanking(), ctx, spy, pointer,
    )

    assert bool(pointer.updates) is expect_updated
