import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    NotificationStatus,
    RecommendationType,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers import buy_candidates_handler as handler_module
from jstock_advisor.services.line_notification_service import NotificationOutcome

_NOW = dt.datetime(2026, 7, 29, 7, 0, tzinfo=dt.UTC)
_CONFIG = load_config()


class _FakeContext:
    function_name = "jstock-advisor-buy-candidates"


def _watchlist_item(stock_code: str) -> WatchlistItem:
    return WatchlistItem(
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler_module, "build_real_provider_bundle", lambda now, config: object())
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


def test_dispatch_mode_dispatches_one_call_per_watchlist_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    items = [_watchlist_item("2914"), _watchlist_item("8136")]
    monkeypatch.setattr(handler_module.WatchlistService, "list_items", lambda self: items)

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
    } in stripped
    assert {
        "fn": "jstock-advisor-buy-candidates",
        "task": "buy_candidate",
        "stock_code": "8136",
    } in stripped


def test_task_buy_candidate_processes_only_requested_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(handler_module.WatchlistService, "get_item", lambda self, code: None)

    class _FakeOutcome:
        data_error = "テストエラー"
        recommendation = None

    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: _FakeOutcome()
    )

    result = handler_module.handler(
        {"task": "buy_candidate", "stock_code": "2914"}, _FakeContext()
    )

    assert result == {"stock_code": "2914", "recommended": False, "notified": False}


def _make_recommendation(
    stock_code: str, total_score: float, recommendation_id: str = "rec-1"
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            tentative=PriceWithRationale(price=Decimal("3500"), rationale="x"),
            standard=PriceWithRationale(price=Decimal("3300"), rationale="x"),
            aggressive=PriceWithRationale(price=Decimal("3100"), rationale="x"),
        ),
        price_at_recommendation=Decimal("4200"),
        total_score=total_score,
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


class _FakeNotificationServiceForRanking:
    """evaluate_notification_status/send_recommendation_notification/notify_batch_summary
    の呼び出しを記録するフェイク(優先度付け通知のロジックのみを検証する)。
    """

    def __init__(self, eligibility_by_stock: dict[str, NotificationOutcome]) -> None:
        self._eligibility_by_stock = eligibility_by_stock
        self.sent_recommendations: list[Recommendation] = []
        self.batch_summary_calls: list[dict[str, object]] = []

    def evaluate_notification_status(
        self, recommendation: Recommendation, now: dt.datetime
    ) -> NotificationOutcome:
        return self._eligibility_by_stock[recommendation.stock_code]

    def send_recommendation_notification(
        self, recommendation: Recommendation, now: dt.datetime
    ) -> None:
        self.sent_recommendations.append(recommendation)

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
    ) -> bool:
        self.batch_summary_calls.append(
            {"total": total, "category_counts": dict(category_counts)}
        )
        return True


def test_process_single_candidate_defers_send_and_records_ranking_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """買いシグナルが成立しても、ワーカー単体ではLINE送信せず、スコアとともに
    ランキング候補として登録するだけであることを確認する(2026-07仕様追加)。
    """
    from jstock_advisor.services.buy_signal_service import BuyAnalysisOutcome

    recommendation = _make_recommendation("2914", total_score=72.5, recommendation_id="rec-1")
    monkeypatch.setattr(
        handler_module.BuySignalService,
        "analyze",
        lambda self, *a, **kw: BuyAnalysisOutcome(
            stock_code="2914",
            recommendation=recommendation,
            screening_passed=True,
            exclusion_reasons=[],
            data_error=None,
        ),
    )
    fake_service = _FakeNotificationServiceForRanking(
        {"2914": NotificationOutcome(status=NotificationStatus.SENT, sent=False)}
    )
    repo = RecommendationRepository(store_dir=tmp_path)

    result = handler_module._process_single_candidate(
        "2914", None, _NOW, object(), _CONFIG, object(), repo, fake_service
    )

    assert result == {"stock_code": "2914", "recommended": True, "notified": False}
    assert fake_service.sent_recommendations == []
    assert repo.get("rec-1") is not None


def test_process_single_candidate_suppressed_when_not_eligible(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from jstock_advisor.services.buy_signal_service import BuyAnalysisOutcome

    recommendation = _make_recommendation("2914", total_score=72.5, recommendation_id="rec-1")
    monkeypatch.setattr(
        handler_module.BuySignalService,
        "analyze",
        lambda self, *a, **kw: BuyAnalysisOutcome(
            stock_code="2914",
            recommendation=recommendation,
            screening_passed=True,
            exclusion_reasons=[],
            data_error=None,
        ),
    )
    fake_service = _FakeNotificationServiceForRanking(
        {
            "2914": NotificationOutcome(
                status=NotificationStatus.DUPLICATE_SUPPRESSED, sent=False
            )
        }
    )
    repo = RecommendationRepository(store_dir=tmp_path)

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda batch_id, category, stock_code=None, ranking_entry=None: captured.update(
            category=category, ranking_entry=ranking_entry
        ),
    )

    handler_module._process_single_candidate(
        "2914", "batch-1", _NOW, object(), _CONFIG, object(), repo, fake_service
    )

    assert captured["category"] == "suppressed"
    assert captured["ranking_entry"] is None


def test_process_single_candidate_review_when_data_quality_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from jstock_advisor.services.buy_signal_service import BuyAnalysisOutcome

    recommendation = _make_recommendation("2914", total_score=72.5, recommendation_id="rec-1")
    monkeypatch.setattr(
        handler_module.BuySignalService,
        "analyze",
        lambda self, *a, **kw: BuyAnalysisOutcome(
            stock_code="2914",
            recommendation=recommendation,
            screening_passed=True,
            exclusion_reasons=[],
            data_error=None,
        ),
    )
    fake_service = _FakeNotificationServiceForRanking(
        {
            "2914": NotificationOutcome(
                status=NotificationStatus.NOT_REQUIRED, sent=False, data_quality_blocked=True
            )
        }
    )
    repo = RecommendationRepository(store_dir=tmp_path)

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda batch_id, category, stock_code=None, ranking_entry=None: captured.update(
            category=category, ranking_entry=ranking_entry
        ),
    )

    handler_module._process_single_candidate(
        "2914", "batch-1", _NOW, object(), _CONFIG, object(), repo, fake_service
    )

    assert captured["category"] == "review"
    assert captured["ranking_entry"] is None


def test_finalize_batch_notifies_only_top_n_by_score(tmp_path) -> None:
    """買い候補スコア上位N件(config.notification.buy_candidate_max_notifications_per_run)
    のみを実際に送信し、残りはcandidate_not_rankedのまま通知しないことを確認する。
    """
    repo = RecommendationRepository(store_dir=tmp_path)
    scores = {"1111": 90.0, "2222": 30.0, "3333": 60.0, "4444": 10.0, "5555": 50.0, "6666": 80.0}
    ranking_entries = []
    for i, (code, score) in enumerate(scores.items()):
        rec_id = f"rec-{i}"
        repo.save(_make_recommendation(code, total_score=score, recommendation_id=rec_id))
        ranking_entries.append(handler_module._encode_ranking_entry(score, code, rec_id))

    config = _CONFIG.model_copy(
        update={
            "notification": _CONFIG.notification.model_copy(
                update={"buy_candidate_max_notifications_per_run": 3}
            )
        }
    )
    progress = handler_module.BatchProgress(
        total=6,
        completed=6,
        category_counts={"candidate_not_ranked": 6},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=ranking_entries,
    )
    fake_service = _FakeNotificationServiceForRanking({})

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

    sent_codes = sorted(r.stock_code for r in fake_service.sent_recommendations)
    assert sent_codes == ["1111", "3333", "6666"]  # スコア90, 60, 80の上位3件

    assert len(fake_service.batch_summary_calls) == 1
    counts = fake_service.batch_summary_calls[0]["category_counts"]
    assert counts["sent"] == 3
    assert counts["candidate_not_ranked"] == 3
