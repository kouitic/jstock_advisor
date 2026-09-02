import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
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
from jstock_advisor.domain.entities.notification_eligibility import NotificationEligibility
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.domain.market_session import (
    expected_latest_completed_trading_session,
)
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
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
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
    monkeypatch.setattr(
        handler_module, "build_real_provider_bundle", lambda now, config: _FakeProviders()
    )
    monkeypatch.setattr(handler_module, "build_line_client_from_env", lambda: object())
    monkeypatch.setattr(handler_module, "TradeCooldownService", _FakeTradeCooldownService)
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
        "holding_id": build_holding_id(DEFAULT_OWNER, "2914"),
        "portfolio_total_market_value": None,
        "portfolio_total_acquisition_cost": "200000",
        "execution_mode": "NORMAL",
        "trade_detection_confirmed": True,
    } in stripped
    assert {
        "fn": "jstock-advisor-holdings-watchlist",
        "task": "holding",
        "holding_id": build_holding_id(DEFAULT_OWNER, "8136"),
        "portfolio_total_market_value": None,
        "portfolio_total_acquisition_cost": "200000",
        "execution_mode": "NORMAL",
        "trade_detection_confirmed": True,
    } in stripped


def test_task_holding_processes_only_requested_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)
    target = _holding("2914")

    def _get_holding(self: object, holding_id: str) -> Holding | None:
        return target if holding_id == build_holding_id(DEFAULT_OWNER, "2914") else None

    monkeypatch.setattr(handler_module.HoldingRepository, "get", _get_holding)
    monkeypatch.setattr(
        handler_module, "build_stock_snapshot", lambda *a, **kw: (None, "テストエラー")
    )

    result = handler_module.handler(
        {"task": "holding", "holding_id": build_holding_id(DEFAULT_OWNER, "2914")}, _FakeContext()
    )

    # データ取得エラー時は評価監査ステータス(要求仕様§12)も併せて返す
    assert result == {
        "holding_id": build_holding_id(DEFAULT_OWNER, "2914"),
        "recommended": False,
        "notified": False,
        "evaluation_status": "DATA_INSUFFICIENT",
        "notification_status": "DATA_INSUFFICIENT",
    }


def test_task_holding_not_found_reports_found_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(handler_module.HoldingRepository, "get", lambda self, holding_id: None)

    result = handler_module.handler(
        {"task": "holding", "holding_id": build_holding_id(DEFAULT_OWNER, "9999")}, _FakeContext()
    )

    assert result == {
        "holding_id": build_holding_id(DEFAULT_OWNER, "9999"),
        "recommended": False,
        "notified": False,
        "found": False,
    }


@dataclass(frozen=True)
class _FakeFinancial:
    sector: str | None = None
    industry: str | None = None

def _fresh_price_as_of_date(now: dt.datetime | None = None) -> dt.date:
    """`now` 時点で期待される直近の完了済みセッション。鮮度が正常な状態を表す。

    Issue #52 Phase B2 review: 以前は「未来日ならmissed=0になる」性質を利用して
    JST暦日の当日を既定にしていたが、**未来日は正常な値ではない**
    (policy層でtimestamp異常として弾かれるようになった)。
    異常値を使って正常系fixtureを作らない。

    本モジュールには `_NOW` を使うテストと実時刻を使うテストが混在するため、
    既定では実時刻から導出する。`_NOW` を使うテストは明示的に渡すこと。
    """
    return expected_latest_completed_trading_session(
        now or dt.datetime.now(dt.UTC),
        BusinessCalendar.from_config(load_config().holiday_calendar),
    )



@dataclass(frozen=True)
class _FakeSnapshot:
    current_price: Decimal
    financial: _FakeFinancial = field(default_factory=_FakeFinancial)
    # Issue #52 Phase B2: 価格の基準日。
    #
    # 本モジュールの既存テストは価格鮮度**以外**の挙動(attention検出・集中リスク通知・
    # 利確判定等)を対象としているため、既定では鮮度が正常な状態を表す。
    #
    # 実時刻から期待セッションを導出する(未来日は使わない)。
    # `_NOW` を使うテストでは `_NOW` 時点の期待セッションより過去になるが、
    # `_NOW`(2026-07-29)より後の日付は `_NOW` 基準では未来日になってしまうため、
    # `_NOW` を使う経路は価格鮮度gateを通らないもの(handler を直接呼ばない
    # 単体テスト)に限られることを前提とする。
    #
    # 鮮度そのものを検証するテストは price_as_of_date を明示的に古くすること。
    price_as_of_date: dt.date = field(default_factory=_fresh_price_as_of_date)


class _NoSignalOutcome:
    recommendation = None
    data_error = None
    triggered_rule_names: tuple[str, ...] = ()
    audit_id: str | None = None


def test_task_holding_hold_category_and_portfolio_concentration_notified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sell/profit_takingがともに無シグナルでも、単一銘柄で取得価格ベースの保有比率が
    閾値(20%)を超える場合はPORTFOLIO_CONCENTRATION_REVIEW通知が別途送られ(要求仕様§14)、
    かつ評価監査上のカテゴリはNO_SIGNAL相当の"hold"になる(要求仕様§12・§13)。
    """
    _patch_common(monkeypatch)
    target = _holding("2914")
    monkeypatch.setattr(handler_module.HoldingRepository, "get", lambda self, holding_id: target)
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
            "holding_id": build_holding_id(DEFAULT_OWNER, "2914"),
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
    monkeypatch.setattr(handler_module.HoldingRepository, "get", lambda self, holding_id: target)
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
            "holding_id": build_holding_id(DEFAULT_OWNER, "2914"),
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
        handler_module.PortfolioService,
        "list_holdings",
        lambda self: [_holding("2914"), _holding("8306")],
    )

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
        handler_module.PortfolioService, "list_holdings", lambda self: [_holding("2914")]
    )

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
    assert "notification_mode" not in dispatched[0]


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


# ===== 再コードレビュー対応(2026-08、detected/sent一元化): DataQuality境界 =====
#
# _HoldingResult.detected_recommendation_type/recommendation_type_at_sendの
# 算出式(_notify_legacy_sell_and_build_result・_notify_holding_decision_and_
# build_result・profit_taking経路の3箇所すべてで同一)を、_notify_legacy_sell_
# and_build_result()を白箱の検証窓口として使い、様々なNotificationOutcomeの
# 組み合わせで直接検証する。recommendation_typeは実際にどのパイプライン由来かは
# 問わず(このテストの関心事は算出式そのものであり、業務的な発生経路の妥当性は
# test_holdings_watchlist_handler_integration.pyの分類テストで別途検証済み)。


class _FakeControllableNotificationService:
    """notification_enabled=Trueの経路(notify_recommendation_with_status)と、
    notification_enabled=Falseの経路(check_data_quality_eligibility、再コード
    レビュー対応2026-08・追加修正3)の両方を独立して制御できるフェイク。"""

    def __init__(
        self,
        outcome: NotificationOutcome,
        data_quality_eligibility: NotificationEligibility | None = None,
    ) -> None:
        self._outcome = outcome
        self._data_quality_eligibility = data_quality_eligibility or NotificationEligibility(
            eligible=True
        )
        self.calls = 0
        self.data_quality_eligibility_calls = 0

    def notify_recommendation_with_status(
        self, recommendation: Recommendation, now: dt.datetime
    ) -> NotificationOutcome:
        self.calls += 1
        return self._outcome

    def check_data_quality_eligibility(
        self, recommendation: Recommendation, now: dt.datetime
    ) -> NotificationEligibility:
        self.data_quality_eligibility_calls += 1
        return self._data_quality_eligibility


def _recommendation_of_type(recommendation_type: RecommendationType) -> Recommendation:
    return _minimal_recommendation().model_copy(update={"recommendation_type": recommendation_type})


def test_detected_and_sent_when_sent_successfully() -> None:
    """指摘10-A: PARTIAL検出+個別送信成功 → detected=PARTIAL/sent=PARTIAL。"""
    holding = _holding("2914")
    repo = _SpyRecommendationRepository()
    outcome = NotificationOutcome(
        status=NotificationStatus.SENT, sent=True, data_quality_blocked=False
    )
    notification_service = _FakeControllableNotificationService(outcome)
    recommendation = _recommendation_of_type(RecommendationType.PARTIAL_PROFIT_TAKE)

    result = handler_module._notify_legacy_sell_and_build_result(
        holding, _NOW, recommendation, repo, notification_service, True,
        ExecutionContext(mode=ExecutionMode.VALIDATION),
    )

    assert result.detected_recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.recommendation_type_at_send == RecommendationType.PARTIAL_PROFIT_TAKE


def test_detected_but_not_sent_when_trade_cooldown_blocks() -> None:
    """指摘10-B: PARTIAL検出+TradeCooldown抑止 → detected=PARTIAL/sent=None。"""
    holding = _holding("2914")
    repo = _SpyRecommendationRepository()
    outcome = NotificationOutcome(
        status=NotificationStatus.NOT_REQUIRED,
        sent=False,
        data_quality_blocked=False,
        block_reason="TRADE_COOLDOWN",
    )
    notification_service = _FakeControllableNotificationService(outcome)
    recommendation = _recommendation_of_type(RecommendationType.PARTIAL_PROFIT_TAKE)

    result = handler_module._notify_legacy_sell_and_build_result(
        holding, _NOW, recommendation, repo, notification_service, True,
        ExecutionContext(mode=ExecutionMode.VALIDATION),
    )

    assert result.detected_recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE
    assert result.recommendation_type_at_send is None


def test_detected_but_not_sent_when_cross_pipeline_priority_blocks() -> None:
    """指摘10-C: FULL検出+CrossPipelinePriority抑止 → detected=FULL/sent=None。"""
    holding = _holding("2914")
    repo = _SpyRecommendationRepository()
    outcome = NotificationOutcome(
        status=NotificationStatus.NOT_REQUIRED,
        sent=False,
        data_quality_blocked=False,
        block_reason="LOW_PRIORITY",
    )
    notification_service = _FakeControllableNotificationService(outcome)
    recommendation = _recommendation_of_type(RecommendationType.FULL_PROFIT_TAKE)

    result = handler_module._notify_legacy_sell_and_build_result(
        holding, _NOW, recommendation, repo, notification_service, True,
        ExecutionContext(mode=ExecutionMode.VALIDATION),
    )

    assert result.detected_recommendation_type == RecommendationType.FULL_PROFIT_TAKE
    assert result.recommendation_type_at_send is None


def test_detected_but_not_sent_when_dedup_blocks() -> None:
    """指摘10-D: SELL検出+再通知抑止(dedup) → detected=SELL/sent=None。"""
    holding = _holding("2914")
    repo = _SpyRecommendationRepository()
    outcome = NotificationOutcome(
        status=NotificationStatus.DUPLICATE_SUPPRESSED, sent=False, data_quality_blocked=False
    )
    notification_service = _FakeControllableNotificationService(outcome)
    recommendation = _recommendation_of_type(RecommendationType.SELL)

    result = handler_module._notify_legacy_sell_and_build_result(
        holding, _NOW, recommendation, repo, notification_service, True,
        ExecutionContext(mode=ExecutionMode.VALIDATION),
    )

    assert result.detected_recommendation_type == RecommendationType.SELL
    assert result.recommendation_type_at_send is None


def test_notification_disabled_actionable_data_quality_ok_is_detected_but_not_sent() -> None:
    """指摘10-E→DQ-1(再コードレビュー対応2026-08・追加修正1/3):
    notification_enabled=False + ACTIONABLE(CRITICAL) + DataQuality OK
    → detected=CRITICAL/sent=None。

    notification_enabled=Falseの間、_send_or_suppress_notification()は
    notify_recommendation_with_status()(実送信経路)を一切呼ばないが、ACTIONABLE/
    ATTENTION対象についてはcheck_data_quality_eligibility()(副作用の無い読み取り
    専用メソッド)でDataQualityだけを評価することを確認する。
    """
    holding = _holding("2914")
    repo = _SpyRecommendationRepository()
    outcome = NotificationOutcome(
        status=NotificationStatus.SENT, sent=True, data_quality_blocked=False
    )
    notification_service = _FakeControllableNotificationService(
        outcome, data_quality_eligibility=NotificationEligibility(eligible=True)
    )
    recommendation = _recommendation_of_type(RecommendationType.URGENT_HOLDING_REVIEW)

    result = handler_module._notify_legacy_sell_and_build_result(
        holding,
        _NOW,
        recommendation,
        repo,
        notification_service,
        False,  # notification_enabled=False
        ExecutionContext(mode=ExecutionMode.VALIDATION),
    )

    # 実送信経路(notify_recommendation_with_status)は呼ばない
    assert notification_service.calls == 0
    assert notification_service.data_quality_eligibility_calls == 1
    assert result.detected_recommendation_type == RecommendationType.URGENT_HOLDING_REVIEW
    assert result.recommendation_type_at_send is None


def test_notification_disabled_actionable_data_quality_blocked_is_not_detected() -> None:
    """DQ-2: notification_enabled=False + ACTIONABLE(SELL) + DataQuality BLOCKED
    → detected=None/sent=None。"""
    holding = _holding("2914")
    repo = _SpyRecommendationRepository()
    outcome = NotificationOutcome(
        status=NotificationStatus.SENT, sent=True, data_quality_blocked=False
    )
    notification_service = _FakeControllableNotificationService(
        outcome,
        data_quality_eligibility=NotificationEligibility(eligible=False),
    )
    recommendation = _recommendation_of_type(RecommendationType.SELL)

    result = handler_module._notify_legacy_sell_and_build_result(
        holding, _NOW, recommendation, repo, notification_service, False,
        ExecutionContext(mode=ExecutionMode.VALIDATION),
    )

    assert notification_service.calls == 0
    assert notification_service.data_quality_eligibility_calls == 1
    assert result.detected_recommendation_type is None
    assert result.recommendation_type_at_send is None


def test_notification_disabled_internal_only_skips_data_quality_evaluation() -> None:
    """DQ-5: notification_enabled=False + INTERNAL_ONLY(通常WATCH) →
    detected目的の不要なDataQuality評価を行わない
    (check_data_quality_eligibility()自体を呼ばない)ことを確認する。

    WATCH(INTERNAL_ONLY)はresolve_holding_summary_action()がNoneを返す型のため
    (test_enums.pyで別途確認済み)、result.detected_recommendation_type自体には
    値が残っても(data_quality_blocked=Falseのまま、Audit上の意味は不変)、
    保有株サマリーの一部売却/全部売却/売却/緊急確認のいずれの集計にも含まれない。
    ここで確認すべきは「DataQuality評価(check_data_quality_eligibility)自体が
    detected目的では一切呼ばれない」ことである。
    """
    holding = _holding("2914")
    repo = _SpyRecommendationRepository()
    outcome = NotificationOutcome(
        status=NotificationStatus.SENT, sent=True, data_quality_blocked=False
    )
    notification_service = _FakeControllableNotificationService(outcome)
    recommendation = _recommendation_of_type(RecommendationType.WATCH)

    result = handler_module._notify_legacy_sell_and_build_result(
        holding, _NOW, recommendation, repo, notification_service, False,
        ExecutionContext(mode=ExecutionMode.VALIDATION),
    )

    assert notification_service.calls == 0
    assert notification_service.data_quality_eligibility_calls == 0
    assert result.detected_recommendation_type == RecommendationType.WATCH
    summary_action = handler_module.resolve_holding_summary_action(
        result.detected_recommendation_type
    )
    assert summary_action is None
    assert result.recommendation_type_at_send is None


def test_data_quality_blocked_is_excluded_from_detected() -> None:
    """指摘10-F: DataQuality BLOCKEDのACTIONABLE → action detectedへ含めない。

    このケースはoutcome.sent=Trueであっても(手動確認メッセージ自体はLINE
    送信されている可能性がある)、data_quality_blocked=Trueの場合はdetected/
    sentいずれからも除外する(NotificationOutcomeのdocstringが要求する
    「呼び出し側の責務」を果たす)。
    """
    holding = _holding("2914")
    repo = _SpyRecommendationRepository()
    outcome = NotificationOutcome(
        status=NotificationStatus.SENT, sent=True, data_quality_blocked=True
    )
    notification_service = _FakeControllableNotificationService(outcome)
    recommendation = _recommendation_of_type(RecommendationType.SELL)

    result = handler_module._notify_legacy_sell_and_build_result(
        holding, _NOW, recommendation, repo, notification_service, True,
        ExecutionContext(mode=ExecutionMode.VALIDATION),
    )

    assert result.detected_recommendation_type is None
    assert result.recommendation_type_at_send is None
    assert result.audit.evaluation_status.value == "DATA_QUALITY_BLOCKED"


# ===== ATTENTION専用のdetected/sent(指摘10-G・H・I) =====
#
# profit_taking経路(_analyze_one_holding内、単独の呼び出し可能関数に切り出されて
# いない)を、handler_module.handler()経由のディスパッチ全体+_finish_batch_item
# スパイで検証する(test_task_holding_hold_category_and_portfolio_concentration_
# notifiedと同じ、既存のprofit_taking到達パターンを再利用)。


def _attention_watch_recommendation(signal: str = "CANDIDATE") -> Recommendation:
    return Recommendation(
        recommendation_id="pt-attention-1",
        stock_code="2914",
        stock_name="銘柄2914",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.WATCH,
        price_at_recommendation=Decimal("1400"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        profit_protection_signal=signal,
        profit_protection_basis_date=dt.date(2026, 6, 1),
        profit_protection_peak_date=dt.date(2026, 6, 10),
        profit_protection_peak_price=Decimal("1500"),
        profit_protection_peak_gain_pct=58.1,
        profit_protection_current_gain_pct=33.4,
        profit_protection_drawdown_from_peak_pct=15.6,
        profit_protection_gain_giveback_ratio_pct=42.5,
    )


class _FakeProfitTakingOutcome:
    def __init__(self, recommendation: Recommendation) -> None:
        self.recommendation = recommendation
        self.stock_code = recommendation.stock_code
        self.data_error = None
        self.audit_id: str | None = None


def _run_attention_scenario(
    monkeypatch: pytest.MonkeyPatch,
    outcome: NotificationOutcome,
    signal: str = "CANDIDATE",
    notification_enabled: bool = True,
    data_quality_eligibility: NotificationEligibility | None = None,
) -> dict[str, object]:
    """data_quality_eligibility: notification_enabled=False時に
    check_data_quality_eligibility()が返す値(再コードレビュー対応2026-08・
    追加修正1/3)。notification_enabled=True時は使われない(outcomeがそのまま
    notify_recommendation_with_status()の戻り値になる)。"""
    _patch_common(monkeypatch)
    target = _holding("2914")
    monkeypatch.setattr(handler_module.HoldingRepository, "get", lambda self, holding_id: target)
    monkeypatch.setattr(
        handler_module,
        "build_stock_snapshot",
        lambda *a, **kw: (_FakeSnapshot(current_price=Decimal("1400")), None),
    )
    monkeypatch.setattr(
        handler_module.SellSignalService, "analyze", lambda self, *a, **kw: _NoSignalOutcome()
    )
    recommendation = _attention_watch_recommendation(signal)
    monkeypatch.setattr(
        handler_module.ProfitTakingService,
        "analyze",
        lambda self, *a, **kw: _FakeProfitTakingOutcome(recommendation),
    )
    monkeypatch.setattr(
        handler_module.HoldingDecisionRuntimeConfigService,
        "get_notification_enabled",
        lambda self: notification_enabled,
    )
    eligibility = data_quality_eligibility or NotificationEligibility(eligible=True)
    monkeypatch.setattr(
        handler_module,
        "LineNotificationService",
        lambda **kwargs: type(
            "_Svc",
            (),
            {
                "notify_data_error": lambda self, *a, **kw: False,
                "notify_recommendation_with_status": lambda self, rec, now: outcome,
                "check_data_quality_eligibility": lambda self, rec, now: eligibility,
            },
        )(),
    )
    captured: dict[str, object] = {}

    def _fake_finish_batch_item(batch_id, category, stock_code, now, *args, **kwargs):
        captured.update(kwargs)
        captured["recommendation_type"] = kwargs.get("recommendation_type")

    monkeypatch.setattr(handler_module, "_finish_batch_item", _fake_finish_batch_item)

    handler_module.handler(
        {
            "task": "holding",
            "holding_id": build_holding_id(DEFAULT_OWNER, "2914"),
            "portfolio_total_market_value": "100000",
            "portfolio_total_acquisition_cost": "100000",
            # VALIDATION: Recommendation保存を実行しない(実ローカルストアを
            # 汚染しない、かつ固定recommendation_idの複数テスト間再利用を許容する)。
            "execution_mode": "VALIDATION",
        },
        _FakeContext(),
    )
    return captured


def test_attention_detected_and_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """指摘10-G: ATTENTION検出+個別送信成功 → attention_detected=True/sent=True。"""
    outcome = NotificationOutcome(
        status=NotificationStatus.SENT,
        sent=True,
        data_quality_blocked=False,
        notification_intent=None,
    )
    captured = _run_attention_scenario(monkeypatch, outcome)

    assert captured["attention_detected"] is True
    assert captured["attention_sent"] is True


def test_attention_detected_but_not_sent_when_dedup_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """指摘10-H: ATTENTION継続event+dedup抑止 → attention_detected=True/sent=False。"""
    outcome = NotificationOutcome(
        status=NotificationStatus.DUPLICATE_SUPPRESSED, sent=False, data_quality_blocked=False
    )
    captured = _run_attention_scenario(monkeypatch, outcome)

    assert captured["attention_detected"] is True
    assert captured["attention_sent"] is False


def test_attention_excluded_from_detected_when_data_quality_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """指摘10-I: ATTENTION+DataQuality BLOCKED → attention_detected/sentとも
    Falseとなる(追加修正1のDataQuality境界定義をATTENTIONにも一貫適用)。"""
    outcome = NotificationOutcome(
        status=NotificationStatus.SENT, sent=True, data_quality_blocked=True
    )
    captured = _run_attention_scenario(monkeypatch, outcome)

    assert captured["attention_detected"] is False
    assert captured["attention_sent"] is False
    assert captured["detected_recommendation_type"] is None


def test_normal_watch_is_not_counted_as_attention(monkeypatch: pytest.MonkeyPatch) -> None:
    """指摘10-J: 通常WATCH(profit_protection_signal無し)はattention_detected/
    sentのいずれにも計上されない(将来の回帰防止、resolver単体での確認)。"""
    outcome = NotificationOutcome(status=NotificationStatus.NOT_REQUIRED, sent=False)
    captured = _run_attention_scenario(monkeypatch, outcome, signal="NONE")

    assert captured["attention_detected"] is False
    assert captured["attention_sent"] is False


def test_notification_disabled_attention_data_quality_ok_is_detected_but_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DQ-3: notification_enabled=False + ATTENTION + DataQuality OK
    → attention_detected=True/attention_sent=False。"""
    outcome = NotificationOutcome(
        status=NotificationStatus.SENT, sent=True, data_quality_blocked=False
    )
    captured = _run_attention_scenario(
        monkeypatch,
        outcome,
        notification_enabled=False,
        data_quality_eligibility=NotificationEligibility(eligible=True),
    )

    assert captured["attention_detected"] is True
    assert captured["attention_sent"] is False


def test_notification_disabled_attention_data_quality_blocked_is_not_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DQ-4: notification_enabled=False + ATTENTION + DataQuality BLOCKED
    → attention_detected=False/attention_sent=False。"""
    outcome = NotificationOutcome(
        status=NotificationStatus.SENT, sent=True, data_quality_blocked=False
    )
    captured = _run_attention_scenario(
        monkeypatch,
        outcome,
        notification_enabled=False,
        data_quality_eligibility=NotificationEligibility(eligible=False),
    )

    assert captured["attention_detected"] is False
    assert captured["attention_sent"] is False


# ===== 追加修正3: _finish_batch_item → record_result() のIF引数を直接検証 =====
#
# BatchProgressを偽造してsummaryだけを見るテスト(既存test_holdings_watchlist_
# handler_integration.pyの_finish_batch_item_with_notification_categories等)とは
# 別に、判定結果(_HoldingResult/handler()の実行結果)がrecord_result()へ渡す
# 実引数を直接assertする。record_result()自体(handler_module.record_result)を
# monkeypatchし、_finish_batch_item()は実物をそのまま実行させる。


def _capture_record_result(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def _fake_record_result(batch_id, category, stock_code=None, **kwargs):
        captured["batch_id"] = batch_id
        captured["category"] = category
        captured["stock_code"] = stock_code
        captured.update(kwargs)
        return None  # progress未完了相当。_finish_batch_item()はこの直後に早期returnする

    monkeypatch.setattr(handler_module, "record_result", _fake_record_result)
    return captured


def test_record_result_if_a_notification_disabled_actionable_data_quality_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IF-A: notification_enabled=False + ACTIONABLE(SELL) + DataQuality OK
    → detected_category_entryは設定され、notification_category_entry/
    attention_detected_stock_code/attention_sent_stock_codeはいずれもNone。"""
    holding = _holding("2914")
    repo = _SpyRecommendationRepository()
    outcome = NotificationOutcome(
        status=NotificationStatus.SENT, sent=True, data_quality_blocked=False
    )
    notification_service = _FakeControllableNotificationService(
        outcome, data_quality_eligibility=NotificationEligibility(eligible=True)
    )
    recommendation = _recommendation_of_type(RecommendationType.SELL)

    result = handler_module._notify_legacy_sell_and_build_result(
        holding, _NOW, recommendation, repo, notification_service, False,
        ExecutionContext(mode=ExecutionMode.VALIDATION),
    )

    captured = _capture_record_result(monkeypatch)
    handler_module._finish_batch_item(
        "test-batch-if-a",
        result.category,
        holding.stock_code,
        _NOW,
        None,
        None,
        recommendation_type=result.recommendation_type_at_send,
        detected_recommendation_type=result.detected_recommendation_type,
        attention_detected=result.attention_detected,
        attention_sent=result.attention_sent,
    )

    assert captured["detected_category_entry"] == f"{RecommendationType.SELL.value}|2914"
    assert captured["notification_category_entry"] is None
    assert captured["attention_detected_stock_code"] is None
    assert captured["attention_sent_stock_code"] is None


def test_record_result_if_b_notification_disabled_actionable_data_quality_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IF-B: notification_enabled=False + ACTIONABLE(SELL) + DataQuality BLOCKED
    → detected_category_entry/notification_category_entryともにNone。"""
    holding = _holding("2914")
    repo = _SpyRecommendationRepository()
    outcome = NotificationOutcome(
        status=NotificationStatus.SENT, sent=True, data_quality_blocked=False
    )
    notification_service = _FakeControllableNotificationService(
        outcome, data_quality_eligibility=NotificationEligibility(eligible=False)
    )
    recommendation = _recommendation_of_type(RecommendationType.SELL)

    result = handler_module._notify_legacy_sell_and_build_result(
        holding, _NOW, recommendation, repo, notification_service, False,
        ExecutionContext(mode=ExecutionMode.VALIDATION),
    )

    captured = _capture_record_result(monkeypatch)
    handler_module._finish_batch_item(
        "test-batch-if-b",
        result.category,
        holding.stock_code,
        _NOW,
        None,
        None,
        recommendation_type=result.recommendation_type_at_send,
        detected_recommendation_type=result.detected_recommendation_type,
        attention_detected=result.attention_detected,
        attention_sent=result.attention_sent,
    )

    assert captured["detected_category_entry"] is None
    assert captured["notification_category_entry"] is None


def _run_attention_scenario_and_capture_record_result(
    monkeypatch: pytest.MonkeyPatch, eligibility: NotificationEligibility
) -> dict[str, object]:
    """_run_attention_scenario()と同じセットアップだが、_finish_batch_item()自体は
    実物をそのまま実行させ、その内部で呼ばれるrecord_result()の実引数を捕捉する
    (追加修正3: _finish_batch_item → record_result のIF引数を直接検証)。"""
    _patch_common(monkeypatch)
    target = _holding("2914")
    monkeypatch.setattr(handler_module.HoldingRepository, "get", lambda self, holding_id: target)
    monkeypatch.setattr(
        handler_module,
        "build_stock_snapshot",
        lambda *a, **kw: (_FakeSnapshot(current_price=Decimal("1400")), None),
    )
    monkeypatch.setattr(
        handler_module.SellSignalService, "analyze", lambda self, *a, **kw: _NoSignalOutcome()
    )
    recommendation = _attention_watch_recommendation("CANDIDATE")
    monkeypatch.setattr(
        handler_module.ProfitTakingService,
        "analyze",
        lambda self, *a, **kw: _FakeProfitTakingOutcome(recommendation),
    )
    monkeypatch.setattr(
        handler_module.HoldingDecisionRuntimeConfigService,
        "get_notification_enabled",
        lambda self: False,
    )
    monkeypatch.setattr(
        handler_module,
        "LineNotificationService",
        lambda **kwargs: type(
            "_Svc",
            (),
            {
                "notify_data_error": lambda self, *a, **kw: False,
                "notify_recommendation_with_status": lambda self, rec, now: NotificationOutcome(
                    status=NotificationStatus.SENT, sent=True, data_quality_blocked=False
                ),
                "check_data_quality_eligibility": lambda self, rec, now: eligibility,
            },
        )(),
    )
    captured = _capture_record_result(monkeypatch)

    handler_module.handler(
        {
            "task": "holding",
            "holding_id": build_holding_id(DEFAULT_OWNER, "2914"),
            "portfolio_total_market_value": "100000",
            "portfolio_total_acquisition_cost": "100000",
            "batch_id": "test-batch-if-attention",
            "execution_mode": "VALIDATION",
        },
        _FakeContext(),
    )
    return captured


def test_record_result_if_c_notification_disabled_attention_data_quality_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IF-C: notification_enabled=False + ATTENTION + DataQuality OK →
    attention_detected_stock_codeは設定され、attention_sent_stock_codeはNone。"""
    captured = _run_attention_scenario_and_capture_record_result(
        monkeypatch, NotificationEligibility(eligible=True)
    )

    assert captured["attention_detected_stock_code"] == build_holding_id(DEFAULT_OWNER, "2914")
    assert captured["attention_sent_stock_code"] is None


def test_record_result_if_d_notification_disabled_attention_data_quality_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IF-D: notification_enabled=False + ATTENTION + DataQuality BLOCKED →
    attention_detected_stock_code/attention_sent_stock_codeともにNone。"""
    captured = _run_attention_scenario_and_capture_record_result(
        monkeypatch, NotificationEligibility(eligible=False)
    )

    assert captured["attention_detected_stock_code"] is None
    assert captured["attention_sent_stock_code"] is None


# ===== M3.1: 複数owner・BatchProgressの命名整理 =====


def test_finish_batch_item_sends_single_summary_for_multiple_owners_of_same_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本人#8306・子供#8306という2件のholding_idがBatchProgress上で別集計されて
    いても(test_record_result_counts_two_owners_of_same_stock_as_separate_entries
    参照)、バッチ完了時のユーザー向けサマリー通知は従来どおり1通だけ送信される
    (必須テストH)。"""
    from jstock_advisor.infrastructure.aws.batch_tracker import BatchProgress

    call_count = {"n": 0}
    honnin_holding_id = build_holding_id(DEFAULT_OWNER, "8306")
    kodomo_holding_id = build_holding_id("子供", "8306")
    detected_entries = [
        f"{RecommendationType.PARTIAL_PROFIT_TAKE.value}|{honnin_holding_id}",
        f"{RecommendationType.PARTIAL_PROFIT_TAKE.value}|{kodomo_holding_id}",
    ]

    def _fake_record_result(batch_id, category, stock_code=None, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 2:
            return None  # 未完了(1件目)
        return BatchProgress(
            total=2,
            completed=2,
            category_counts={"sent": 2},
            data_insufficient_stock_codes=[],
            failed_stock_codes=[],
            ranking_entries=[],
            sector_entries=[],
            holding_count=1,  # 8306はユニーク銘柄コードとしては1件
            notification_categories=detected_entries,
            detected_categories=detected_entries,
        )

    monkeypatch.setattr(handler_module, "record_result", _fake_record_result)

    summary_calls: list[dict[str, object]] = []

    class _FakeNotificationServiceForSummary:
        def notify_batch_summary(self, *args: object, **kwargs: object) -> bool:
            summary_calls.append(kwargs)
            return True

    class _AlwaysEnabledRuntimeConfigService:
        def get_notification_enabled(self) -> bool:
            return True

    notification_service = _FakeNotificationServiceForSummary()
    runtime_config_service = _AlwaysEnabledRuntimeConfigService()

    handler_module._finish_batch_item(
        "batch-multi-owner",
        "sent",
        honnin_holding_id,
        _NOW,
        notification_service,
        runtime_config_service,
        recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
        detected_recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
    )
    handler_module._finish_batch_item(
        "batch-multi-owner",
        "sent",
        kodomo_holding_id,
        _NOW,
        notification_service,
        runtime_config_service,
        recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
        detected_recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
    )

    # 2 owner分の検出があっても、物理的なLINE送信(notify_batch_summary呼び出し)は
    # バッチ完了時に1回だけ。
    assert len(summary_calls) == 1
    assert summary_calls[0]["partial_sell_detected_count"] == 2


# --- Issue #31: holdings側summary finalize-onceゲート --------------------------


def _issue31_completed_progress() -> object:
    from jstock_advisor.infrastructure.aws.batch_tracker import BatchProgress

    return BatchProgress(
        total=1,
        completed=2,  # 処理済みholding_idのretryでcompleted>totalとなった状態を再現
        category_counts={"sent": 1},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[],
        sector_entries=[],
        holding_count=1,
    )


class _Issue31FakeNotificationService:
    def __init__(self) -> None:
        self.summary_calls: list[str] = []

    def notify_batch_summary(self, process_name, *args, **kwargs) -> bool:
        self.summary_calls.append(process_name)
        return True


class _Issue31FakeRuntimeConfig:
    def get_notification_enabled(self) -> bool:
        return True


def test_issue31_holdings_summary_runs_once_under_duplicate_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """J/L: 複数のworkerトリガーがis_complete==Trueを観測しても、
    try_acquire_completion_finalizeに成功した1実行だけがsummaryフローへ進み、
    正常終了後にmark_completion_finalize_completedが自分のtokenで1回だけ
    呼ばれる(Issue #31)。"""
    monkeypatch.setattr(
        handler_module, "record_result", lambda *a, **kw: _issue31_completed_progress()
    )
    acquire_results = iter(["issue31-token", None])
    monkeypatch.setattr(
        handler_module,
        "try_acquire_completion_finalize",
        lambda batch_id, now: next(acquire_results),
    )
    marks: list[tuple[str, str]] = []
    monkeypatch.setattr(
        handler_module,
        "mark_completion_finalize_completed",
        lambda batch_id, token, now: marks.append((batch_id, token)) or True,
    )
    service = _Issue31FakeNotificationService()

    for _ in range(2):  # retryによるis_complete再成立(二重トリガー)を再現
        handler_module._finish_batch_item(
            "batch-1",
            "sent",
            "owner-a#2914",
            _NOW,
            service,  # type: ignore[arg-type]
            _Issue31FakeRuntimeConfig(),  # type: ignore[arg-type]
        )

    assert len(service.summary_calls) == 1  # summaryフローは1回だけ
    assert marks == [("batch-1", "issue31-token")]


# --- Issue #75 Phase B1(2026-08-30): 利確判定不能の holdings pipeline への接続 ---
#
# recommendation=None でも、「判定できたうえで利確シグナルなし」と
# 「入力が不正で判定そのものが成立しなかった」は別状態である。
# 以前は data_error を参照しておらず、両者とも COMPLETED / NO_SIGNAL / "hold" として
# 記録されていたため、運用者が沈黙抑止に気付けなかった。


class _InvalidCostOutcome:
    """ProfitTakingService が取得原価不正を検出したときの outcome。"""

    recommendation = None
    data_error = (
        "平均取得単価が不正なため利確判定は不能"
        "(平均取得単価・総取得金額は正である必要があります: average_purchase_price=0)"
    )
    triggered_rule_names: tuple[str, ...] = ()
    audit_id: str | None = None


def _run_holding_task_with_profit_taking_outcome(
    monkeypatch: pytest.MonkeyPatch, outcome: object
) -> dict[str, object]:
    _patch_common(monkeypatch)
    target = _holding("2914")
    monkeypatch.setattr(handler_module.HoldingRepository, "get", lambda self, holding_id: target)
    monkeypatch.setattr(
        handler_module,
        "build_stock_snapshot",
        lambda *a, **kw: (_FakeSnapshot(current_price=Decimal("1200")), None),
    )
    monkeypatch.setattr(
        handler_module.SellSignalService, "analyze", lambda self, *a, **kw: _NoSignalOutcome()
    )
    monkeypatch.setattr(
        handler_module.ProfitTakingService, "analyze", lambda self, *a, **kw: outcome
    )
    return handler_module.handler(
        {
            "task": "holding",
            "holding_id": build_holding_id(DEFAULT_OWNER, "2914"),
            "portfolio_total_market_value": None,
            "portfolio_total_acquisition_cost": None,
        },
        _FakeContext(),
    )


def test_profit_taking_input_invalid_is_recorded_as_data_insufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T8: 判定不能が EvaluationStatus.DATA_INSUFFICIENT へ到達する。"""
    result = _run_holding_task_with_profit_taking_outcome(monkeypatch, _InvalidCostOutcome())

    assert result["evaluation_status"] == "DATA_INSUFFICIENT"


def test_profit_taking_no_signal_remains_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    """T10: 正常な「利確シグナルなし」は従来どおり COMPLETED のまま(回帰)。

    T8 と対にすることで、両者が pipeline 上で区別されることを示す。
    """
    result = _run_holding_task_with_profit_taking_outcome(monkeypatch, _NoSignalOutcome())

    assert result["evaluation_status"] == "COMPLETED"


# --- Issue #57 Phase B1: completion_id 伝播 / finalize failure persistence ------


def test_i57_holdings_passes_holding_id_as_completion_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1: holdingsはholding_id(owner#stock_code)をcompletion_idとして渡す。
    同一銘柄を複数ownerが保有しても別件として数えるため(M3.1の既存方針)。"""
    captured: dict[str, object] = {}

    def _fake_record_result(batch_id, category, stock_code=None, **kwargs):
        captured.update(kwargs)
        captured["stock_code"] = stock_code
        return None

    monkeypatch.setattr(handler_module, "record_result", _fake_record_result)

    handler_module._finish_batch_item(
        batch_id="batch-1",
        category="hold",
        holding_id="owner-a#8306",
        now=_NOW,
        notification_service=object(),
        runtime_config_service=object(),
    )

    assert captured["completion_id"] == "owner-a#8306"


# --- Issue #57 Phase B2: FINALIZE_ONLY は worker 処理を再実行しない ---------------


def test_b2_holdings_finalize_only_does_not_reexecute_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T14/T30: holdings の FINALIZE_ONLY でも worker 処理は走らない。
    サマリー送信は通常経路と同一の `_send_batch_summary()` を通る。"""
    from jstock_advisor.domain.entities.execution_context import ExecutionContext
    from jstock_advisor.infrastructure.aws.batch_tracker import (
        BatchFamily,
        BatchProgress,
        CompletionBatchRecord,
    )

    def _must_not_run(*_a, **_kw):
        pytest.fail("worker path must not be re-executed by FINALIZE_ONLY")

    monkeypatch.setattr(handler_module, "_process_single_holding", _must_not_run)
    monkeypatch.setattr(handler_module, "record_result", _must_not_run)
    monkeypatch.setattr(handler_module, "dispatch_async", _must_not_run)
    monkeypatch.setattr(handler_module, "start_batch", _must_not_run)

    progress = BatchProgress(
        total=1,
        completed=1,
        category_counts={},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[],
        sector_entries=[],
        holding_count=0,
        completed_codes=["owner-a#8306"],
    )
    record = CompletionBatchRecord(
        batch_id="hold-1",
        family=BatchFamily.HOLDINGS_WATCHLIST,
        execution_context=ExecutionContext.normal(),
        progress=progress,
        attempt_count=1,
        finalize_started_at=None,
        finalize_completed_at=None,
        finalize_failed_at=None,
    )
    monkeypatch.setattr(
        handler_module, "resolve_finalize_only_request", lambda *a, **kw: record
    )
    summary_calls: list[str] = []
    monkeypatch.setattr(
        handler_module,
        "_send_batch_summary",
        lambda batch_id, *a, **kw: summary_calls.append(batch_id),
    )

    result = handler_module.handler(
        {
            "recovery_action": "FINALIZE_ONLY",
            "batch_id": "hold-1",
            "batch_family": "HOLDINGS_WATCHLIST",
            "execution_mode": "NORMAL",
        },
        _FakeContext(),
    )

    assert result == {"finalize_recovery": "ATTEMPTED"}
    assert summary_calls == ["hold-1"]


def test_b2_holdings_kill_switch_blocks_recovery_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T11/T12: kill switch ON 中は recovery でも gate を取らずサマリーも送らない
    (recovery 専用経路で kill switch を迂回しない)。解除後は次回 recovery で回復する。"""
    from jstock_advisor.infrastructure.aws.batch_tracker import BatchProgress

    progress = BatchProgress(
        total=1,
        completed=1,
        category_counts={},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[],
        sector_entries=[],
        holding_count=0,
        completed_codes=["owner-a#8306"],
    )
    monkeypatch.setattr(
        handler_module, "try_acquire_completion_finalize",
        lambda *a, **kw: pytest.fail("kill switch ON must not acquire the gate"),
    )

    class _KillSwitchOff:
        def get_notification_enabled(self) -> bool:
            return False

    handler_module._send_batch_summary(
        "hold-1", progress, _NOW, object(), _KillSwitchOff()
    )
