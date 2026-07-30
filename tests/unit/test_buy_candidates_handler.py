import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import (
    BuyAction,
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
        buy_action = None
        ranking_group = None

    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: _FakeOutcome()
    )

    result = handler_module.handler(
        {"task": "buy_candidate", "stock_code": "2914"}, _FakeContext()
    )

    assert result == {"stock_code": "2914", "recommended": False, "notified": False}


def _make_recommendation(
    stock_code: str,
    company_quality_score: float,
    recommendation_id: str = "rec-1",
    buy_action: BuyAction = BuyAction.BUY,
    purchase_attractiveness_score: float = 50.0,
    current_vs_entry_price_pct: str | None = None,
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
        company_quality_score=company_quality_score,
        purchase_attractiveness_score=purchase_attractiveness_score,
        current_vs_entry_price_pct=Decimal(current_vs_entry_price_pct)
        if current_vs_entry_price_pct is not None
        else None,
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
    """evaluate_notification_status/notify_buy_candidates_digest/notify_batch_summary
    の呼び出しを記録するフェイク(優先度付け通知のロジックのみを検証する)。
    """

    def __init__(self, eligibility_by_stock: dict[str, NotificationOutcome]) -> None:
        self._eligibility_by_stock = eligibility_by_stock
        self.sent_recommendations: list[Recommendation] = []
        self.digest_calls: list[list[Recommendation]] = []
        self.batch_summary_calls: list[dict[str, object]] = []
        self.evaluate_calls_context: list[object] = []

    def evaluate_notification_status(
        self,
        recommendation: Recommendation,
        now: dt.datetime,
        context: object | None = None,
    ) -> NotificationOutcome:
        self.evaluate_calls_context.append(context)
        return self._eligibility_by_stock[recommendation.stock_code]

    def send_recommendation_notification(
        self, recommendation: Recommendation, now: dt.datetime
    ) -> None:
        self.sent_recommendations.append(recommendation)

    def notify_buy_candidates_digest(
        self, winners: list[Recommendation], now: dt.datetime
    ) -> int:
        self.digest_calls.append(list(winners))
        self.sent_recommendations.extend(winners)
        return len(winners)

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
        send_empty_summary: bool = True,
    ) -> bool:
        self.batch_summary_calls.append(
            {
                "total": total,
                "category_counts": dict(category_counts),
                "buy_candidates_sent_count": buy_candidates_sent_count,
                "send_empty_summary": send_empty_summary,
            }
        )
        return True


def test_process_single_candidate_defers_send_and_records_buy_ranking_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """買い候補判定が成立しても、ワーカー単体ではLINE送信せず、ランキング候補として
    登録するだけであることを確認する(2026-07 BUYパイプライン再設計)。
    """
    recommendation = _make_recommendation(
        "2914", company_quality_score=72.5, recommendation_id="rec-1", buy_action=BuyAction.BUY
    )
    outcome = _outcome(recommendation, ranking_group="buy_candidate")
    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome
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


def test_process_single_candidate_watch_price_counted_without_ranking_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """価格待ち(WATCH_FOR_PRICE)はLINE通知対象外のため、件数のみ集計し
    ランキング登録は行わない(BUYパイプライン第2次修正2026-07)。
    """
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
    fake_service = _FakeNotificationServiceForRanking({})
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
        "7239", "batch-1", _NOW, object(), _CONFIG, object(), repo, fake_service
    )

    assert captured["category"] == "watch_not_ranked"
    assert captured["ranking_entry"] is None
    # evaluate_notification_statusは呼ばれない(上位N件確定後のみ評価する設計)。
    assert fake_service.sent_recommendations == []


def test_process_single_candidate_does_not_evaluate_eligibility_per_stock(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """再通知抑止・データ品質チェックは_finalize_batchで上位N件のみに対して
    行う設計のため、_process_single_candidate単体ではevaluate_notification_status
    を一切呼ばない(BUYパイプライン第2次修正2026-07。要求仕様15節)。
    """
    recommendation = _make_recommendation(
        "2914", company_quality_score=72.5, recommendation_id="rec-1"
    )
    outcome = _outcome(recommendation, ranking_group="buy_candidate")
    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome
    )

    class _EligibilityCallRecordingService(_FakeNotificationServiceForRanking):
        def evaluate_notification_status(self, recommendation, now, context=None):
            raise AssertionError(
                "_process_single_candidateはevaluate_notification_statusを呼ばないはず"
            )

    fake_service = _EligibilityCallRecordingService({})
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

    assert captured["category"] == "candidate_not_ranked"
    assert captured["ranking_entry"] is not None


def test_process_single_candidate_review_when_manual_review_action(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """buy_action=MANUAL_REVIEW(整合性検証違反)の場合、data_quality_blockedでなくても
    reviewカテゴリへ振り分ける(要求仕様20節)。
    """
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
    fake_service = _FakeNotificationServiceForRanking(
        {"2914": NotificationOutcome(status=NotificationStatus.SENT, sent=False)}
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


def test_process_single_candidate_excluded_maps_to_hold(monkeypatch: pytest.MonkeyPatch) -> None:
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
    fake_service = _FakeNotificationServiceForRanking({})

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda batch_id, category, stock_code=None, ranking_entry=None: captured.update(
            category=category
        ),
    )

    # EXCLUDEDパスはrecommendation_repo.saveを呼ばないことを、saveされたら
    # 失敗するダミーリポジトリで直接検証する。
    class _NoSaveRepo:
        def save(self, *_a, **_kw):
            raise AssertionError("EXCLUDEDの場合はRecommendationを保存しないはず")

        def get(self, *_a, **_kw):
            return None

    handler_module._process_single_candidate(
        "9861", "batch-1", _NOW, object(), _CONFIG, object(), _NoSaveRepo(), fake_service
    )

    assert captured["category"] == "hold"


def _config_with_max_notifications(max_notifications: int):
    return _CONFIG.model_copy(
        update={
            "notification": _CONFIG.notification.model_copy(
                update={"buy_candidate_max_notifications_per_run": max_notifications}
            )
        }
    )


def test_finalize_batch_ranks_buy_candidates_and_digests_top_n(tmp_path) -> None:
    """購入候補ランキングは(action_priority, purchase_attractiveness_score,
    company_quality_score, discount_to_standard_pct)の降順で、上位N件のみを
    1回のnotify_buy_candidates_digest呼び出しにまとめて送信する
    (BUYパイプライン第2次修正2026-07。要求仕様15節・17節)。価格待ちは
    ランキング・送信の対象外(件数のみバッチサマリーに反映)。
    """
    repo = RecommendationRepository(store_dir=tmp_path)

    buy_specs = [
        ("1111", BuyAction.STRONG_BUY, 90.0, 60.0),  # action_priority最大
        ("2222", BuyAction.BUY, 30.0, 60.0),
        ("3333", BuyAction.BUY, 60.0, 60.0),
        ("4444", BuyAction.SMALL_ENTRY, 10.0, 60.0),
        ("5555", BuyAction.BUY, 50.0, 60.0),
    ]
    ranking_entries = []
    eligibility: dict[str, NotificationOutcome] = {}
    for i, (code, action, purchase_score, quality_score) in enumerate(buy_specs):
        rec_id = f"buy-{i}"
        rec = _make_recommendation(
            code,
            company_quality_score=quality_score,
            recommendation_id=rec_id,
            buy_action=action,
            purchase_attractiveness_score=purchase_score,
        )
        repo.save(rec)
        ranking_entries.append(handler_module._encode_buy_ranking_entry(rec))
        eligibility[code] = NotificationOutcome(status=NotificationStatus.SENT, sent=False)

    config = _config_with_max_notifications(2)
    progress = handler_module.BatchProgress(
        total=8,
        completed=8,
        category_counts={"candidate_not_ranked": 5, "watch_not_ranked": 3},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=ranking_entries,
    )
    fake_service = _FakeNotificationServiceForRanking(eligibility)

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

    # 購入候補: STRONG_BUY(1111)が最優先、次点はBUY同士でpurchase_attractiveness_score
    # が高い方(3333=60 > 5555=50 > 2222=30)。上位2件は1111, 3333。
    assert len(fake_service.digest_calls) == 1
    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["1111", "3333"]

    assert len(fake_service.batch_summary_calls) == 1
    call = fake_service.batch_summary_calls[0]
    assert call["category_counts"]["sent"] == 2
    assert call["category_counts"]["candidate_not_ranked"] == 3  # 5-2
    assert call["category_counts"]["watch_not_ranked"] == 3  # 変更なし(ランキング対象外)
    assert call["buy_candidates_sent_count"] == 2


def test_finalize_batch_excludes_suppressed_and_data_quality_blocked_winners(tmp_path) -> None:
    """上位N件のうち、再通知抑止・データ品質チェックで送信不可と判定された
    銘柄はnotify_buy_candidates_digestへ渡さず、それぞれsuppressed/reviewの
    件数として計上する(BUYパイプライン第2次修正2026-07。要求仕様15節)。
    """
    repo = RecommendationRepository(store_dir=tmp_path)

    specs = [
        ("1111", BuyAction.STRONG_BUY, NotificationStatus.SENT, False),
        ("2222", BuyAction.BUY, NotificationStatus.DUPLICATE_SUPPRESSED, False),
        (
            "3333",
            BuyAction.BUY,
            NotificationStatus.NOT_REQUIRED,
            True,
        ),  # data_quality_blocked
    ]
    ranking_entries = []
    eligibility: dict[str, NotificationOutcome] = {}
    for i, (code, action, status, blocked) in enumerate(specs):
        rec = _make_recommendation(
            code,
            company_quality_score=60.0,
            recommendation_id=f"rec-{i}",
            buy_action=action,
            purchase_attractiveness_score=50.0 - i,
        )
        repo.save(rec)
        ranking_entries.append(handler_module._encode_buy_ranking_entry(rec))
        eligibility[code] = NotificationOutcome(
            status=status, sent=False, data_quality_blocked=blocked
        )

    config = _config_with_max_notifications(5)
    progress = handler_module.BatchProgress(
        total=3,
        completed=3,
        category_counts={"candidate_not_ranked": 3},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=ranking_entries,
    )
    fake_service = _FakeNotificationServiceForRanking(eligibility)

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["1111"]

    call = fake_service.batch_summary_calls[0]
    assert call["category_counts"]["sent"] == 1
    assert call["category_counts"]["suppressed"] == 1
    assert call["category_counts"]["review"] == 1
    assert call["category_counts"]["candidate_not_ranked"] == 0
    assert call["buy_candidates_sent_count"] == 1


def test_finalize_batch_reports_zero_buy_candidates_sent_when_none_ranked(tmp_path) -> None:
    repo = RecommendationRepository(store_dir=tmp_path)
    progress = handler_module.BatchProgress(
        total=3,
        completed=3,
        category_counts={"hold": 1, "watch_not_ranked": 2},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[],
    )
    fake_service = _FakeNotificationServiceForRanking({})

    handler_module._finalize_batch(progress, _CONFIG, _NOW, repo, fake_service)

    assert fake_service.batch_summary_calls[0]["buy_candidates_sent_count"] == 0
    assert fake_service.digest_calls == [[]]


def test_finalize_batch_passes_send_empty_summary_from_config(tmp_path) -> None:
    repo = RecommendationRepository(store_dir=tmp_path)
    progress = handler_module.BatchProgress(
        total=1,
        completed=1,
        category_counts={"hold": 1},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[],
    )
    config = _CONFIG.model_copy(
        update={
            "notification": _CONFIG.notification.model_copy(
                update={"send_empty_summary": False}
            )
        }
    )
    fake_service = _FakeNotificationServiceForRanking({})

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

    assert fake_service.batch_summary_calls[0]["send_empty_summary"] is False


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
    """個別のデータ取得エラーは既定でLINE個別通知しない(BUYパイプライン第3次修正
    2026-07)。CloudWatch警告ログとdata_insufficientカテゴリへの計上のみ行う。
    """
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

    fake_service = _NotifyDataErrorAssertingService({})
    repo = RecommendationRepository(store_dir=tmp_path)

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda batch_id, category, stock_code=None, ranking_entry=None: captured.update(
            category=category
        ),
    )

    with caplog.at_level("WARNING"):
        result = handler_module._process_single_candidate(
            "2914",
            "batch-1",
            _NOW,
            object(),
            _config_with_notify_data_errors(False),
            object(),
            repo,
            fake_service,
        )

    assert result == {"stock_code": "2914", "recommended": False, "notified": False}
    assert captured["category"] == "data_insufficient"
    assert "buy_candidate_data_error stock_code=2914" in caplog.text
    assert "テストエラー" in caplog.text


def test_process_single_candidate_data_error_notifies_line_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """notify_data_errors=trueの場合のみ、従来通りnotify_data_errorを呼ぶ
    (運用上どうしても個別通知が必要な場合の明示的なオプトイン)。
    """
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

    fake_service = _NotifyDataErrorRecordingService({})
    repo = RecommendationRepository(store_dir=tmp_path)

    handler_module._process_single_candidate(
        "2914",
        None,
        _NOW,
        object(),
        _config_with_notify_data_errors(True),
        object(),
        repo,
        fake_service,
    )

    assert calls == ["2914"]


def _add_ranked_candidate(
    repo: RecommendationRepository,
    ranking_entries: list[str],
    eligibility: dict[str, NotificationOutcome],
    stock_code: str,
    purchase_score: float,
    outcome: NotificationOutcome,
    recommendation_id: str | None = None,
) -> None:
    rec = _make_recommendation(
        stock_code,
        company_quality_score=60.0,
        recommendation_id=recommendation_id or f"rec-{stock_code}",
        buy_action=BuyAction.BUY,
        purchase_attractiveness_score=purchase_score,
    )
    repo.save(rec)
    ranking_entries.append(handler_module._encode_buy_ranking_entry(rec))
    eligibility[stock_code] = outcome


def test_finalize_batch_promotes_lower_ranked_candidate_when_top_is_suppressed(
    tmp_path,
) -> None:
    """1位が再送抑止で除外されても、下位の適格候補(6位)が繰り上げられ、
    最終的に上限件数(5件)が送信されることを確認する(BUYパイプライン第3次修正
    2026-07。従来は上位N件を先に切り出していたため繰り上げが起きなかった)。
    """
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    eligibility: dict[str, NotificationOutcome] = {}

    # purchase_scoreの降順が = ランキング順位。1位を抑止し、6位まで作る。
    scores = [100.0, 90.0, 80.0, 70.0, 60.0, 50.0]
    for rank, (code, score) in enumerate(
        zip(["1st", "2nd", "3rd", "4th", "5th", "6th"], scores, strict=True), start=1
    ):
        status = (
            NotificationStatus.DUPLICATE_SUPPRESSED if rank == 1 else NotificationStatus.SENT
        )
        _add_ranked_candidate(
            repo,
            ranking_entries,
            eligibility,
            code,
            score,
            NotificationOutcome(status=status, sent=False),
        )

    config = _config_with_max_notifications(5)
    progress = handler_module.BatchProgress(
        total=6,
        completed=6,
        category_counts={"candidate_not_ranked": 6},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=ranking_entries,
    )
    fake_service = _FakeNotificationServiceForRanking(eligibility)

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["2nd", "3rd", "4th", "5th", "6th"]
    assert len(digested_codes) == 5

    call = fake_service.batch_summary_calls[0]
    assert call["buy_candidates_sent_count"] == 5
    assert call["category_counts"]["suppressed"] == 1
    assert call["category_counts"]["candidate_not_ranked"] == 0


def test_finalize_batch_sends_up_to_max_when_multiple_top_ranked_are_suppressed(
    tmp_path,
) -> None:
    """上位5件のうち3件が抑止されても、下位の適格候補で埋め合わせて上限
    (5件)まで送信することを確認する。
    """
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    eligibility: dict[str, NotificationOutcome] = {}

    codes = [f"c{i}" for i in range(1, 9)]  # 8件、上位5件中3件を抑止
    suppressed_ranks = {1, 2, 3}
    scores = [80.0 - i for i in range(8)]
    for rank, (code, score) in enumerate(zip(codes, scores, strict=True), start=1):
        status = (
            NotificationStatus.DUPLICATE_SUPPRESSED
            if rank in suppressed_ranks
            else NotificationStatus.SENT
        )
        _add_ranked_candidate(
            repo,
            ranking_entries,
            eligibility,
            code,
            score,
            NotificationOutcome(status=status, sent=False),
        )

    config = _config_with_max_notifications(5)
    progress = handler_module.BatchProgress(
        total=8,
        completed=8,
        category_counts={"candidate_not_ranked": 8},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=ranking_entries,
    )
    fake_service = _FakeNotificationServiceForRanking(eligibility)

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["c4", "c5", "c6", "c7", "c8"]
    assert fake_service.batch_summary_calls[0]["buy_candidates_sent_count"] == 5


def test_finalize_batch_sends_exactly_all_eligible_when_fewer_than_max(tmp_path) -> None:
    """適格な候補が上限件数(5件)未満(3件)しか無い場合、その3件のみを送信し、
    存在しない候補を無理に水増ししない。"""
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    eligibility: dict[str, NotificationOutcome] = {}

    for code, score in [("a", 90.0), ("b", 80.0), ("c", 70.0)]:
        _add_ranked_candidate(
            repo,
            ranking_entries,
            eligibility,
            code,
            score,
            NotificationOutcome(status=NotificationStatus.SENT, sent=False),
        )

    config = _config_with_max_notifications(5)
    progress = handler_module.BatchProgress(
        total=3,
        completed=3,
        category_counts={"candidate_not_ranked": 3},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=ranking_entries,
    )
    fake_service = _FakeNotificationServiceForRanking(eligibility)

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

    digested_codes = [r.stock_code for r in fake_service.digest_calls[0]]
    assert digested_codes == ["a", "b", "c"]
    assert fake_service.batch_summary_calls[0]["buy_candidates_sent_count"] == 3


def test_finalize_batch_sends_nothing_when_all_candidates_are_suppressed(tmp_path) -> None:
    """適格な候補が0件の場合は送信しない(digestは空リストで呼ばれる)。"""
    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    eligibility: dict[str, NotificationOutcome] = {}

    for code, score in [("a", 90.0), ("b", 80.0)]:
        _add_ranked_candidate(
            repo,
            ranking_entries,
            eligibility,
            code,
            score,
            NotificationOutcome(status=NotificationStatus.DUPLICATE_SUPPRESSED, sent=False),
        )

    config = _config_with_max_notifications(5)
    progress = handler_module.BatchProgress(
        total=2,
        completed=2,
        category_counts={"candidate_not_ranked": 2},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=ranking_entries,
    )
    fake_service = _FakeNotificationServiceForRanking(eligibility)

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

    assert fake_service.digest_calls == [[]]
    assert fake_service.batch_summary_calls[0]["buy_candidates_sent_count"] == 0
    assert fake_service.batch_summary_calls[0]["category_counts"]["suppressed"] == 2


def test_finalize_batch_evaluates_with_buy_candidate_batch_context(tmp_path) -> None:
    """evaluate_notification_statusはBUY_CANDIDATE_BATCHコンテキストで呼ばれる
    (BUYパイプライン第3次修正2026-07。要手動確認LINE安全弁を抑止するため)。
    """
    from jstock_advisor.domain.entities.enums import NotificationContext

    repo = RecommendationRepository(store_dir=tmp_path)
    ranking_entries: list[str] = []
    eligibility: dict[str, NotificationOutcome] = {}
    _add_ranked_candidate(
        repo,
        ranking_entries,
        eligibility,
        "2914",
        90.0,
        NotificationOutcome(status=NotificationStatus.SENT, sent=False),
    )

    config = _config_with_max_notifications(5)
    progress = handler_module.BatchProgress(
        total=1,
        completed=1,
        category_counts={"candidate_not_ranked": 1},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=ranking_entries,
    )
    fake_service = _FakeNotificationServiceForRanking(eligibility)

    handler_module._finalize_batch(progress, config, _NOW, repo, fake_service)

    assert fake_service.evaluate_calls_context == [NotificationContext.BUY_CANDIDATE_BATCH]
