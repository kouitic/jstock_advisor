import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import (
    AccountType,
    BuyAction,
    BuyIndustrySector,
    CandidateSource,
    ConfidenceLevel,
    EligibilityBlockCategory,
    ExecutionMode,
    PortfolioValuationBasis,
    RecommendationType,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.notification_eligibility import NotificationEligibility
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
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
    stock_code: str, shares: int = 100, average_purchase_price: str = "1000"
) -> Holding:
    price = Decimal(average_purchase_price)
    return Holding(
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


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler_module, "build_real_provider_bundle", lambda now, config: object())
    monkeypatch.setattr(handler_module, "build_line_client_from_env", lambda: object())
    monkeypatch.setattr(handler_module, "build_stock_snapshot", lambda *a, **kw: (object(), None))
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
    monkeypatch.setattr(handler_module, "build_stock_snapshot", lambda *a, **kw: (object(), None))


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
        self, winners: list[Recommendation], now: dt.datetime
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
    ) -> bool:
        self.batch_summary_calls.append(
            {
                "total": total,
                "category_counts": dict(category_counts),
                "buy_candidates_sent_count": buy_candidates_sent_count,
                "near_buy_sent_count": near_buy_sent_count,
                "send_empty_summary": send_empty_summary,
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
    """価格待ち(WATCH_FOR_PRICE)はLINE通知対象外のため、件数のみ集計し
    ランキング登録は行わない。"""
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

    assert captured["category"] == "watch_not_ranked"
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
    sentinel_snapshot = object()

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
        handler_module.PortfolioService, "get_holding", lambda self, code: _holding(code)
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

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

    assert len(fake_service.digest_calls) == 1
    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["1111", "3333"]

    call = fake_service.batch_summary_calls[0]
    assert call["category_counts"]["sent"] == 2
    # v3: 全ランキングを評価し尽くすため、上限を超えた3件はOUTSIDE_TOP_5として
    # suppressedへ計上される(candidate_not_rankedは「まだ評価していない」件数の
    # ため0になる)。
    assert call["category_counts"]["candidate_not_ranked"] == 0
    assert call["category_counts"]["suppressed"] == 3
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

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["1111"]

    call = fake_service.batch_summary_calls[0]
    assert call["category_counts"]["sent"] == 1
    assert call["category_counts"]["suppressed"] == 1
    assert call["category_counts"]["review"] == 1
    assert call["category_counts"]["candidate_not_ranked"] == 0
    assert call["buy_candidates_sent_count"] == 1


def test_finalize_batch_reports_zero_buy_candidates_sent_when_none_ranked(
    monkeypatch, tmp_path
) -> None:
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    progress = _progress([], total=3, category_counts={"hold": 1, "watch_not_ranked": 2})
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, _CONFIG, _NOW, repo, fake_service)

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

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

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

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["2nd", "3rd", "4th", "5th", "6th"]

    call = fake_service.batch_summary_calls[0]
    assert call["buy_candidates_sent_count"] == 5
    assert call["category_counts"]["suppressed"] == 1
    assert call["category_counts"]["candidate_not_ranked"] == 0


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

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

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

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

    assert fake_service.digest_calls == [[]]
    assert fake_service.batch_summary_calls[0]["buy_candidates_sent_count"] == 0
    assert fake_service.batch_summary_calls[0]["category_counts"]["suppressed"] == 2


def test_finalize_batch_evaluates_with_buy_candidate_batch_context(monkeypatch, tmp_path) -> None:
    _patch_audit(monkeypatch)
    from jstock_advisor.domain.entities.enums import NotificationContext

    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    _add_ranked_candidate(repo, ranking_entries, "2914", 90.0)

    config = _config_with_max_notifications(5)
    progress = _progress(ranking_entries, total=1, category_counts={"candidate_not_ranked": 1})
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

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

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["c1", "c2", "c3", "c4", "c5"]
    call = fake_service.batch_summary_calls[0]
    # c6はデータ品質でreviewへ計上され、OUTSIDE_TOP_5(suppressed)ではない。
    assert call["category_counts"]["review"] == 1


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

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["c1", "c2", "c3", "c4", "c5"]
    assert fake_service.batch_summary_calls[0]["category_counts"]["suppressed"] == 1


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
        handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)


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

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

    assert fake_service.batch_summary_calls[0]["buy_candidates_sent_count"] == 0


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
            handler_module._encode_sector_entry(
                BuyIndustrySector.AUTOMOTIVE_PARTS, Decimal("100000"), "7239"
            )
        ],
        holding_count=1,
    )
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

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
            handler_module._encode_sector_entry(
                BuyIndustrySector.AUTOMOTIVE_PARTS, Decimal("100000"), "7239"
            ),
            handler_module._encode_sector_entry(
                BuyIndustrySector.BANK, Decimal("2500000"), "8001"
            ),
        ],
        holding_count=2,
    )
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

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
            handler_module._encode_sector_entry(
                BuyIndustrySector.AUTOMOTIVE_PARTS, Decimal("100000"), "7239"
            )
        ],
        holding_count=2,
    )
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

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

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

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
            handler_module._encode_sector_entry(
                BuyIndustrySector.AUTOMOTIVE_PARTS, Decimal("50000"), "9002"
            ),
            handler_module._encode_sector_entry(
                BuyIndustrySector.BANK, Decimal("2500000"), "8001"
            ),
        ],
        holding_count=2,
    )
    fake_service = _FakeNotificationServiceForRanking()

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["9001", "9002", "9003"]


# --- sector_entries書き込み/読み込み時検証・保有データ整合性の単体テスト
# (統合BUY候補パイプライン2026-07 §4・§5) ---


def test_encode_decode_sector_entry_round_trip() -> None:
    entry = handler_module._encode_sector_entry(
        BuyIndustrySector.BANK, Decimal("123456"), "7203"
    )
    decoded = handler_module._decode_sector_entry(entry)

    assert decoded == (BuyIndustrySector.BANK, Decimal("123456"), "7203")


@pytest.mark.parametrize(
    "invalid_entry",
    [
        "BANK|123456",  # 要素数不足
        "BANK|123456|7203|extra",  # 要素数過多
        "NOT_A_SECTOR|123456|7203",  # 不正な業種
        "BANK|not_a_number|7203",  # Decimalとしてパース不可
        "BANK|123456|watch-high",  # 銘柄コードが数字パターンでない
        "BANK|123456|123",  # 銘柄コードが3桁(4〜5桁のパターンに不一致)
        "",
    ],
)
def test_decode_sector_entry_returns_none_for_invalid_entries(invalid_entry: str) -> None:
    assert handler_module._decode_sector_entry(invalid_entry) is None


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


def test_aggregate_sector_entries_full_coverage_yields_market_value_basis() -> None:
    entries = [
        handler_module._encode_sector_entry(BuyIndustrySector.BANK, Decimal("100000"), "7203"),
        handler_module._encode_sector_entry(
            BuyIndustrySector.AUTOMOTIVE_PARTS, Decimal("200000"), "9001"
        ),
    ]

    totals, portfolio_total, basis, coverage = handler_module._aggregate_sector_entries(
        entries, holding_count=2
    )

    assert basis == PortfolioValuationBasis.MARKET_VALUE
    assert portfolio_total == Decimal("300000")
    assert totals == {
        BuyIndustrySector.BANK: Decimal("100000"),
        BuyIndustrySector.AUTOMOTIVE_PARTS: Decimal("200000"),
    }
    assert coverage == 1.0


def test_aggregate_sector_entries_incomplete_coverage_is_unavailable() -> None:
    entries = [
        handler_module._encode_sector_entry(BuyIndustrySector.BANK, Decimal("100000"), "7203"),
    ]

    totals, portfolio_total, basis, coverage = handler_module._aggregate_sector_entries(
        entries, holding_count=2
    )

    assert basis == PortfolioValuationBasis.UNAVAILABLE
    assert portfolio_total is None
    assert totals == {}
    assert coverage == 0.5


def test_aggregate_sector_entries_conflicting_duplicate_is_unavailable() -> None:
    """同一銘柄で異なる値の複数エントリが存在する場合、信頼性不足として扱う
    (例: 価格再取得やリトライによる不一致)。"""
    entries = [
        handler_module._encode_sector_entry(BuyIndustrySector.BANK, Decimal("100000"), "7203"),
        handler_module._encode_sector_entry(BuyIndustrySector.BANK, Decimal("999999"), "7203"),
    ]

    totals, portfolio_total, basis, coverage = handler_module._aggregate_sector_entries(
        entries, holding_count=1
    )

    assert basis == PortfolioValuationBasis.UNAVAILABLE
    assert portfolio_total is None
    assert totals == {}


def test_aggregate_sector_entries_identical_duplicate_is_not_double_counted() -> None:
    """DynamoDB String Setは完全一致文字列の重複を保持しないが、呼び出し側の
    集計ロジック自体も同一銘柄・同一値の重複入力を二重加算しないことを確認する。
    """
    entry = handler_module._encode_sector_entry(BuyIndustrySector.BANK, Decimal("100000"), "7203")

    totals, portfolio_total, basis, _coverage = handler_module._aggregate_sector_entries(
        [entry, entry], holding_count=1
    )

    assert basis == PortfolioValuationBasis.MARKET_VALUE
    assert portfolio_total == Decimal("100000")
    assert totals == {BuyIndustrySector.BANK: Decimal("100000")}


def test_aggregate_sector_entries_ignores_invalid_entries_without_crashing() -> None:
    entries = [
        handler_module._encode_sector_entry(BuyIndustrySector.BANK, Decimal("100000"), "7203"),
        "garbage|not|valid|entry",
    ]

    totals, portfolio_total, basis, coverage = handler_module._aggregate_sector_entries(
        entries, holding_count=1
    )

    # 不正エントリは無視され、有効な1銘柄分だけで完全カバレッジとして扱われる
    assert basis == PortfolioValuationBasis.MARKET_VALUE
    assert portfolio_total == Decimal("100000")
    assert coverage == 1.0
    assert totals == {BuyIndustrySector.BANK: Decimal("100000")}


def test_aggregate_sector_entries_zero_holding_count_is_unavailable_without_crashing() -> None:
    totals, portfolio_total, basis, coverage = handler_module._aggregate_sector_entries(
        [], holding_count=0
    )

    assert basis == PortfolioValuationBasis.UNAVAILABLE
    assert portfolio_total is None
    assert totals == {}
    assert coverage == 0.0


def test_aggregate_sector_entries_exceeding_max_is_unavailable_without_crashing() -> None:
    entries = [
        handler_module._encode_sector_entry(
            BuyIndustrySector.BANK, Decimal("1000"), f"{9000 + i}"
        )
        for i in range(handler_module.MAX_SECTOR_ENTRIES + 1)
    ]

    totals, portfolio_total, basis, _coverage = handler_module._aggregate_sector_entries(
        entries, holding_count=len(entries)
    )

    assert basis == PortfolioValuationBasis.UNAVAILABLE
    assert portfolio_total is None
    assert totals == {}


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
        progress, config, _NOW, repo, fake_service, execution_context
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

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

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
