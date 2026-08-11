import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import (
    AccountType,
    ConfidenceLevel,
    ExecutionMode,
    NotificationStatus,
    RecommendationType,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.lambda_handlers import holdings_watchlist_handler as handler_module
from jstock_advisor.services import holding_decision_service as holding_decision_service_module
from jstock_advisor.services.audit_service import AuditService as RealAuditService
from jstock_advisor.services.line_notification_service import NotificationOutcome

_NOW = dt.datetime(2026, 7, 29, 7, 0, tzinfo=dt.UTC)


class _FakeContext:
    function_name = "jstock-advisor-holdings-watchlist"


def _holding(stock_code: str) -> Holding:
    return Holding(
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        shares=100,
        average_purchase_price=Decimal("1000"),
        total_purchase_amount=Decimal("100000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _watchlist_item(stock_code: str) -> WatchlistItem:
    return WatchlistItem(
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        created_at=_NOW,
        updated_at=_NOW,
    )


class _FakeMarketData:
    def get_latest_price(self, stock_code: str) -> object | None:
        return None


class _FakeProviders:
    market_data = _FakeMarketData()


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handler_module, "build_real_provider_bundle", lambda now, config: _FakeProviders()
    )
    monkeypatch.setattr(handler_module, "build_line_client_from_env", lambda: object())
    monkeypatch.setattr(
        handler_module,
        "LineNotificationService",
        lambda **kwargs: type(
            "_Svc",
            (),
            {
                "notify_data_error": lambda self, *a, **kw: False,
                "notify_recommendation": lambda self, *a, **kw: False,
            },
        )(),
    )


def test_dispatch_mode_dispatches_one_call_per_holding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """統合BUY候補パイプライン(2026-07)への移行後、本ハンドラは保有銘柄の
    売却・利確判定(task="holding")のみをdispatchする。ウォッチリストの
    買いシグナル評価(旧task="watchlist")はbuy_candidates_handler.pyへ
    一本化されたため、このハンドラはもうWatchlistServiceを参照しない
    (回帰確認)。
    """
    _patch_common(monkeypatch)
    holdings = [_holding("2914"), _holding("8136")]

    monkeypatch.setattr(
        handler_module.PortfolioService, "list_holdings", lambda self: holdings
    )
    assert not hasattr(handler_module, "WatchlistService")

    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        handler_module,
        "dispatch_async",
        lambda function_name, payload: dispatched.append({"fn": function_name, **payload}),
    )

    result = handler_module.handler({}, _FakeContext())

    assert result == {"dispatched_holdings": 2}
    assert len(dispatched) == 2
    # 全ディスパッチが同一のbatch_idを共有していることを確認する
    batch_ids = {d["batch_id"] for d in dispatched}
    assert len(batch_ids) == 1

    def _without_batch_id(payload: dict[str, object]) -> dict[str, object]:
        return {k: v for k, v in payload.items() if k != "batch_id"}

    stripped = [_without_batch_id(d) for d in dispatched]
    # 保有銘柄タスクにはポートフォリオ集中リスク判定用の全体集計値が付与される
    # (要求仕様§14)。フェイクのmarket_dataは常にNoneを返すため時価総額ベースは
    # 算出不能(None)、取得価格ベースは2銘柄分(10万円×2)が合算される。
    assert {
        "fn": "jstock-advisor-holdings-watchlist",
        "task": "holding",
        "stock_code": "2914",
        "portfolio_total_market_value": None,
        "portfolio_total_acquisition_cost": "200000",
        "execution_mode": "NORMAL",
    } in stripped
    assert {
        "fn": "jstock-advisor-holdings-watchlist",
        "task": "holding",
        "stock_code": "8136",
        "portfolio_total_market_value": None,
        "portfolio_total_acquisition_cost": "200000",
        "execution_mode": "NORMAL",
    } in stripped


def test_task_holding_processes_only_requested_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)
    target = _holding("2914")

    def _get_holding(self: object, stock_code: str) -> Holding | None:
        return target if stock_code == "2914" else None

    monkeypatch.setattr(handler_module.PortfolioService, "get_holding", _get_holding)
    monkeypatch.setattr(
        handler_module, "build_stock_snapshot", lambda *a, **kw: (None, "テストエラー")
    )

    result = handler_module.handler({"task": "holding", "stock_code": "2914"}, _FakeContext())

    # データ取得エラー時は評価監査ステータス(要求仕様§12)も併せて返す
    assert result == {
        "stock_code": "2914",
        "recommended": False,
        "notified": False,
        "evaluation_status": "DATA_INSUFFICIENT",
        "notification_status": "DATA_INSUFFICIENT",
    }


def test_task_holding_not_found_reports_found_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(handler_module.PortfolioService, "get_holding", lambda self, code: None)

    result = handler_module.handler({"task": "holding", "stock_code": "9999"}, _FakeContext())

    assert result == {
        "stock_code": "9999",
        "recommended": False,
        "notified": False,
        "found": False,
    }


@dataclass(frozen=True)
class _FakeFinancial:
    sector: str | None = None
    industry: str | None = None


@dataclass(frozen=True)
class _FakeSnapshot:
    current_price: Decimal
    financial: _FakeFinancial = field(default_factory=_FakeFinancial)


class _NoSignalOutcome:
    recommendation = None
    data_error = None
    triggered_rule_names: tuple[str, ...] = ()


def test_task_holding_hold_category_and_portfolio_concentration_notified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sell/profit_takingがともに無シグナルでも、単一銘柄で取得価格ベースの保有比率が
    閾値(20%)を超える場合はPORTFOLIO_CONCENTRATION_REVIEW通知が別途送られ(要求仕様§14)、
    かつ評価監査上のカテゴリはNO_SIGNAL相当の"hold"になる(要求仕様§12・§13)。
    """
    _patch_common(monkeypatch)
    target = _holding("2914")
    monkeypatch.setattr(handler_module.PortfolioService, "get_holding", lambda self, code: target)
    monkeypatch.setattr(
        handler_module,
        "build_stock_snapshot",
        lambda *a, **kw: (_FakeSnapshot(current_price=Decimal("1200")), None),
    )
    monkeypatch.setattr(
        handler_module.SellSignalService, "analyze", lambda self, *a, **kw: _NoSignalOutcome()
    )
    monkeypatch.setattr(
        handler_module.ProfitTakingService, "analyze", lambda self, *a, **kw: _NoSignalOutcome()
    )
    # このテストの関心事はkill switchではなく集中リスク通知そのものであるため、
    # RuntimeConfig未初期化時の安全側フォールバック(notification_enabled=False)の
    # 影響を受けないよう明示的にTrueへ固定する(コードレビュー対応でkill switchが
    # 集中リスク通知にも適用されるようになったため)。
    monkeypatch.setattr(
        handler_module.HoldingDecisionRuntimeConfigService,
        "get_notification_enabled",
        lambda self: True,
    )

    notified: list[object] = []

    def _fake_notify_with_status(self, rec, now):
        notified.append(rec)
        return NotificationOutcome(status=NotificationStatus.SENT, sent=True)

    monkeypatch.setattr(
        handler_module,
        "LineNotificationService",
        lambda **kwargs: type(
            "_Svc",
            (),
            {
                "notify_data_error": lambda self, *a, **kw: False,
                "notify_recommendation": lambda self, rec, now: notified.append(rec) or True,
                "notify_recommendation_with_status": _fake_notify_with_status,
            },
        )(),
    )

    result = handler_module.handler(
        {
            "task": "holding",
            "stock_code": "2914",
            # 単一銘柄で全体の取得価格を占めるため取得価格ベースの比率は100%になる
            "portfolio_total_market_value": None,
            "portfolio_total_acquisition_cost": "100000",
        },
        _FakeContext(),
    )

    assert result["evaluation_status"] == "COMPLETED"
    assert len(notified) == 1
    concentration_recommendation = notified[0]
    assert concentration_recommendation.recommendation_type == (
        RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW
    )
    assert concentration_recommendation.portfolio_acquisition_cost_weight_pct == pytest.approx(
        100.0
    )


def test_task_holding_validation_mode_does_not_grow_production_audit_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """通知検証モード コードレビュー対応(Issue 2): 保有銘柄分析(task="holding")を
    VALIDATIONで実行し、HoldingDecisionService.evaluate()が最後まで完了して
    self._audit.record()を実際に経由しても、本番AuditLogRepositoryへは一切
    保存されないことを、実物のAuditService/AuditLogRepositoryを使って(保存先の
    みtmp_pathへ差し替えて)検証する。
    """
    _patch_common(monkeypatch)
    target = _holding("2914")
    monkeypatch.setattr(handler_module.PortfolioService, "get_holding", lambda self, code: target)
    monkeypatch.setattr(
        handler_module,
        "build_stock_snapshot",
        lambda *a, **kw: (_FakeSnapshot(current_price=Decimal("1200")), None),
    )
    monkeypatch.setattr(
        handler_module.SellSignalService, "analyze", lambda self, *a, **kw: _NoSignalOutcome()
    )
    monkeypatch.setattr(
        handler_module.ProfitTakingService, "analyze", lambda self, *a, **kw: _NoSignalOutcome()
    )
    monkeypatch.setattr(
        handler_module.HoldingDecisionRuntimeConfigService,
        "get_notification_enabled",
        lambda self: True,
    )
    monkeypatch.setattr(
        handler_module,
        "LineNotificationService",
        lambda **kwargs: type(
            "_Svc",
            (),
            {
                "notify_data_error": lambda self, *a, **kw: False,
                "notify_recommendation": lambda self, rec, now: True,
                "notify_recommendation_with_status": lambda self, rec, now: NotificationOutcome(
                    status=NotificationStatus.SENT, sent=True
                ),
            },
        )(),
    )

    audit_repo = AuditLogRepository(store_dir=tmp_path)
    monkeypatch.setattr(
        holding_decision_service_module,
        "AuditService",
        lambda *a, **kw: RealAuditService(
            repository=audit_repo, execution_context=kw.get("execution_context")
        ),
    )

    result = handler_module.handler(
        {
            "task": "holding",
            "stock_code": "2914",
            "portfolio_total_market_value": None,
            "portfolio_total_acquisition_cost": "100000",
            "execution_mode": "VALIDATION",
        },
        _FakeContext(),
    )

    assert result["evaluation_status"] == "COMPLETED"
    assert audit_repo.list_all() == []


class _RaisingThenOkMarketData:
    """1銘柄目の価格取得で例外を発生させ、2銘柄目は正常応答するフェイク。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_latest_price(self, stock_code: str) -> object:
        self.calls.append(stock_code)
        if stock_code == "2914":
            raise RuntimeError("yfinance boom")
        return type("_Snap", (), {"close_price": Decimal("1000")})()


class _RaisingProviders:
    def __init__(self, market_data: _RaisingThenOkMarketData) -> None:
        self.market_data = market_data


def test_estimate_portfolio_totals_isolates_single_holding_price_fetch_error() -> None:
    """1銘柄の価格取得が例外を投げても、他の銘柄の処理を止めず、時価総額のみを
    算出不能(None)として扱う(取得価格総額は影響を受けない)。"""
    holdings = [_holding("2914"), _holding("8136")]
    market_data = _RaisingThenOkMarketData()
    providers = _RaisingProviders(market_data)

    total_market_value, total_acquisition_cost = handler_module._estimate_portfolio_totals(
        holdings, providers
    )

    assert total_market_value is None
    assert total_acquisition_cost == Decimal("200000")
    # 例外が発生した銘柄で処理が止まらず、2銘柄目も呼び出されていることを確認する
    assert market_data.calls == ["2914", "8136"]


# --- 通知検証モード機能(2026-08追加) -------------------------------------


def _minimal_recommendation(recommendation_id: str = "rec-1") -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code="2914",
        stock_name="銘柄2914",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.SELL,
        sell_prices=None,
        buy_prices=BuyPriceLevels(
            entry=PriceWithRationale(price=Decimal("1000"), rationale="x"),
            standard=PriceWithRationale(price=Decimal("950"), rationale="x"),
            strong=PriceWithRationale(price=Decimal("900"), rationale="x"),
        ),
        price_at_recommendation=Decimal("1200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


class _SpyRecommendationRepository:
    def __init__(self) -> None:
        self.saved: list[Recommendation] = []

    def save(self, recommendation: Recommendation) -> None:
        self.saved.append(recommendation)

    def get(self, recommendation_id: str) -> Recommendation | None:
        return None


class _SpyHoldingDecisionResultRepository:
    def __init__(self) -> None:
        self.saved: list[object] = []

    def save(self, result: object) -> None:
        self.saved.append(result)


class _AlwaysSendsNotificationService:
    def __init__(self) -> None:
        self.notified: list[Recommendation] = []

    def notify_recommendation_with_status(
        self, recommendation: Recommendation, now: dt.datetime
    ) -> NotificationOutcome:
        self.notified.append(recommendation)
        return NotificationOutcome(status=NotificationStatus.SENT, sent=True)


def test_dispatch_mode_propagates_validation_execution_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        handler_module.PortfolioService, "list_holdings", lambda self: [_holding("2914")]
    )

    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        handler_module,
        "dispatch_async",
        lambda function_name, payload: dispatched.append(payload),
    )

    result = handler_module.handler({"execution_mode": "VALIDATION"}, _FakeContext())

    assert result == {"dispatched_holdings": 1}
    assert dispatched[0]["execution_mode"] == "VALIDATION"


def test_dispatch_mode_unspecified_execution_mode_propagates_normal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        handler_module.PortfolioService, "list_holdings", lambda self: [_holding("2914")]
    )

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
    called: list[str] = []
    monkeypatch.setattr(
        handler_module, "load_config", lambda: called.append("load_config")
    )

    with pytest.raises(ValueError, match="unknown execution_mode"):
        handler_module.handler({"execution_mode": "BOGUS"}, _FakeContext())

    assert called == []


def test_evaluate_portfolio_concentration_and_notify_validation_mode_skips_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holding = _holding("2914")
    repo = _SpyRecommendationRepository()
    notification_service = _AlwaysSendsNotificationService()

    handler_module._evaluate_portfolio_concentration_and_notify(
        holding,
        Decimal("1000"),
        None,
        Decimal("100000"),
        handler_module.load_config(),
        repo,
        notification_service,
        handler_module.RuleVersionService(),
        _NOW,
        True,
        ExecutionContext(mode=ExecutionMode.VALIDATION),
    )

    assert repo.saved == []
    assert len(notification_service.notified) == 1


def test_evaluate_portfolio_concentration_and_notify_normal_mode_still_saves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NORMAL回帰確認: VALIDATION対応追加後もRecommendation保存は従来通り行われる。"""
    holding = _holding("2914")
    repo = _SpyRecommendationRepository()
    notification_service = _AlwaysSendsNotificationService()

    handler_module._evaluate_portfolio_concentration_and_notify(
        holding,
        Decimal("1000"),
        None,
        Decimal("100000"),
        handler_module.load_config(),
        repo,
        notification_service,
        handler_module.RuleVersionService(),
        _NOW,
        True,
    )

    assert len(repo.saved) == 1


def test_notify_legacy_sell_validation_mode_skips_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holding = _holding("2914")
    repo = _SpyRecommendationRepository()
    notification_service = _AlwaysSendsNotificationService()
    recommendation = _minimal_recommendation()

    snapshot_calls: list[object] = []
    monkeypatch.setattr(
        handler_module,
        "save_decision_snapshot_safely",
        lambda *a, **kw: snapshot_calls.append(a),
    )

    result = handler_module._notify_legacy_sell_and_build_result(
        holding,
        _NOW,
        recommendation,
        repo,
        notification_service,
        True,
        ExecutionContext(mode=ExecutionMode.VALIDATION),
    )

    assert repo.saved == []
    assert snapshot_calls == []
    assert result.notified is True  # LINE送信自体は行われる


def test_notify_legacy_sell_normal_mode_still_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    """NORMAL回帰確認。"""
    holding = _holding("2914")
    repo = _SpyRecommendationRepository()
    notification_service = _AlwaysSendsNotificationService()
    recommendation = _minimal_recommendation()

    snapshot_calls: list[object] = []
    monkeypatch.setattr(
        handler_module,
        "save_decision_snapshot_safely",
        lambda *a, **kw: snapshot_calls.append(a),
    )

    handler_module._notify_legacy_sell_and_build_result(
        holding, _NOW, recommendation, repo, notification_service, True,
    )

    assert len(repo.saved) == 1
    assert len(snapshot_calls) == 1
